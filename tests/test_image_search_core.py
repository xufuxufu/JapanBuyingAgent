from __future__ import annotations

import io
import json
import threading
import time

import numpy as np
import pytest
from PIL import Image

import app.image_search as image_search
import app.product_image_localization as pil_module
from app.image_search import (
    ImageSearchError, IndexCorruptError, IndexNotReadyError,
    TOP_K_DEFAULT, TOP_K_MAX,
)
from app.models import Product

REAL_MODEL_PATH = image_search.IMAGE_SEARCH_MODEL_DIR / "clip-vit-b32-vision-int8.onnx"
MODEL_AVAILABLE = REAL_MODEL_PATH.is_file()
requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason="ONNX CLIP model not prepared locally (run scripts/prepare_image_search_model.py)",
)


def jpeg_bytes(color=(30, 60, 90), size=(64, 64)) -> bytes:
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
    else:
        monkeypatch.setattr(image_search, "MODEL_PATH", image_search.IMAGE_SEARCH_MODEL_DIR / "does-not-exist.onnx")
    monkeypatch.setattr(image_search, "_model_session", None)


@pytest.fixture
def isolated_image_dirs(tmp_path, monkeypatch):
    main_dir = tmp_path / "product-main"
    qinsi_dir = tmp_path / "product-qinsi"
    main_dir.mkdir()
    qinsi_dir.mkdir()
    monkeypatch.setattr(pil_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(image_search, "PRODUCT_IMAGE_DIR", main_dir)
    monkeypatch.setattr(image_search, "QINSI_PRODUCT_IMAGE_DIR", qinsi_dir)
    return main_dir, qinsi_dir


def _write_fake_faiss_index(index_dir, vectors: np.ndarray, product_ids: list[int]) -> None:
    import faiss
    idx = faiss.IndexFlatIP(vectors.shape[1])
    idx.add(vectors.astype(np.float32))
    faiss.write_index(idx, str(index_dir / "products.faiss"))
    (index_dir / "product_image_map.json").write_text(json.dumps(product_ids), encoding="utf-8")
    (index_dir / "index_meta.json").write_text(json.dumps({
        "built_at": "2026-01-01T00:00:00+00:00", "model_name": "fake-test-model",
        "embedding_dim": int(vectors.shape[1]), "product_count": len(product_ids),
        "image_count": len(product_ids), "skipped_no_image": 0, "failed_image_download": 0,
        "duration_seconds": 0.1,
    }), encoding="utf-8")


def _vectors_for_similarities(similarities: list[float], dim: int = 2) -> np.ndarray:
    """Vectors on the unit circle (axes 0/1, zero elsewhere) with EXACT
    cosine similarity to the query [1,0,...,0] equal to each requested
    value -- lets tests assert precise threshold-filtering behaviour."""
    vectors = np.zeros((len(similarities), dim), dtype=np.float32)
    for i, sim in enumerate(similarities):
        sim = max(-1.0, min(1.0, sim))
        vectors[i, 0] = sim
        vectors[i, 1] = (1 - sim ** 2) ** 0.5
    return vectors


def _axis0_query(dim: int = 2) -> np.ndarray:
    vec = np.zeros((1, dim), dtype=np.float32)
    vec[0, 0] = 1.0
    return vec


def _make_product_with_local_image(db, image_dir, filename, *, sku, color=(10, 20, 30)):
    path = image_dir / filename
    path.write_bytes(jpeg_bytes(color))
    product = Product(internal_sku=sku, local_image_path=str(path))
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


# ==================== 1. model lazy load ====================

@requires_model
def test_model_lazy_load_is_process_wide_singleton(isolated_model_path):
    session1 = image_search._load_model()
    session2 = image_search._load_model()
    assert session1 is session2


def test_is_model_ready_reflects_file_presence(isolated_model_path):
    assert image_search.is_model_ready() == MODEL_AVAILABLE


# ==================== 2. index missing -> friendly error ====================

def test_index_files_exist_false_when_missing(isolated_index_paths):
    assert image_search.index_files_exist() is False


def test_get_loaded_index_raises_index_not_ready(isolated_index_paths):
    with pytest.raises(IndexNotReadyError):
        image_search._get_loaded_index()


# ==================== 3. malformed index -> friendly error ====================

def test_corrupt_index_files_raise_index_corrupt_error(isolated_index_paths):
    image_search.INDEX_PATH.write_bytes(b"not a real faiss index at all")
    image_search.MAP_PATH.write_text("[]", encoding="utf-8")
    image_search.META_PATH.write_text("{}", encoding="utf-8")
    with pytest.raises(IndexCorruptError):
        image_search._get_loaded_index()


def test_malformed_map_json_raises_index_corrupt_error(isolated_index_paths):
    import faiss
    idx = faiss.IndexFlatIP(4)
    idx.add(np.zeros((1, 4), dtype=np.float32))
    faiss.write_index(idx, str(image_search.INDEX_PATH))
    image_search.MAP_PATH.write_text("not json", encoding="utf-8")
    image_search.META_PATH.write_text("{}", encoding="utf-8")
    with pytest.raises(IndexCorruptError):
        image_search._get_loaded_index()


# ==================== 4-6. query image validation ====================

def test_validate_query_image_accepts_jpeg():
    image = image_search.validate_query_image(jpeg_bytes())
    assert image.size == (64, 64)


def test_validate_query_image_rejects_non_image_bytes():
    with pytest.raises(ImageSearchError):
        image_search.validate_query_image(b"this is not an image, just plain text bytes here")


def test_validate_query_image_rejects_empty():
    with pytest.raises(ImageSearchError):
        image_search.validate_query_image(b"")


def test_validate_query_image_rejects_over_10mb():
    oversized = b"\xff\xd8\xff" + b"0" * (image_search.MAX_QUERY_IMAGE_BYTES + 1)
    with pytest.raises(ImageSearchError):
        image_search.validate_query_image(oversized)


# ==================== 7-9. top_k defaults / clamping / ordering ====================

def test_search_similar_products_default_top_k(isolated_index_paths, monkeypatch):
    # all 15 comfortably above the 0.82 threshold so top_k's ceiling (not
    # the threshold) is what's under test here
    similarities = [0.99 - i * 0.005 for i in range(15)]
    vectors = _vectors_for_similarities(similarities)
    _write_fake_faiss_index(isolated_index_paths, vectors, list(range(1, 16)))
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)))
    assert len(hits) == TOP_K_DEFAULT


