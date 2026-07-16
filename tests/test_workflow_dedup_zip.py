from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import re
import zipfile

from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.models import AiRecognitionRun, DuplicateDetectionLog, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem, ZipPackageJob, ZipPackageItem
from app.services import is_receipt_export_eligible


def picture(seed: int = 1, *, quality: int = 95, exif=None) -> bytes:
    image = Image.new("RGB", (180, 260), (238, 236, 229))
    draw = ImageDraw.Draw(image)
    draw.rectangle((18, 12, 162, 248), fill=(250, 250, 247), outline=(35 + seed, 35, 35), width=3)
    for index in range(7):
        y = 35 + index * 25
        draw.line((32, y, 145 - (index * seed) % 30, y), fill=(20 + seed * 3, 20, 20), width=2)
    draw.text((35, 220), f"TOTAL {1000 + seed}", fill=(0, 0, 0))
    output = io.BytesIO(); options = {"quality": quality}
    if exif is not None:
        options["exif"] = exif
    image.save(output, "JPEG", **options); return output.getvalue()


def upload(http, content: bytes, name: str, request_id: str):
    return http.post("/api/receipt-batches/upload", files={"files": (name, content, "image/jpeg")}, data={"source_type": "mobile", "request_id": request_id})


def sourced_payload(base: dict, image: ReceiptImage, *, store="测试店", number=None, purchased="2026-07-15T02:30:00+09:00", paid=1200, item_name="商品A", quantity=1):
    payload = deepcopy(base)
    payload.update(schema_version="1.1", source_file=image.recognition_filename, source_page_no=image.page_no)
    payload["store"] = {"raw_name": store, "purchased_at": purchased, "receipt_number": number}
    payload["totals"].update(subtotal=paid, paid_total=paid)
    payload["items"][0].update(source_file=image.recognition_filename, source_page_no=image.page_no, raw_name=item_name, quantity=quantity, line_total=paid)
    return payload


def create_batch(http, db, seed: int, request_id: str):
    data = upload(http, picture(seed), f"receipt-{seed}.jpg", request_id).json()
    return data, db.get(ReceiptImage, data["images"][0]["id"])


def test_upload_page_resets_completed_state_and_is_mobile_compact(client):
    text = client[0].get("/receipts/upload").text
    assert "function resetUploadSession" in text and "pollToken++" in text and "activeRequestId=null" in text
    assert "camera.addEventListener('change',event=>addFiles" in text and "gallery.addEventListener('change',event=>addFiles" in text
    assert "continueButton.addEventListener" in text and "resetUploadSession(true)" in text
    assert "正在处理 ${state.processed_images+state.failed_images} / ${state.total_images}" in text
    assert "✓ 已完成${state.total_images}张" in text and "phase complete" not in text
    assert "failureBlock.hidden=failures.length===0" in text and "最近完成：" in text
    css = client[0].get("/static/app.css").text
    assert "word-break:keep-all" in css and "min-height:44px" in css


def test_history_multiselect_controls_today_pending_and_reidentify_warning(client):
    http, db, _ = client
    create_batch(http, db, 1, "history-select-0001")
    text = http.get("/receipts").text
    for expected in ("batch-checkbox", "全选今天", "选择待识别", "取消选择", "下载识别ZIP", "bulk-bar"):
        assert expected in text
    assert "data-today=\"true\"" in text and "data-pending=\"true\"" in text
    assert "将重新识别" in text and "已选择${current.length}个批次" in text


def test_multibatch_zip_order_names_and_download_audit(client):
    http, db, _ = client
    first, first_image = create_batch(http, db, 2, "zip-batch-0001")
    second, second_image = create_batch(http, db, 3, "zip-batch-0002")
    url = f"/receipts/recognition-images.zip?batch_ids={second['id']}&batch_ids={first['id']}"
    one = http.get(url); two = http.get(url)
    assert one.status_code == two.status_code == 200
    assert re.search(r"GPT%E8%AF%86%E5%88%AB_\d{8}_2%E6%89%B9%E6%AC%A1_2%E5%BC%A0\.zip", one.headers["content-disposition"])
    with zipfile.ZipFile(io.BytesIO(one.content)) as archive:
        assert archive.namelist() == [first_image.recognition_filename, second_image.recognition_filename]
        assert all("receipt-" not in name for name in archive.namelist())
    job = db.scalar(select(ZipPackageJob))
    assert job.batch_count == 2 and job.image_count == 2 and job.download_count == 2
    assert job.first_downloaded_at and job.last_downloaded_at
    assert db.scalar(select(func.count()).select_from(ZipPackageItem)) == 2
    assert all(batch.zip_download_count == 2 and batch.gpt_status == "zip_downloaded" for batch in db.scalars(select(ReceiptBatch)))


def test_gpt_mark_import_and_review_timestamps(client, valid_payload):
    http, db, _ = client
    batch, image = create_batch(http, db, 4, "gpt-flow-0001")
    marked = http.post(f"/receipts/{batch['id']}/gpt-sent", headers={"accept": "application/json"})
    assert marked.json()["gpt_status"] == "sent_to_gpt"
    payload = sourced_payload(valid_payload, image, number="A-100")
    assert http.post(f"/api/receipt-batches/{batch['id']}/recognition-json", json=payload).status_code == 200
    current = db.get(ReceiptBatch, batch["id"])
    assert current.gpt_status == "json_imported" and current.gpt_sent_at and current.json_imported_at
    assert http.post(f"/receipts/{batch['id']}/review/confirm").status_code == 200
    db.refresh(current)
    assert current.gpt_status == "reviewed" and current.reviewed_at


