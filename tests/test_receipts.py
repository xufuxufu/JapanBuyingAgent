from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import AiRecognitionRun, Product, ProductBarcode, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem


def upload(client, content, name="receipt.jpg"):
    return client.post("/api/receipt-batches/upload", files={"files": (name, content, "image/jpeg")}, data={"source_type": "mobile"})


def test_single_upload_saves_original_preview_and_records(client, jpeg_bytes):
    http, db, root = client
    response = upload(http, jpeg_bytes)
    assert response.status_code == 201
    data = response.json()
    assert data["image_count"] == 1 and data["source_type"] == "mobile"
    image = db.scalar(select(ReceiptImage))
    assert image.file_size == len(jpeg_bytes)
    assert len(image.file_hash) == 64
    assert (root / image.original_path).read_bytes() == jpeg_bytes
    assert (root / image.processed_path).is_file()


def test_multiple_upload(client, jpeg_bytes):
    http, db, _ = client
    response = http.post("/api/receipt-batches/upload", files=[("files", ("a.jpg", jpeg_bytes, "image/jpeg")), ("files", ("b.jpg", jpeg_bytes, "image/jpeg"))])
    assert response.status_code == 201
    assert response.json()["image_count"] == 1 and response.json()["duplicate_count"] == 1
    assert [x.page_no for x in db.scalars(select(ReceiptImage).order_by(ReceiptImage.page_no))] == [1]


def test_non_image_rejected_without_batch(client):
    http, db, _ = client
    response = upload(http, b"not an image", "fake.jpg")
    assert response.status_code == 415
    assert db.scalar(select(func.count()).select_from(ReceiptBatch)) == 0


def test_path_traversal_is_not_used(client, jpeg_bytes):
    http, db, root = client
    assert upload(http, jpeg_bytes, "../../evil.jpg").status_code == 201
    image = db.scalar(select(ReceiptImage))
    assert image.original_filename == "evil.jpg"
    assert ".." not in image.original_path
    assert (root / image.original_path).resolve().is_relative_to(root.resolve())


def test_duplicate_names_do_not_overwrite(client, jpeg_bytes):
    http, db, root = client
    assert upload(http, jpeg_bytes, "same.jpg").status_code == 201
    duplicate = upload(http, jpeg_bytes, "same.jpg")
    assert duplicate.status_code == 201 and duplicate.json()["duplicate_only"] is True
    images = list(db.scalars(select(ReceiptImage).order_by(ReceiptImage.id)))
    assert len(images) == 1 and duplicate.json()["duplicate_count"] == 1
    assert all((root / image.original_path).is_file() for image in images)


def test_history_detail_and_health(client, jpeg_bytes):
    http, _, _ = client
    batch = upload(http, jpeg_bytes).json()
    assert http.get("/health").json()["database"] == "ok"
    assert http.get("/").status_code == 200
    assert http.get("/receipts/upload").status_code == 200
    assert batch["batch_no"][-4:] in http.get("/receipts").text
    assert http.get(f"/receipts/{batch['id']}").status_code == 200
    assert http.get(f"/api/receipt-batches/{batch['id']}").json()["images"][0]["preview_url"]


def test_valid_json_import_preserves_raw_and_leading_zero(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=valid_payload)
    assert response.status_code == 200
    item = db.scalar(select(ReceiptItem))
    run = db.scalar(select(AiRecognitionRun))
    assert item.jan_candidate == "0490123456789"
    assert json.loads(run.raw_response_json)["items"][0]["jan_candidate"] == "0490123456789"
    assert db.scalar(select(ReceiptBatch)).status == "review"


@pytest.mark.parametrize("mutator", [
    lambda p: p["items"][0].update(quantity=0),
    lambda p: p["items"][0].update(confidence=1.1),
])
def test_invalid_item_values_are_atomic(client, jpeg_bytes, valid_payload, mutator):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    mutator(valid_payload)
    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=valid_payload)
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0
    assert db.scalar(select(func.count()).select_from(AiRecognitionRun)) == 0


def test_malformed_json_is_atomic(client, jpeg_bytes):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", content=b"{bad", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0


def test_null_jan_is_legal(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    valid_payload["items"][0]["jan_candidate"] = None
    assert http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=valid_payload).status_code == 200
    assert db.scalar(select(ReceiptItem)).jan_candidate is None


def test_delete_unconfirmed_soft_deletes_and_preserves_original(client, jpeg_bytes):
    http, db, root = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    image = db.scalar(select(ReceiptImage))
    original = root / image.original_path
    response = http.delete(f"/api/receipt-batches/{batch_id}")
    assert response.status_code == 204
    assert original.exists()
    batch = db.get(ReceiptBatch, batch_id)
    assert batch.status == "deleted" and batch.current_stage == "deleted"
    assert f'/receipts/{batch_id}' not in http.get('/receipts').text


def test_delete_confirmed_is_blocked(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    assert http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=valid_payload).status_code == 200
    receipt = db.scalar(select(Receipt))
    receipt.confirmation_status = "confirmed"
    db.commit()
    assert http.delete(f"/api/receipt-batches/{batch_id}").status_code == 409
    assert db.get(ReceiptBatch, batch_id) is not None


# ---------------- single-receipt import: enrichment is backgrounded too ----------------
# Same fix as import_gpt_job_json(): import_recognition_json() must never wait
# on product enrichment after its own commit succeeds. Both call sites
# (recognition_page_post's form route and api_recognition's JSON route)
# schedule process_receipt_items_enrichment as a BackgroundTask instead.


def make_valid_jan(body12: str) -> str:
    digits = [int(c) for c in body12]
    weighted = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - weighted % 10) % 10
    return body12 + str(check)


