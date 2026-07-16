from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image
from sqlalchemy import func, select

import app.image_processing as processing
import app.services as services
from app.models import AiRecognitionRun, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem


def upload(http, content, name="receipt.jpg"):
    return http.post("/api/receipt-batches/upload", files={"files": (name, content, "image/jpeg")}, data={"source_type": "mobile"})


def test_mobile_upload_page_has_preview_remove_and_duplicate_guard(client):
    http, _, _ = client
    text = http.get("/receipts/upload").text
    assert "直接拍照" in text and "从相册多选" in text
    assert "uploadPreviews" in text and "selectedFiles.splice" in text
    assert "isUploading" in text and "submit.disabled" in text


def test_exif_orientation_is_corrected(tmp_path):
    source, destination = tmp_path / "oriented.jpg", tmp_path / "processed.jpg"
    image = Image.new("RGB", (40, 80), "white")
    exif = Image.Exif(); exif[274] = 6
    image.save(source, exif=exif)
    result = processing.prepare_receipt_image(source, destination)
    with Image.open(destination) as output:
        assert output.width == 80 and output.height == 40
    assert "exif" in result.method


def test_auto_crop_succeeds(tmp_path):
    source, destination = tmp_path / "receipt.jpg", tmp_path / "processed.jpg"
    image = Image.new("RGB", (600, 800), (60, 60, 60))
    for x in range(100, 500):
        for y in range(60, 750):
            image.putpixel((x, y), (245, 245, 240))
    image.save(source)
    result = processing.prepare_receipt_image(source, destination)
    assert "auto_crop" in result.method
    assert result.width < 600


def test_missing_boundary_safe_fallback(tmp_path, monkeypatch):
    source, destination = tmp_path / "plain.jpg", tmp_path / "processed.jpg"
    Image.new("RGB", (200, 500), "white").save(source)
    monkeypatch.setattr(processing, "detect_receipt_bbox", lambda _image: None)
    result = processing.prepare_receipt_image(source, destination)
    assert "safe_fallback" in result.method and result.warning
    assert destination.is_file()


