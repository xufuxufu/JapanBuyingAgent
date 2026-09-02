from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

import app.image_search as image_search
import app.main as main_module
from app.models import Product

REAL_MODEL_PATH = image_search.IMAGE_SEARCH_MODEL_DIR / "clip-vit-b32-vision-int8.onnx"
MODEL_AVAILABLE = REAL_MODEL_PATH.is_file()
requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason="ONNX CLIP model not prepared locally (run scripts/prepare_image_search_model.py)",
)


def jpeg_bytes(color=(40, 80, 120), size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def isolated_index_paths(tmp_path, monkeypatch):
    index_dir = tmp_path / "image-search"
    index_dir.mkdir()
    monkeypatch.setattr(image_search, "IMAGE_SEARCH_DIR", index_dir)
    monkeypatch.setattr(image_search, "INDEX_PATH", index_dir / "products.faiss")
    monkeypatch.setattr(image_search, "MAP_PATH", index_dir / "product_image_map.json")
    monkeypatch.setattr(image_search, "META_PATH", index_dir / "index_meta.json")
    monkeypatch.setattr(image_search, "_loaded_index", None)
    monkeypatch.setattr(image_search, "_build_in_progress", False)
    return index_dir


@pytest.fixture
def isolated_model_path(monkeypatch):
    if MODEL_AVAILABLE:
        monkeypatch.setattr(image_search, "MODEL_PATH", REAL_MODEL_PATH)
    monkeypatch.setattr(image_search, "_model_session", None)


def _write_fake_index_for_products(index_dir, product_ids: list[int]):
    """Deterministic 512-dim index whose vector i == one-hot(i) so a query
    embedding of one-hot(0) always ranks product_ids[0] as the exact match."""
    import faiss
    dim = 512
    vectors = np.zeros((len(product_ids), dim), dtype=np.float32)
    for i in range(len(product_ids)):
        vectors[i, i % dim] = 1.0
    idx = faiss.IndexFlatIP(dim)
    idx.add(vectors)
    faiss.write_index(idx, str(index_dir / "products.faiss"))
    (index_dir / "product_image_map.json").write_text(json.dumps(product_ids), encoding="utf-8")
    (index_dir / "index_meta.json").write_text(json.dumps({
        "built_at": "2026-01-01T00:00:00+00:00", "model_name": "fake-test-model",
        "embedding_dim": dim, "product_count": len(product_ids), "image_count": len(product_ids),
        "skipped_no_image": 0, "failed_image_download": 0, "duration_seconds": 0.1,
    }), encoding="utf-8")
    return vectors


def _fake_embed_one_hot(images):
    dim = 512
    out = np.zeros((len(images), dim), dtype=np.float32)
    out[:, 0] = 1.0
    return out


# ==================== no index -> friendly, no 500 ====================

def test_image_search_api_returns_friendly_status_without_index(client, isolated_index_paths):
    http, _db, _tmp = client
    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "no_index"
    assert payload["results"] == []
    assert "索引" in payload["message"]


# ==================== malformed image rejected, not 500 ====================

def test_image_search_api_rejects_non_image_upload(client, isolated_index_paths):
    http, _db, _tmp = client
    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.txt", b"plain text, not an image", "text/plain")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["results"] == []


def test_image_search_api_rejects_oversized_upload(client, isolated_index_paths):
    http, _db, _tmp = client
    oversized = b"\xff\xd8\xff" + b"0" * (image_search.MAX_QUERY_IMAGE_BYTES + 1)
    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.jpg", oversized, "image/jpeg")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "error"


# ==================== full happy path against a fake but real-shaped index ====================

def test_image_search_api_returns_product_identity_and_inventory_fields(
    client, isolated_index_paths, monkeypatch,
):
    http, db, _tmp = client
    product = Product(internal_sku="IMGAPI-001", jan="4900000000001", qinsi_product_code="QC-001", name_cn="测试商品A")
    db.add(product)
    db.commit()
    db.refresh(product)

    _write_fake_index_for_products(isolated_index_paths, [product.id])
    monkeypatch.setattr(image_search, "embed_images", _fake_embed_one_hot)

    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["product_id"] == product.id
    assert result["jan"] == "4900000000001"
    assert result["qinsi_product_code"] == "QC-001"
    assert "similarity_score" in result
    # china/japan: no snapshot at all in this test DB -> unknown, must be null not 0
    assert result["china_known"] is False
    assert result["china_quantity"] is None
    assert result["japan_known"] is False
    assert result["japan_quantity"] is None


def test_image_search_api_known_zero_inventory_is_zero_not_null(client, isolated_index_paths, monkeypatch):
    from datetime import datetime, timezone
    from app.models import Location, QinsiInventorySnapshot, QinsiInventorySnapshotLine

    http, db, _tmp = client
    product = Product(internal_sku="IMGAPI-002", name_cn="测试商品B")
    db.add(product)
    db.commit()
    db.refresh(product)

    warehouse = Location(internal_code="QW-2025-QIANYU", display_name="千羽仓", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
    db.add(warehouse)
    db.flush()
    snapshot = QinsiInventorySnapshot(
        batch_no="QS-IMGAPI-TEST", original_filename="t.xlsx", file_hash="imgapi-hash-1",
        file_content=b"x", data_at=datetime.now(timezone.utc), status="completed",
    )
    db.add(snapshot)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snapshot.id, original_row_no=1, raw_summary_json=json.dumps({}),
        product_id=product.id, warehouse_id=warehouse.id, quantity=0, matching_status="matched",
        warehouse_status="matched",
    ))
    db.commit()

    _write_fake_index_for_products(isolated_index_paths, [product.id])
    monkeypatch.setattr(image_search, "embed_images", _fake_embed_one_hot)

    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")},
    )
    result = response.json()["results"][0]
    assert result["china_known"] is True
    assert result["china_quantity"] == 0
    assert result["japan_known"] is False
    assert result["japan_quantity"] is None


