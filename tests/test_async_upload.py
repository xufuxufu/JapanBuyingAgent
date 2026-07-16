from __future__ import annotations

import io

from PIL import Image, ImageDraw
from sqlalchemy import func, select

import app.main as main_module
import app.services as services
from app.models import ReceiptBatch, ReceiptImage


SAFARI_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Version/18.5 Mobile/15E148 Safari/604.1"


def jpeg_bytes(color=(30, 120, 90), marker: int = 0) -> bytes:
    output = io.BytesIO(); image = Image.new("RGB", (120, 180), color)
    if marker:
        marker = (marker - 1) % 8 + 1
        draw = ImageDraw.Draw(image); draw.rectangle((8 + marker * 5, 15 + marker * 12, 70 + marker * 3, 35 + marker * 12), fill="black")
    image.save(output, "JPEG"); return output.getvalue()


def files(count: int, offset: int = 0):
    return [("files", (f"iphone-{offset + index}.jpg", jpeg_bytes((30 + offset + index, 120, 90), offset + index), "image/jpeg")) for index in range(1, count + 1)]


def test_upload_frontend_is_relative_same_origin_and_safari_safe(client):
    text = client[0].get("/receipts/upload").text
    assert "const UPLOAD_URL = '/api/receipt-batches/upload'" in text
    assert "xhr.open('POST', UPLOAD_URL, true)" in text and "new FormData()" in text and "xhr.send(body)" in text
    assert "localhost" not in text and "127.0.0.1" not in text and "http://" not in text and "8020" not in text
    assert "xhr.responseType" not in text


def test_no_progress_status_zero_timeout_and_retry_messages(client):
    text = client[0].get("/receipts/upload").text
    assert "progressBar.removeAttribute('value')" in text and "正在上传${'.'.repeat(dots)}" in text
    assert "xhr.status === 0" in text and "UPLOAD_CONNECTION" in text and "Safari/Tailscale HTTPS" in text
    assert "xhr.timeout = 180000" in text and "UPLOAD_TIMEOUT" in text and "UPLOAD_ABORTED" in text
    assert "所选文件已保留，可直接重试" in text and "activeRequestId=activeRequestId||makeRequestId()" in text


def test_four_and_eight_jpegs_one_multipart_request(client):
    http, db, _ = client
    four = http.post("/api/receipt-batches/upload", files=files(4), data={"source_type": "pc", "request_id": "pc-four-0001"})
    eight = http.post("/api/receipt-batches/upload", files=files(8, 20), data={"source_type": "pc", "request_id": "pc-eight-0001"})
    assert four.status_code == eight.status_code == 201
    assert four.json()["success_count"] == 4 and eight.json()["success_count"] == 8
    assert [batch.image_count for batch in db.scalars(select(ReceiptBatch).order_by(ReceiptBatch.id))] == [4, 8]


def test_iphone_safari_user_agent_uploads_four_images_and_is_logged(client, monkeypatch):
    http, _, _ = client; messages = []
    monkeypatch.setattr(services.logger, "info", lambda template, *args: messages.append(template % args))
    response = http.post("/api/receipt-batches/upload", files=files(4), data={"source_type": "mobile", "request_id": "iphone-four-0001"}, headers={"user-agent": SAFARI_UA, "host": "jba.example.ts.net", "x-forwarded-proto": "https"})
    assert response.status_code == 201 and response.json()["success_count"] == 4
    request_log = next(item for item in messages if "stage='files_received'" in item)
    assert "file_count=4" in request_log and "iPhone" in request_log and "scheme='https'" in request_log


def test_request_id_retry_reuses_batch_without_duplicate_files(client):
    http, db, root = client; request_id = "retry-safari-0001"
    first = http.post("/api/receipt-batches/upload", files=files(4), data={"source_type": "mobile", "request_id": request_id})
    second = http.post("/api/receipt-batches/upload", files=files(4), data={"source_type": "mobile", "request_id": request_id})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"] and second.json()["idempotent_replay"] is True
    assert db.scalar(select(func.count()).select_from(ReceiptBatch)) == 1
    assert db.scalar(select(func.count()).select_from(ReceiptImage)) == 4
    assert len(list((root / "uploads" / "original").iterdir())) == 4


def test_upload_returns_before_processing_and_status_endpoint_reports_progress(client, monkeypatch):
    http, db, root = client
    monkeypatch.setattr(main_module, "process_receipt_batch", lambda *_args: None)
    response = http.post("/api/receipt-batches/upload", files=files(4), data={"source_type": "mobile", "request_id": "status-pending-0001"})
    assert response.status_code == 201 and response.json()["status"] == "uploaded"
    batch_id = response.json()["id"]
    status = http.get(f"/api/receipt-batches/{batch_id}/status").json()
    assert status == {"batch_id": batch_id, "batch_status": "uploaded", "image_status": "uploaded", "gpt_status": "not_packaged", "total_images": 4, "uploaded_images": 4, "processed_images": 0, "failed_images": 0, "current_stage": "uploaded", "errors": []}
    assert len(list((root / "uploads" / "original").iterdir())) == 4
    assert not list((root / "uploads" / "preview").iterdir())
    assert all(image.preprocessing_status == "uploaded" for image in db.scalars(select(ReceiptImage)))


def test_background_processing_failure_keeps_original_and_reports_code(client, monkeypatch):
    http, _, root = client
    monkeypatch.setattr(services, "prepare_receipt_image", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(services, "recognition_jpeg_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fallback")))
    response = http.post("/api/receipt-batches/upload", files=files(1), data={"request_id": "process-fail-0001"})
    status = http.get(f"/api/receipt-batches/{response.json()['id']}/status").json()
    assert status["batch_status"] == "failed" and status["failed_images"] == 1
    assert status["errors"][0]["code"] == "PROCESS_FAILED"
    assert len(list((root / "uploads" / "original").iterdir())) == 1


def test_async_rotate_reprocess_and_source_return_card_payload(client):
    http, _, _ = client
    batch = http.post("/api/receipt-batches/upload", files=files(1), data={"request_id": "async-actions-0001"}).json(); image_id = batch["images"][0]["id"]
    headers = {"accept": "application/json"}
    rotate = http.post(f"/receipts/{batch['id']}/images/{image_id}/rotate", headers=headers)
    retry = http.post(f"/receipts/{batch['id']}/images/{image_id}/reprocess", headers=headers)
    source = http.post(f"/receipts/{batch['id']}/images/{image_id}/source", data={"source": "original"}, headers=headers)
    assert rotate.json()["message"] == "已旋转90°" and rotate.json()["image"]["id"] == image_id
    assert retry.json()["message"] == "处理完成" and retry.json()["image"]["id"] == image_id
    assert source.json()["message"] == "识别图已更新" and source.json()["image"]["recognition_source"] == "original"
    text = http.get(f"/receipts/{batch['id']}").text
    assert "async-image-form" in text and "event.preventDefault()" in text and "updateImageCard" in text
    assert "window.location" not in text and "location.reload" not in text
    assert "scrollPosition={x:window.scrollX,y:window.scrollY}" in text and "window.scrollTo(scrollPosition.x,scrollPosition.y)" in text