def test_search_similar_products_clamps_top_k_to_max(isolated_index_paths, monkeypatch):
    # top 25 above threshold, remaining 5 far below -- FAISS's own top_k=20
    # cap (clamped from 999) never even considers those low ones.
    similarities = [0.97 - i * 0.004 for i in range(25)] + [0.3] * 5
    vectors = _vectors_for_similarities(similarities)
    _write_fake_faiss_index(isolated_index_paths, vectors, list(range(1, 31)))
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=999)
    assert len(hits) == TOP_K_MAX


def test_search_similar_products_orders_by_similarity_descending(isolated_index_paths, monkeypatch):
    # all four above threshold, distinct order
    vectors = _vectors_for_similarities([0.99, 0.95, 0.90, 0.85])
    _write_fake_faiss_index(isolated_index_paths, vectors, [101, 102, 103, 104])
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    assert [hit.product_id for hit in hits] == [101, 102, 103, 104]
    sims = [hit.similarity for hit in hits]
    assert sims == sorted(sims, reverse=True)


# ==================== 10-11. index building: skip no-image / single failure ====================

@requires_model
def test_build_index_skips_products_with_no_image(client, isolated_image_dirs, isolated_index_paths, isolated_model_path):
    _http, db, _tmp = client
    _main_dir, qinsi_dir = isolated_image_dirs
    _make_product_with_local_image(db, qinsi_dir, "1.jpg", sku="IMGCORE-001")
    db.add(Product(internal_sku="IMGCORE-002"))  # no image at all
    db.commit()

    result = image_search.build_product_image_search_index(db.get_bind())
    assert result.total_products == 2
    assert result.indexed_products == 1
    assert result.skipped_no_image == 1
    assert result.failed_image_download == 0