def multi_item_payload(jans: list[str]) -> dict:
    items = [{
        "line_no": line_no, "raw_name": f"商品{line_no}", "recognized_name": "",
        "jan_candidate": jan, "quantity": 1, "unit_price": 100, "discount_amount": 0,
        "tax_rate": 0.1, "line_total": 100, "confidence": 0.9,
    } for line_no, jan in enumerate(jans, 1)]
    return {
        "schema_version": "1.0",
        "store": {"raw_name": "测试药妆店", "purchased_at": None},
        "totals": {
            "subtotal": sum(item["line_total"] for item in items), "discount_total": 0,
            "tax_total": 0, "paid_total": sum(item["line_total"] for item in items),
        },
        "items": items,
        "warnings": [],
    }


def test_import_recognition_json_returns_fast_even_with_many_new_jans(client, jpeg_bytes):
    import time
    import app.services as services

    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    jans = [make_valid_jan(f"4930{i:08d}") for i in range(30)]
    raw = json.dumps(multi_item_payload(jans), ensure_ascii=False)

    started = time.monotonic()
    receipt = services.import_recognition_json(db, batch, raw)
    elapsed = time.monotonic() - started

    assert len(receipt.items) == 30
    assert elapsed < 5, f"import_recognition_json took {elapsed:.2f}s -- enrichment must not run inline"
    from app.models import ProductEnrichmentTask
    assert db.scalar(select(func.count()).select_from(ProductEnrichmentTask)) == 0


def test_api_recognition_route_schedules_background_enrichment(client, jpeg_bytes, monkeypatch):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    jans = [make_valid_jan(f"4931{i:08d}") for i in range(5)]
    payload = multi_item_payload(jans)

    calls = []

    def fake_enrichment(database_url, receipt_item_ids, trigger_source):
        calls.append((receipt_item_ids, trigger_source))

    import app.main as main_module
    monkeypatch.setattr(main_module, "process_receipt_items_enrichment", fake_enrichment)

    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=payload)
    assert response.status_code == 200
    assert len(calls) == 1
    item_ids, trigger_source = calls[0]
    assert len(item_ids) == 5 and trigger_source == "gpt_receipt_json"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 5


def test_recognition_page_post_route_schedules_background_enrichment_and_redirects(client, jpeg_bytes, monkeypatch):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    jans = [make_valid_jan(f"4932{i:08d}") for i in range(5)]
    raw = json.dumps(multi_item_payload(jans), ensure_ascii=False)

    calls = []

    def fake_enrichment(database_url, receipt_item_ids, trigger_source):
        calls.append((receipt_item_ids, trigger_source))

    import app.main as main_module
    monkeypatch.setattr(main_module, "process_receipt_items_enrichment", fake_enrichment)

    response = http.post(f"/receipts/{batch_id}/recognition-json", data={"payload": raw}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/receipts/{batch_id}/review"
    assert len(calls) == 1
    item_ids, trigger_source = calls[0]
    assert len(item_ids) == 5 and trigger_source == "gpt_receipt_json"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 5


def test_api_recognition_route_survives_enrichment_exception(client, jpeg_bytes, monkeypatch):
    # Same reasoning as the gpt-jobs regression test: break the real
    # process_receipt_items_enrichment's one internal call rather than
    # replacing the entry point itself, since Starlette's BackgroundTask
    # does not catch exceptions on its own -- the isolation must come from
    # process_receipt_items_enrichment's own try/except.
    import app.product_enrichment as enrichment

    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    jans = [make_valid_jan(f"4933{i:08d}") for i in range(3)]
    payload = multi_item_payload(jans)

    def failing_safe_trigger(session, items, trigger_source):
        raise RuntimeError("simulated enrichment crash")

    monkeypatch.setattr(enrichment, "safe_trigger_receipt_items", failing_safe_trigger)

    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=payload)
    assert response.status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 3


# ---------------- #11 regression: automatic import-time matching must agree with manual save ----------------
# Previously, receipt-item identity matching only ran once review_status became "confirmed"
# (at manual save or final confirm), so a freshly imported row whose JAN exactly matched an
# existing, fully complete product still showed "unmatched" until the user saved the row.
# preview_match_item()/sync_pending_item_previews() now run the same matcher immediately at
# import time and on every review-page load, without requiring confirmation first.