def test_preprocessing_failure_keeps_original(client, jpeg_bytes, monkeypatch):
    http, db, root = client
    monkeypatch.setattr(services, "prepare_receipt_image", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    response = upload(http, jpeg_bytes)
    assert response.status_code == 201
    image = db.scalar(select(ReceiptImage))
    assert (root / image.original_path).read_bytes() == jpeg_bytes
    assert image.preprocessing_status == "fallback"
    assert (root / image.processed_path).is_file()


def test_recognition_image_and_zip_download(client, jpeg_bytes):
    http, _, _ = client
    first = upload(http, jpeg_bytes, "a.jpg").json()
    image = first["images"][0]
    single = http.get(f"/receipts/{first['id']}/images/{image['id']}/download")
    assert single.status_code == 200 and single.content.startswith(b"\xff\xd8")
    archive = http.get(f"/receipts/{first['id']}/recognition-images.zip")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == [image["recognition_filename"]]
        assert zipped.read(zipped.namelist()[0]).startswith(b"\xff\xd8")


def test_prompt_and_json_preview(client, jpeg_bytes, valid_payload):
    http, _, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    prompt = http.get("/receipts/gpt-prompt")
    assert prompt.status_code == 200 and "不得猜测" in prompt.text and "schema_version" in prompt.text
    preview = http.post(f"/receipts/{batch_id}/recognition-preview", data={"payload": json.dumps(valid_payload, ensure_ascii=False)})
    assert preview.status_code == 200 and "JSON 导入预览" in preview.text and "测试药妆店" in preview.text


def test_repeat_import_keeps_all_raw_runs(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    batch_id = upload(http, jpeg_bytes).json()["id"]
    first_raw = json.dumps(valid_payload, ensure_ascii=False)
    assert http.post(f"/api/receipt-batches/{batch_id}/recognition-json", content=first_raw).status_code == 200
    valid_payload["store"]["raw_name"] = "第二次识别"
    second_raw = json.dumps(valid_payload, ensure_ascii=False)
    assert http.post(f"/api/receipt-batches/{batch_id}/recognition-json", content=second_raw).status_code == 200
    runs = list(db.scalars(select(AiRecognitionRun).order_by(AiRecognitionRun.id)))
    assert len(runs) == 2
    assert [run.raw_response_json for run in runs] == [first_raw, second_raw]
    assert db.scalar(select(func.count()).select_from(Receipt)) == 1


def imported_batch(http, jpeg_bytes, valid_payload):
    batch_id = upload(http, jpeg_bytes).json()["id"]
    response = http.post(f"/api/receipt-batches/{batch_id}/recognition-json", json=valid_payload)
    assert response.status_code == 200
    return batch_id


def test_review_edit_add_ignore_delete_and_invalid_quantity(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    batch_id = imported_batch(http, jpeg_bytes, valid_payload)
    item = db.scalar(select(ReceiptItem))
    edit = {"raw_name":"原名","recognized_name":"整理名","jan_candidate":"0012345678901","quantity":"2","unit_price":"100","discount_amount":"0","tax_rate":"0.1","line_total":"200","confidence":"0.8","review_status":"reviewed"}
    assert http.post(f"/receipts/{batch_id}/review/items/{item.id}", data=edit).status_code == 200
    db.refresh(item); assert item.quantity == 2 and item.jan_candidate == "0012345678901"
    invalid = dict(edit, quantity="0")
    assert http.post(f"/receipts/{batch_id}/review/items/{item.id}", data=invalid, follow_redirects=False).status_code == 422
    add = dict(edit, raw_name="新增", jan_candidate="", quantity="1")
    assert http.post(f"/receipts/{batch_id}/review/items", data=add).status_code == 200
    items = list(db.scalars(select(ReceiptItem).order_by(ReceiptItem.id)))
    assert len(items) == 2
    assert http.post(f"/receipts/{batch_id}/review/items/{items[1].id}/ignore").status_code == 200
    db.refresh(items[1]); assert items[1].review_status == "ignored"
    assert http.post(f"/receipts/{batch_id}/review/items/{items[1].id}/delete").status_code == 200
    assert db.get(ReceiptItem, items[1].id) is None


def test_amount_warning_draft_and_final_confirmation(client, jpeg_bytes, valid_payload):
    http, db, _ = client
    valid_payload["totals"]["paid_total"] = 999
    batch_id = imported_batch(http, jpeg_bytes, valid_payload)
    review = http.get(f"/receipts/{batch_id}/review")
    assert "金额校验警告" in review.text
    receipt = db.scalar(select(Receipt)); batch = db.get(ReceiptBatch, batch_id)
    assert receipt.confirmation_status == "pending" and batch.status == "review"
    assert http.post(f"/receipts/{batch_id}/review/confirm").status_code == 200
    db.refresh(receipt); db.refresh(batch)
    assert receipt.confirmation_status == "confirmed" and batch.status == "confirmed"
    assert "不一致" in receipt.confirmation_warning
    assert http.delete(f"/api/receipt-batches/{batch_id}").status_code == 409


def test_rotate_reprocess_and_source_selection(client, jpeg_bytes):
    http, db, _ = client
    batch = upload(http, jpeg_bytes).json(); image_id = batch["images"][0]["id"]
    assert http.post(f"/receipts/{batch['id']}/images/{image_id}/rotate").status_code == 200
    image = db.get(ReceiptImage, image_id); assert image.rotation_degrees == 90
    assert http.post(f"/receipts/{batch['id']}/images/{image_id}/source", data={"source":"original"}).status_code == 200
    db.refresh(image); assert image.recognition_source == "original"
    assert http.post(f"/receipts/{batch['id']}/images/{image_id}/reprocess").status_code == 200