@requires_model
def test_build_index_single_download_failure_does_not_abort_build(
    client, isolated_image_dirs, isolated_index_paths, isolated_model_path, monkeypatch,
):
    _http, db, _tmp = client
    _main_dir, qinsi_dir = isolated_image_dirs
    _make_product_with_local_image(db, qinsi_dir, "1.jpg", sku="IMGCORE-101")
    db.add(Product(internal_sku="IMGCORE-102", display_image_url="https://example.invalid/broken.jpg"))
    db.commit()

    def failing_download(url, *, client=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(image_search, "download_remote_image", failing_download)
    result = image_search.build_product_image_search_index(db.get_bind())
    assert result.indexed_products == 1
    assert result.failed_image_download == 1


# ==================== 12. index metadata saved ====================

@requires_model
def test_build_index_writes_metadata(client, isolated_image_dirs, isolated_index_paths, isolated_model_path):
    _http, db, _tmp = client
    _main_dir, qinsi_dir = isolated_image_dirs
    _make_product_with_local_image(db, qinsi_dir, "1.jpg", sku="IMGCORE-201")
    db.commit()

    image_search.build_product_image_search_index(db.get_bind())
    meta = image_search.read_index_meta()
    assert meta is not None
    assert meta["model_name"] == image_search.MODEL_NAME
    assert meta["product_count"] == 1
    assert meta["image_count"] == 1
    assert "built_at" in meta
    assert "embedding_dim" in meta


# ==================== 13. atomic replace (no leftover tmp files) ====================

@requires_model
def test_build_index_atomic_replace_leaves_no_tmp_files(client, isolated_image_dirs, isolated_index_paths, isolated_model_path):
    _http, db, _tmp = client
    _main_dir, qinsi_dir = isolated_image_dirs
    _make_product_with_local_image(db, qinsi_dir, "1.jpg", sku="IMGCORE-301")
    db.commit()

    image_search.build_product_image_search_index(db.get_bind())
    assert image_search.INDEX_PATH.is_file()
    leftover = list(image_search.IMAGE_SEARCH_DIR.glob(".*.tmp-*"))
    assert leftover == []


# ==================== 14. concurrent rebuild rejected ====================

def test_concurrent_rebuild_is_rejected(isolated_index_paths):
    assert image_search.try_reserve_build_slot() is True
    try:
        assert image_search.try_reserve_build_slot() is False
        assert image_search.is_build_in_progress() is True
    finally:
        image_search._release_build_slot()
    assert image_search.try_reserve_build_slot() is True
    image_search._release_build_slot()


# ==================== 15. build failure preserves old index ====================

@requires_model
def test_build_failure_preserves_existing_index(client, isolated_image_dirs, isolated_index_paths, isolated_model_path):
    _http, db, _tmp = client
    _main_dir, qinsi_dir = isolated_image_dirs
    good = _make_product_with_local_image(db, qinsi_dir, "1.jpg", sku="IMGCORE-401")
    image_search.build_product_image_search_index(db.get_bind())
    old_bytes = image_search.INDEX_PATH.read_bytes()
    old_meta = image_search.read_index_meta()

    good.local_image_path = None
    db.commit()
    with pytest.raises(RuntimeError):
        image_search.build_product_image_search_index(db.get_bind())

    assert image_search.INDEX_PATH.read_bytes() == old_bytes
    assert image_search.read_index_meta() == old_meta


# ==================== 16. bounded concurrency for fetch ====================

@requires_model
def test_build_index_fetch_concurrency_is_bounded(
    client, isolated_index_paths, isolated_model_path, monkeypatch,
):
    """Downloads must run concurrently (not one-at-a-time) but never exceed
    the configured worker cap -- even with more candidates than one fetch
    batch holds."""
    _http, db, _tmp = client
    item_count = image_search.BUILD_FETCH_BATCH_SIZE + 12  # spans two fetch batches
    for i in range(item_count):
        db.add(Product(internal_sku=f"IMGCONC-{i}", display_image_url=f"https://example.invalid/{i}.jpg"))
    db.commit()

    lock = threading.Lock()
    state = {"current": 0, "peak": 0, "calls": 0}

    def fake_fetch(item, http_client):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            state["calls"] += 1
        time.sleep(0.03)
        with lock:
            state["current"] -= 1
        return jpeg_bytes((5, 5, 5))

    monkeypatch.setattr(image_search, "_fetch_candidate_image_bytes", fake_fetch)
    result = image_search.build_product_image_search_index(db.get_bind())

    assert result.indexed_products == item_count
    assert state["calls"] == item_count
    max_workers = image_search._build_fetch_max_workers()
    assert 4 <= max_workers <= 8
    # Genuinely concurrent (more than one in flight at a time)...
    assert state["peak"] > 1
    # ...but never more than the configured cap, regardless of total candidates.
    assert state["peak"] <= max_workers


# ==================== 17. multiple concurrent failures don't abort the build ====================

@requires_model
def test_build_index_concurrent_mixed_failures_do_not_abort(
    client, isolated_index_paths, isolated_model_path, monkeypatch,
):
    _http, db, _tmp = client
    for i in range(10):
        db.add(Product(internal_sku=f"IMGCONCFAIL-{i}", display_image_url=f"https://example.invalid/{i}.jpg"))
    db.commit()

    def flaky_fetch(item, http_client):
        # every other item "fails" to download
        index = int(item["product_id"]) % 2
        if index == 0:
            raise RuntimeError("simulated concurrent network failure")
        return jpeg_bytes((7, 7, 7))

    monkeypatch.setattr(image_search, "_fetch_candidate_image_bytes", flaky_fetch)
    result = image_search.build_product_image_search_index(db.get_bind())
    assert result.indexed_products + result.failed_image_download == 10
    assert result.failed_image_download > 0
    assert result.indexed_products > 0


# ==================== similarity threshold filtering ====================

def test_min_similarity_threshold_default_is_082():
    assert image_search.min_similarity_threshold() == pytest.approx(0.82)


def test_min_similarity_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JBA_IMAGE_SEARCH_MIN_SIMILARITY", "0.9")
    assert image_search.min_similarity_threshold() == pytest.approx(0.9)


def test_min_similarity_threshold_ignores_invalid_env(monkeypatch):
    monkeypatch.setenv("JBA_IMAGE_SEARCH_MIN_SIMILARITY", "not-a-number")
    assert image_search.min_similarity_threshold() == pytest.approx(0.82)


def test_threshold_drops_candidates_below_082_keeps_above(isolated_index_paths, monkeypatch):
    """0.90, 0.88, 0.84, 0.80 with threshold=0.82 -> only the first three survive."""
    vectors = _vectors_for_similarities([0.90, 0.88, 0.84, 0.80])
    _write_fake_faiss_index(isolated_index_paths, vectors, [201, 202, 203, 204])
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    assert [hit.product_id for hit in hits] == [201, 202, 203]
    assert all(hit.similarity >= 0.82 for hit in hits)


def test_threshold_all_below_082_returns_empty(isolated_index_paths, monkeypatch):
    vectors = _vectors_for_similarities([0.81, 0.79, 0.70])
    _write_fake_faiss_index(isolated_index_paths, vectors, [301, 302, 303])
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    assert hits == []


def test_threshold_single_candidate_above_082_returned(isolated_index_paths, monkeypatch):
    vectors = _vectors_for_similarities([0.91])
    _write_fake_faiss_index(isolated_index_paths, vectors, [401])
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    assert len(hits) == 1
    assert hits[0].product_id == 401


def test_threshold_filtered_results_still_strictly_descending(isolated_index_paths, monkeypatch):
    vectors = _vectors_for_similarities([0.99, 0.90, 0.83, 0.60])
    _write_fake_faiss_index(isolated_index_paths, vectors, [501, 502, 503, 504])
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    sims = [hit.similarity for hit in hits]
    assert sims == sorted(sims, reverse=True)
    assert len(hits) == 3  # the 0.60 one is dropped


def test_threshold_top_k_ten_but_only_three_above_threshold(isolated_index_paths, monkeypatch):
    similarities = [0.90, 0.88, 0.84] + [0.5] * 7  # 10 candidates total, only 3 survive
    vectors = _vectors_for_similarities(similarities)
    _write_fake_faiss_index(isolated_index_paths, vectors, list(range(601, 611)))
    monkeypatch.setattr(image_search, "embed_images", lambda images: _axis0_query())
    hits = image_search.search_similar_products(Image.new("RGB", (8, 8)), top_k=10)
    assert len(hits) == 3
