from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import AiRecognitionRun, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem


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