def test_exact_rename_exif_and_light_recompression_are_auto_skipped(client):
    http, db, root = client
    original = picture(5, quality=96)
    first = upload(http, original, "first.jpg", "image-dedup-0001").json()
    renamed = upload(http, original, "renamed.jpg", "image-dedup-0002").json()
    assert renamed["duplicate_only"] and renamed["duplicates"][0]["matched_batch_id"] == first["id"]

    base = Image.open(io.BytesIO(original)).convert("RGB")
    rotated = base.rotate(90, expand=True)
    exif = Image.Exif(); exif[274] = 6
    rotated_bytes = io.BytesIO(); rotated.save(rotated_bytes, "JPEG", quality=96, exif=exif)
    exif_result = upload(http, rotated_bytes.getvalue(), "rotated.jpg", "image-dedup-0003").json()
    assert exif_result["duplicate_only"] is True

    recompressed = io.BytesIO(); base.save(recompressed, "JPEG", quality=82)
    compressed_result = upload(http, recompressed.getvalue(), "wechat.jpg", "image-dedup-0004").json()
    assert compressed_result["duplicate_only"] is True
    assert db.scalar(select(func.count()).select_from(ReceiptImage)) == 1
    assert len(list((root / "uploads" / "original").iterdir())) == 1
    assert db.scalar(select(func.count()).select_from(DuplicateDetectionLog).where(DuplicateDetectionLog.entity_type == "image")) == 3


def test_similar_but_distinct_images_and_repeat_purchases_remain_atomic(client, valid_payload):
    http, db, _ = client
    first, first_image = create_batch(http, db, 6, "distinct-buy-0001")
    second, second_image = create_batch(http, db, 13, "distinct-buy-0002")
    first_payload = sourced_payload(valid_payload, first_image, purchased="2026-07-15T10:00:00+09:00", paid=1500, item_name="商品A")
    second_payload = sourced_payload(valid_payload, second_image, purchased="2026-07-15T12:00:00+09:00", paid=1500, item_name="商品A")
    assert http.post(f"/api/receipt-batches/{first['id']}/recognition-json", json=first_payload).status_code == 200
    assert http.post(f"/api/receipt-batches/{second['id']}/recognition-json", json=second_payload).status_code == 200
    receipts = list(db.scalars(select(Receipt).order_by(Receipt.id)))
    assert len(receipts) == 2 and receipts[1].duplicate_status == "distinct"
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 2


def test_receipt_number_and_complete_business_facts_auto_duplicate_without_deleting_evidence(client, valid_payload):
    http, db, root = client
    first, first_image = create_batch(http, db, 7, "receipt-number-0001")
    second, second_image = create_batch(http, db, 15, "receipt-number-0002")
    assert http.post(f"/api/receipt-batches/{first['id']}/recognition-json", json=sourced_payload(valid_payload, first_image, number="X-7788")).status_code == 200
    assert http.post(f"/api/receipt-batches/{second['id']}/recognition-json", json=sourced_payload(valid_payload, second_image, number="X-7788")).status_code == 200
    receipts = list(db.scalars(select(Receipt).order_by(Receipt.id)))
    duplicate = next(receipt for receipt in receipts if receipt.duplicate_status == "auto_duplicate")
    assert duplicate.duplicate_of_receipt_id and not is_receipt_export_eligible(duplicate)
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 2
    assert db.scalar(select(func.count()).select_from(AiRecognitionRun)) == 2
    assert len(list((root / "uploads" / "original").iterdir())) == 2


def test_business_identical_auto_duplicate_master_priority_and_zip_exclusion(client, valid_payload):
    http, db, _ = client
    first, first_image = create_batch(http, db, 8, "master-rule-0001")
    assert http.post(f"/api/receipt-batches/{first['id']}/recognition-json", json=sourced_payload(valid_payload, first_image)).status_code == 200
    assert http.post(f"/receipts/{first['id']}/review/confirm").status_code == 200
    second, second_image = create_batch(http, db, 18, "master-rule-0002")
    assert http.post(f"/api/receipt-batches/{second['id']}/recognition-json", json=sourced_payload(valid_payload, second_image)).status_code == 200
    master = db.scalar(select(Receipt).where(Receipt.batch_id == first["id"]))
    duplicate = db.scalar(select(Receipt).where(Receipt.batch_id == second["id"]))
    assert duplicate.duplicate_status == "auto_duplicate" and duplicate.duplicate_of_receipt_id == master.id
    archive = http.get(f"/receipts/recognition-images.zip?batch_ids={first['id']}&batch_ids={second['id']}")
    assert archive.status_code == 200 and archive.headers["x-excluded-duplicates"] == "1" and archive.headers["x-zip-images"] == "1"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == [first_image.recognition_filename]
    detail = http.get(f"/receipts/{second['id']}").text
    assert "查看主记录" in detail


def test_conflicting_high_similarity_enters_review_required(client, valid_payload):
    http, db, _ = client
    first, first_image = create_batch(http, db, 9, "review-conflict-0001")
    second, second_image = create_batch(http, db, 10, "review-conflict-0002")
    first_payload = sourced_payload(valid_payload, first_image, store="店A", paid=1000, item_name="商品A")
    second_payload = sourced_payload(valid_payload, second_image, store="店B", paid=2200, item_name="商品B")
    http.post(f"/api/receipt-batches/{first['id']}/recognition-json", json=first_payload)
    http.post(f"/api/receipt-batches/{second['id']}/recognition-json", json=second_payload)
    receipt = db.scalar(select(Receipt).where(Receipt.batch_id == second["id"]))
    assert receipt.duplicate_status == "review_required"
    assert "冲突" in receipt.duplicate_reason