def test_image_search_api_similarity_field_present_and_descending(client, isolated_index_paths, monkeypatch):
    http, db, _tmp = client
    products = []
    for i in range(3):
        p = Product(internal_sku=f"IMGAPI-ORD-{i}", name_cn=f"排序测试{i}")
        db.add(p)
        products.append(p)
    db.commit()
    for p in products:
        db.refresh(p)

    import faiss
    dim = 512
    vectors = np.zeros((3, dim), dtype=np.float32)
    vectors[0, 0] = 1.0
    vectors[1, 0], vectors[1, 1] = 0.8, 0.2
    vectors[1] /= np.linalg.norm(vectors[1])
    vectors[2, 1] = 1.0
    idx = faiss.IndexFlatIP(dim)
    idx.add(vectors)
    faiss.write_index(idx, str(isolated_index_paths / "products.faiss"))
    (isolated_index_paths / "product_image_map.json").write_text(
        json.dumps([p.id for p in products]), encoding="utf-8",
    )
    (isolated_index_paths / "index_meta.json").write_text(json.dumps({
        "built_at": "t", "model_name": "fake", "embedding_dim": dim,
        "product_count": 3, "image_count": 3, "skipped_no_image": 0, "failed_image_download": 0,
        "duration_seconds": 0.1,
    }), encoding="utf-8")
    monkeypatch.setattr(image_search, "embed_images", _fake_embed_one_hot)

    response = http.post(
        "/api/products/image-search",
        files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")},
    )
    results = response.json()["results"]
    assert len(results) == 3
    scores = [r["similarity_score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0]["product_id"] == products[0].id


# ==================== top_k default / max via the API ====================

def test_image_search_api_top_k_default_is_ten(client, isolated_index_paths, monkeypatch):
    http, db, _tmp = client
    ids = []
    for i in range(15):
        p = Product(internal_sku=f"IMGAPI-TK-{i}")
        db.add(p)
        db.flush()
        ids.append(p.id)
    db.commit()
    _write_fake_index_for_products(isolated_index_paths, ids)
    monkeypatch.setattr(image_search, "embed_images", _fake_embed_one_hot)

    response = http.post("/api/products/image-search", files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")})
    assert len(response.json()["results"]) == image_search.TOP_K_DEFAULT


def test_image_search_api_top_k_clamped_to_max(client, isolated_index_paths, monkeypatch):
    http, db, _tmp = client
    ids = []
    for i in range(30):
        p = Product(internal_sku=f"IMGAPI-TKMAX-{i}")
        db.add(p)
        db.flush()
        ids.append(p.id)
    db.commit()
    _write_fake_index_for_products(isolated_index_paths, ids)
    monkeypatch.setattr(image_search, "embed_images", _fake_embed_one_hot)

    response = http.post(
        "/api/products/image-search",
        data={"top_k": "999"},
        files={"image": ("q.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert len(response.json()["results"]) == image_search.TOP_K_MAX


# ==================== rebuild-index endpoint: concurrency + no traceback ====================

def test_rebuild_index_endpoint_rejects_concurrent_trigger(client, isolated_index_paths):
    http, _db, _tmp = client
    image_search.try_reserve_build_slot()
    try:
        response = http.post("/api/products/image-search/rebuild-index")
        assert response.status_code == 202
        assert response.json()["status"] == "already_running"
    finally:
        image_search._release_build_slot()


def test_index_status_endpoint_reports_no_index(client, isolated_index_paths):
    http, _db, _tmp = client
    response = http.get("/api/products/image-search/index-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"] is False
    assert payload["building"] is False
    assert payload["meta"] is None
