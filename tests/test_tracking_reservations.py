from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import io
import re

from PIL import Image, ImageDraw
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.image_processing import prepare_receipt_image
from app.main import tokyo_datetime
from app.models import (
    Marketplace, PriceSearchRun, Product, ProductOffer, QinsiExportJob,
    QinsiExportLine, QinsiExportLineSource, ReceiptImage, ReceiptItem,
)
from app.services import new_batch_no


def image_bytes(size=(80, 120), receipt=False) -> bytes:
    image = Image.new("RGB", size, (55, 55, 55) if receipt else "white")
    if receipt:
        ImageDraw.Draw(image).rectangle((size[0] // 6, size[1] // 12, size[0] * 5 // 6, size[1] * 11 // 12), fill=(245, 245, 240))
    output = io.BytesIO(); image.save(output, "JPEG", quality=95)
    return output.getvalue()


def upload(http, content=None, name="receipt.jpg"):
    return http.post("/api/receipt-batches/upload", files={"files": (name, content or image_bytes(), "image/jpeg")}, data={"source_type": "mobile"})


def sourced_payload(valid_payload, image: ReceiptImage):
    payload = deepcopy(valid_payload)
    payload.update(schema_version="1.1", source_file=image.recognition_filename, source_page_no=image.page_no)
    payload["items"][0].update(source_file=image.recognition_filename, source_page_no=image.page_no)
    return payload


def test_upload_progress_and_duplicate_submit_guard_are_present(client):
    text = client[0].get("/receipts/upload").text
    assert "XMLHttpRequest" in text and "xhr.upload.addEventListener('progress'" in text
    assert "progressPercent" in text and "当前文件" in text and "正在处理图片" in text
    assert "if (isUploading || !selectedFiles.length) return" in text


def test_high_resolution_image_is_not_resized_by_default(tmp_path, monkeypatch):
    # 2000x2600 (5.2MP) is deliberately smaller than a real phone photo (this
    # app's own receipts run closer to 3000x4000) but still comfortably
    # exceeds every internal working-copy size prepare_receipt_image touches
    # (detect_receipt_bbox thumbnails to <=1200x1800, lightly_correct_
    # perspective to <=1000x1600) and stays far below the 7000px oversize
    # threshold this test is asserting against -- so it's still a real
    # exercise of "large image, no resize" while using a fraction of the
    # peak encode memory a full 3000x4000 (12MP) source needs during the
    # final quality=95/subsampling=0/optimize=True JPEG save below. That
    # save previously made this test flaky under full-suite memory pressure
    # (a real, reproducible libjpeg "Insufficient memory (case 4)" encoder
    # error -- not a decode bug and not a bug in prepare_receipt_image
    # itself, which is unchanged here) whenever peak resident memory from
    # earlier tests in the same run left too little headroom for a second
    # 4:4:4/optimize pass over 36MB of raw pixel data.
    source, destination = tmp_path / "large.jpg", tmp_path / "out.jpg"
    Image.new("RGB", (2000, 2600), "white").save(source, "JPEG", quality=90)
    monkeypatch.setattr("app.image_processing.detect_receipt_bbox", lambda _image: None)
    result = prepare_receipt_image(source, destination)
    assert (result.width, result.height) == (2000, 2600)
    assert "oversize_resize" not in result.method and "light_enhance" not in result.method


def test_boundary_failure_defaults_to_original_and_confident_crop_to_processed(client):
    http, db, _ = client
    first = upload(http, image_bytes()).json()
    first_image = db.get(ReceiptImage, first["images"][0]["id"])
    assert first_image.recognition_source == "original"
    second = upload(http, image_bytes((600, 800), receipt=True), "crop.jpg").json()
    second_image = db.get(ReceiptImage, second["images"][0]["id"])
    assert "auto_crop" in second_image.processing_method and second_image.recognition_source == "processed"


def test_first_rotation_is_visible_and_selects_processed(client):
    http, db, _ = client
    batch = upload(http).json(); image_id = batch["images"][0]["id"]
    response = http.post(f"/receipts/{batch['id']}/images/{image_id}/rotate", headers={"accept": "application/json"})
    assert response.status_code == 200 and response.json()["message"] == "已旋转90°"
    image = db.get(ReceiptImage, image_id)
    assert image.rotation_degrees == 90 and image.recognition_source == "processed"
    assert "?v=90" in response.json()["image"]["processed_url"]


def test_image_viewer_controls_are_rendered(client):
    http, _, _ = client
    batch = upload(http).json(); text = http.get(f"/receipts/{batch['id']}").text
    assert "imageViewer" in text and "viewerClose" in text and "data-viewer-type=\"original\"" in text
    assert "pointerdown" in text and "wheel" in text and "event.key === 'Escape'" in text
    assert "target=\"_blank\"" not in text


def test_tokyo_time_batch_and_recognition_filename_formats(client):
    assert tokyo_datetime(datetime(2026, 7, 14, 15, 28, tzinfo=timezone.utc)) == "2026年7月15日 00:28"
    batch_no = new_batch_no(datetime(2026, 7, 14, 15, 28, tzinfo=timezone.utc))
    assert re.fullmatch(r"RCPT-20260715-0028-[A-Z0-9]{4}", batch_no)
    data = upload(client[0]).json()
    assert re.fullmatch(r"RCPT-\d{8}-\d{4}-[A-Z0-9]{4}_P01\.jpg", data["images"][0]["recognition_filename"])


def test_source_file_maps_only_within_its_batch_and_preserves_duplicate_names(client, valid_payload):
    http, db, _ = client
    batch1 = upload(http, name="same.jpg").json(); image1 = db.get(ReceiptImage, batch1["images"][0]["id"])
    batch2 = upload(http, image_bytes(receipt=True), name="same.jpg").json(); image2 = db.get(ReceiptImage, batch2["images"][0]["id"])
    assert http.post(f"/api/receipt-batches/{batch1['id']}/recognition-json", json=sourced_payload(valid_payload, image1)).status_code == 200
    wrong = sourced_payload(valid_payload, image1)
    assert http.post(f"/api/receipt-batches/{batch2['id']}/recognition-json", json=wrong).status_code == 422
    assert http.post(f"/api/receipt-batches/{batch2['id']}/recognition-json", json=sourced_payload(valid_payload, image2)).status_code == 200
    items = list(db.scalars(select(ReceiptItem).order_by(ReceiptItem.id)))
    assert [item.source_image_id for item in items] == [image1.id, image2.id]


def test_same_product_receipt_items_stay_atomic_and_qinsi_sources_are_many_to_one(client, valid_payload):
    http, db, _ = client
    images = []
    for index, name in enumerate(("a.jpg", "b.jpg")):
        batch = upload(http, image_bytes((80 + index * 10, 120), receipt=bool(index)), name=name).json(); image = db.get(ReceiptImage, batch["images"][0]["id"]); images.append(image)
        assert http.post(f"/api/receipt-batches/{batch['id']}/recognition-json", json=sourced_payload(valid_payload, image)).status_code == 200
    product = Product(jan="0490123456789", name_cn="同一商品")
    db.add(product); db.flush()
    items = list(db.scalars(select(ReceiptItem).order_by(ReceiptItem.id)))
    for item in items: item.product_id = product.id
    job = QinsiExportJob(status="pending"); db.add(job); db.flush()
    line = QinsiExportLine(job_id=job.id, product_id=product.id, quantity=2, status="pending"); db.add(line); db.flush()
    db.add_all([QinsiExportLineSource(export_line_id=line.id, receipt_item_id=item.id, quantity=1) for item in items]); db.commit()
    assert db.scalar(select(func.count()).select_from(ReceiptItem).where(ReceiptItem.product_id == product.id)) == 2
    assert db.scalar(select(func.count()).select_from(QinsiExportLineSource).where(QinsiExportLineSource.export_line_id == line.id)) == 2
    job2 = QinsiExportJob(status="pending"); db.add(job2); db.flush()
    line2 = QinsiExportLine(job_id=job2.id, product_id=product.id, quantity=1, status="pending"); db.add(line2); db.flush()
    db.add(QinsiExportLineSource(export_line_id=line2.id, receipt_item_id=items[0].id, quantity=1))
    with pytest.raises(IntegrityError): db.commit()
    db.rollback()


def test_price_offer_snapshot_uses_integer_yen_and_does_not_replace_purchase_price(db_session):
    product = Product(jan="0012345678901", name_cn="价格商品", purchase_price=880)
    market = Marketplace(code="test-market", name="测试商城")
    db_session.add_all([product, market]); db_session.flush()
    run = PriceSearchRun(product_id=product.id, jan=product.jan, status="completed"); db_session.add(run); db_session.flush()
    offer = ProductOffer(search_run_id=run.id, marketplace_id=market.id, product_id=product.id, jan=product.jan, url="https://example.invalid/item", item_price=1000, shipping_price=300, total_price=1300, stock_status="in_stock", listing_type="single", condition="new", match_status="matched", fetched_at=datetime.now(timezone.utc), raw_data_json='{"source":"fixture"}')
    db_session.add(offer); db_session.commit(); db_session.refresh(product)
    assert offer.total_price == offer.item_price + offer.shipping_price == 1300
    assert product.purchase_price == 880