def test_existing_product_jan_is_matched_immediately_after_import_without_saving(client, jpeg_bytes):
    import app.services as services

    http, db, _ = client
    jan = "4901008613369"
    product = Product(
        jan=jan, name_cn="现有商品", name_ja="既存商品", status="active",
        main_image_path="local/existing.jpg", brand="品牌", capacity="100ml", purchase_price=500,
    )
    db.add(product)
    db.commit()

    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([jan])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))

    db.refresh(receipt)
    item = receipt.items[0]
    assert item.review_status == "pending"
    assert item.match_status == "matched_existing"
    assert item.product_id == product.id
    assert item.match_method == "jan_exact"


def test_manual_save_agrees_with_automatic_import_preview_for_existing_jan(client, jpeg_bytes):
    import app.services as services

    http, db, _ = client
    jan = "4901008613369"
    product = Product(jan=jan, name_cn="现有商品", status="active")
    db.add(product)
    db.commit()

    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([jan])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))
    item = receipt.items[0]
    assert item.match_status == "matched_existing"

    response = http.post(
        f"/receipts/{batch.id}/review/items/{item.id}",
        data={
            "raw_name": item.raw_name, "recognized_name": item.recognized_name or "",
            "jan_candidate": jan, "quantity": item.quantity, "unit_price": item.unit_price,
            "discount_amount": item.discount_amount, "tax_rate": item.tax_rate,
            "line_total": item.line_total, "confidence": item.confidence,
            "review_status": item.review_status,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.refresh(item)
    assert item.match_status == "matched_existing"
    assert item.product_id == product.id


def test_import_preview_reports_new_product_and_conflict_without_confirming(client, jpeg_bytes):
    import app.services as services

    http, db, _ = client
    shared = "4901234567894"
    product_a = Product(name_cn="冲突甲")
    product_b = Product(name_cn="冲突乙")
    db.add_all([product_a, product_b])
    db.flush()
    db.add_all([
        ProductBarcode(product_id=product_a.id, barcode=shared, source_system="qinsi", is_primary=True),
        ProductBarcode(product_id=product_b.id, barcode=shared, source_system="qinsi", is_primary=True),
    ])
    db.commit()

    not_found_jan = "4570110290418"
    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([shared, not_found_jan])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))

    db.refresh(receipt)
    conflict_item, new_item = receipt.items
    assert conflict_item.match_status == "conflict"
    assert conflict_item.product_id is None
    assert new_item.match_status == "new_product"
    assert new_item.product_id is None


def test_import_preview_never_matches_a_qinsi_product_code_as_jan(client, jpeg_bytes):
    # Regression: a receipt JAN candidate must never be compared against
    # Product.qinsi_product_code -- QinSi 货号 is a different identity space
    # (see BUSINESS_RULES.md) and must never automatically be treated as JAN.
    import app.services as services

    http, db, _ = client
    jan_lookalike = "4901008613369"
    product = Product(name_cn="秦丝货号商品", qinsi_product_code=jan_lookalike)
    db.add(product)
    db.commit()

    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([jan_lookalike])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))

    db.refresh(receipt)
    item = receipt.items[0]
    assert item.match_status == "new_product"
    assert item.product_id is None


def test_import_preview_normalizes_whitespace_around_jan(client, jpeg_bytes):
    import app.services as services

    http, db, _ = client
    jan = make_valid_jan("049012345678")
    product = Product(jan=jan, name_cn="前导零商品")
    db.add(product)
    db.commit()

    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([f"  {jan}  "])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))

    db.refresh(receipt)
    item = receipt.items[0]
    assert item.jan_candidate.strip() == jan
    assert item.match_status == "matched_existing"
    assert item.product_id == product.id


def test_review_page_self_heals_a_stale_unmatched_preview(client, jpeg_bytes):
    # Belt-and-suspenders: even if a row's preview is stale (e.g. the matching product was
    # imported after this row), loading the review page recomputes it via
    # sync_pending_item_previews() without requiring a save or confirm.
    import app.services as services

    http, db, _ = client
    jan = "4901008613369"
    batch_id = upload(http, jpeg_bytes).json()["id"]
    batch = db.get(ReceiptBatch, batch_id)
    payload = multi_item_payload([jan])
    receipt = services.import_recognition_json(db, batch, json.dumps(payload, ensure_ascii=False))
    item = receipt.items[0]
    assert item.match_status == "new_product"

    product = Product(jan=jan, name_cn="后到的商品", status="active")
    db.add(product)
    db.commit()

    response = http.get(f"/receipts/{batch.id}/review?receipt_id={receipt.id}")
    assert response.status_code == 200
    db.refresh(item)
    assert item.match_status == "matched_existing"
    assert item.product_id == product.id
    assert "匹配依据：JAN精确匹配" in response.text
