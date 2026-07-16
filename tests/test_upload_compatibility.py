from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

import app.services as services
from app.models import ReceiptBatch, ReceiptImage


def encoded_image(mode="RGB", fmt="JPEG", *, progressive=False, exif=None, marker=0) -> bytes:
    output = io.BytesIO()
    color = (20, 120, 80) if mode == "RGB" else (0, 100, 100, 0)
    image = Image.new(mode, (96, 144), color)
    if marker and mode == "RGB":
        ImageDraw.Draw(image).rectangle((5 + marker * 6, 8 + marker * 12, 55 + marker * 4, 25 + marker * 12), fill=(0, 0, 0))
    options = {"progressive": progressive} if fmt == "JPEG" else {}
    if exif is not None:
        options["exif"] = exif
    image.save(output, format=fmt, **options)
    return output.getvalue()


def post_one(http, content: bytes, mime: str, name="wechat.jpg"):
    return http.post(
        "/api/receipt-batches/upload",
        files={"files": (name, content, mime)},
        data={"source_type": "pc"},
    )


@pytest.mark.parametrize("mime", ["image/jpeg", "image/jpg", "application/octet-stream"])
def test_jpeg_accepted_independent_of_browser_mime(client, mime):
    http, _, _ = client
    response = post_one(http, encoded_image(), mime)
    assert response.status_code == 201
    assert response.json()["success_count"] == 1


def test_progressive_and_cmyk_jpeg_generate_previews(client):
    http, db, root = client
    response = http.post(
        "/api/receipt-batches/upload",
        files=[
            ("files", ("progressive.jpg", encoded_image(progressive=True), "image/jpeg")),
            ("files", ("cmyk.jpg", encoded_image("CMYK"), "image/jpeg")),
        ],
    )
    assert response.status_code == 201
    assert response.json()["success_count"] == 2
    for image in db.scalars(select(ReceiptImage)):
        with Image.open(root / image.processed_path) as preview:
            assert preview.mode == "RGB" and preview.format == "JPEG"


def test_mpo_encoded_jpeg_family_uploads_as_jpg(client):
    http, db, root = client
    output = io.BytesIO()
    first = Image.new("RGB", (96, 144), (20, 120, 80))
    second = Image.new("RGB", (96, 144), (30, 130, 90))
    first.save(output, format="MPO", save_all=True, append_images=[second])
    response = post_one(http, output.getvalue(), "application/octet-stream", "微信小票.jpg")
    assert response.status_code == 201
    image = db.scalar(select(ReceiptImage))
    assert (root / image.processed_path).is_file()


def test_exif_orientation_jpeg_upload_is_corrected(client):
    http, db, _ = client
    exif = Image.Exif()
    exif[274] = 6
    assert post_one(http, encoded_image(exif=exif), "image/jpeg").status_code == 201
    image = db.scalar(select(ReceiptImage))
    assert (image.processed_width, image.processed_height) == (144, 96)


@pytest.mark.parametrize(("fmt", "name", "mime"), [
    ("PNG", "receipt.png", "image/png"),
    ("WEBP", "receipt.webp", "image/webp"),
])
def test_png_and_webp_upload(client, fmt, name, mime):
    http, _, _ = client
    assert post_one(http, encoded_image(fmt=fmt), mime, name).status_code == 201


def test_non_image_and_corrupt_jpeg_are_clear_and_named(client):
    http, db, root = client
    valid = encoded_image()
    for content, expected in [(b"not an image", "文件内容不是有效图片"), (valid[:-25], "图片已损坏或无法解码")]:
        response = post_one(http, content, "image/jpeg", "坏图.jpg")
        assert response.status_code == 415
        failure = response.json()["failures"][0]
        assert failure["filename"] == "坏图.jpg" and failure["reason"] == expected
    assert db.scalar(select(func.count()).select_from(ReceiptBatch)) == 0
    assert not list((root / "uploads" / "original").iterdir())
    assert not list((root / "uploads" / "preview").iterdir())


def test_decodable_but_unsupported_actual_format_is_rejected(client):
    http, db, _ = client
    response = post_one(http, encoded_image(fmt="GIF"), "image/jpeg", "renamed.jpg")
    assert response.status_code == 415
    assert response.json()["failures"] == [{"filename": "renamed.jpg", "reason": "实际图片格式不受支持", "code": "IMAGE_INVALID"}]
    assert db.scalar(select(func.count()).select_from(ReceiptBatch)) == 0


def test_eight_valid_images_all_succeed(client):
    http, db, _ = client
    files = [("files", (f"微信小票{i}.jpg", encoded_image(marker=i), "application/octet-stream")) for i in range(1, 9)]
    response = http.post("/api/receipt-batches/upload", files=files)
    assert response.status_code == 201
    assert response.json()["success_count"] == 8
    batch = db.scalar(select(ReceiptBatch))
    assert batch.image_count == 8


def test_partial_failure_keeps_seven_with_continuous_pages_and_no_orphans(client):
    http, db, root = client
    files = [("files", (f"receipt-{i}.jpg", encoded_image(marker=i), "image/jpeg")) for i in range(1, 8)]
    files.insert(3, ("files", ("不是图片.jpg", b"fake", "image/jpeg")))
    response = http.post("/api/receipt-batches/upload", files=files)
    data = response.json()
    assert response.status_code == 201
    assert (data["success_count"], data["failure_count"]) == (7, 1)
    assert data["failures"] == [{"filename": "不是图片.jpg", "reason": "文件内容不是有效图片", "code": "IMAGE_INVALID"}]
    batch = db.scalar(select(ReceiptBatch))
    assert batch.image_count == 7
    assert [item.page_no for item in db.scalars(select(ReceiptImage).order_by(ReceiptImage.page_no))] == list(range(1, 8))
    assert len(list((root / "uploads" / "original").iterdir())) == 7
    assert len(list((root / "uploads" / "preview").iterdir())) == 7


def test_upload_page_displays_counts_failures_and_retry_controls(client):
    http, _, _ = client
    text = http.get("/receipts/upload").text
    assert "resultSummary" in text and "✓ 已完成${state.total_images}张" in text
    assert "failureList" in text and "重试失败" in text and "查看批次" in text


def test_safe_debug_log_has_stage_and_no_absolute_path(client, monkeypatch):
    http, _, _ = client
    messages = []
    monkeypatch.setattr(services.logger, "info", lambda template, *args: messages.append(template % args))
    post_one(http, b"invalid", "application/octet-stream", "wx.jpg")
    message = next(item for item in messages if "filename='wx.jpg'" in item)
    assert "filename='wx.jpg'" in message and "stage='pillow_open'" in message
    assert "exception_type='UnidentifiedImageError'" in message
    assert "E:\\" not in message and "C:\\" not in message
