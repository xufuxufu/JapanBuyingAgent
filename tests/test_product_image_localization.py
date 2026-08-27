from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select

import app.field_purchase as field_purchase
import app.image_localization_worker as localization_worker
import app.product_image_localization as image_localization
from app.field_purchase import process_durable_job
from app.models import DurableBackgroundJob, Product
from app.product_image_localization import (
    ImageLocalizationError,
    download_remote_image,
    product_display_image,
    preferred_product_image_url,
    process_product_image_job,
    queue_product_image_localization,
    validate_remote_image_url,
)


PUBLIC_IP = ["93.184.216.34"]


def png_bytes(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color=color).save(output, format="PNG")
    return output.getvalue()


def mock_client(content: bytes, *, content_type: str = "image/png", status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": content_type, "content-length": str(len(content))},
            content=content,
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def local_image_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "project"
    directory = root / "data" / "products" / "qinsi-localized"
    monkeypatch.setattr(image_localization, "PROJECT_ROOT", root)
    monkeypatch.setattr(image_localization, "QINSI_PRODUCT_IMAGE_DIR", directory)
    monkeypatch.setenv("JBA_QINSI_IMAGE_ALLOWED_HOSTS", "qinsilk.com")
    return directory


def test_image_download_success_sha256_dedupe_atomic_and_display_update(
    db_session,
    monkeypatch,
    tmp_path,
):
    directory = local_image_root(monkeypatch, tmp_path)
    content = png_bytes()
    products = [
        Product(name_cn="图一", image_url="https://images.qinsilk.com/a.png"),
        Product(name_cn="图二", image_url="https://images.qinsilk.com/b.png"),
    ]
    db_session.add_all(products)
    db_session.commit()
    jobs = [queue_product_image_localization(db_session, product) for product in products]
    db_session.commit()
    with mock_client(content) as client:
        for job in jobs:
            assert process_product_image_job(
                db_session, job, client=client, resolver=lambda _: PUBLIC_IP,
            ) == "COMPLETED"
            db_session.commit()

    assert products[0].image_sha256 == products[1].image_sha256
    assert products[0].local_image_path == products[1].local_image_path
    assert products[0].display_image_url.startswith(f"/product-local-images/{products[0].id}?v=")
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == content
    assert not list(directory.rglob(".jba-image-*"))


def test_image_failure_timeout_fake_mime_size_and_ssrf_are_blocked(monkeypatch):
    monkeypatch.setenv("JBA_QINSI_IMAGE_ALLOWED_HOSTS", "qinsilk.com")
    with pytest.raises(ImageLocalizationError, match="白名单"):
        validate_remote_image_url("https://example.com/a.png", resolver=lambda _: PUBLIC_IP)
    monkeypatch.setenv("JBA_QINSI_IMAGE_ALLOWED_HOSTS", "qinsilk.com,thumbnail.image.rakuten.co.jp")
    assert validate_remote_image_url(
        "https://thumbnail.image.rakuten.co.jp/@0_mall/example/a.jpg",
        resolver=lambda _: PUBLIC_IP,
    ).startswith("https://thumbnail.image.rakuten.co.jp/")
    with pytest.raises(ImageLocalizationError, match="SSRF"):
        validate_remote_image_url(
            "https://images.qinsilk.com/a.png", resolver=lambda _: ["127.0.0.1"],
        )
    with pytest.raises(ImageLocalizationError, match="账号"):
        validate_remote_image_url(
            "https://user:secret@images.qinsilk.com/a.png", resolver=lambda _: PUBLIC_IP,
        )

    fake = b"\x89PNG\r\n\x1a\nnot-a-real-image"
    with mock_client(fake) as client, pytest.raises(ImageLocalizationError, match="损坏|解码"):
        download_remote_image(
            "https://images.qinsilk.com/fake.png", client=client, resolver=lambda _: PUBLIC_IP,
        )
    with mock_client(png_bytes(), content_type="image/jpeg") as client, pytest.raises(
        ImageLocalizationError, match="MIME"
    ):
        download_remote_image(
            "https://images.qinsilk.com/mismatch.png", client=client, resolver=lambda _: PUBLIC_IP,
        )

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout_handler)) as client, pytest.raises(httpx.ReadTimeout):
        download_remote_image(
            "https://images.qinsilk.com/timeout.png", client=client, resolver=lambda _: PUBLIC_IP,
        )

    monkeypatch.setenv("JBA_QINSI_IMAGE_MAX_MB", "1")
    def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": str(2 * 1024 * 1024)},
            content=b"",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(oversized_handler)) as client, pytest.raises(
        ImageLocalizationError, match="超过允许大小"
    ):
        download_remote_image(
            "https://images.qinsilk.com/large.png", client=client, resolver=lambda _: PUBLIC_IP,
        )


def test_durable_image_job_failure_keeps_remote_and_old_display_for_retry(
    db_session,
    monkeypatch,
):
    product = Product(
        name_cn="失败图",
        image_url="https://images.qinsilk.com/new.png",
        display_image_url="/product-local-images/99?v=old",
        image_localization_source_url="https://images.qinsilk.com/old.png",
    )
    db_session.add(product)
    db_session.commit()
    job = queue_product_image_localization(db_session, product)
    db_session.commit()

    def fail_job(*_args, **_kwargs):
        raise httpx.ReadTimeout("mock timeout")

    monkeypatch.setattr(field_purchase, "process_product_image_job", fail_job)
    process_durable_job(job.id, db_session.get_bind())
    db_session.expire_all()
    failed_job = db_session.get(DurableBackgroundJob, job.id)
    failed_product = db_session.get(Product, product.id)
    assert failed_job.status == "FAILED_RETRYABLE"
    assert failed_product.image_url == "https://images.qinsilk.com/new.png"
    assert failed_product.display_image_url == "/product-local-images/99?v=old"
    assert failed_product.image_localization_status == "FAILED_RETRYABLE"
    assert "ReadTimeout" in failed_product.image_localization_error


def test_changed_qinsi_image_creates_new_job_without_replacing_old_display(db_session):
    product = Product(
        name_cn="换图",
        image_url="https://images.qinsilk.com/old.png",
        display_image_url="/product-local-images/1?v=old",
        image_localization_source_url="https://images.qinsilk.com/old.png",
        image_localization_status="COMPLETED",
        local_image_path="data/products/qinsi-localized/old.png",
    )
    db_session.add(product)
    db_session.commit()
    assert queue_product_image_localization(db_session, product) is None
    product.image_url = "https://images.qinsilk.com/new.png"
    job = queue_product_image_localization(db_session, product)
    db_session.commit()
    assert job is not None and job.status == "PENDING"
    assert product.display_image_url == "/product-local-images/1?v=old"
    assert product.image_localization_source_url.endswith("/old.png")
    assert db_session.scalar(
        select(func.count()).select_from(DurableBackgroundJob)
    ) == 1


def test_main_image_source_url_can_be_queued_and_failure_keeps_product_saved(db_session, monkeypatch):
    monkeypatch.setenv("JBA_QINSI_IMAGE_ALLOWED_HOSTS", "qinsilk.com")
    product = Product(
        name_cn="远程图",
        main_image_source_url="https://images.qinsilk.com/source.png",
    )
    db_session.add(product)
    db_session.commit()
    job = queue_product_image_localization(db_session, product)
    db_session.commit()
    assert job is not None

    with mock_client(b"not-image", content_type="text/plain") as client:
        with pytest.raises(ImageLocalizationError):
            process_product_image_job(db_session, job, client=client, resolver=lambda _: PUBLIC_IP)
    db_session.refresh(product)
    assert product.main_image_source_url == "https://images.qinsilk.com/source.png"
    assert db_session.get(Product, product.id) is not None


def test_product_image_fallback_and_field_offline_cache_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(image_localization, "PROJECT_ROOT", tmp_path)
    product = Product(id=7, image_url="https://images.qinsilk.com/source.png")
    assert preferred_product_image_url(product) == "https://images.qinsilk.com/source.png"
    local_path = tmp_path / "data" / "products" / "qinsi-localized" / "a.jpg"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"local")
    product.local_image_path = "data/products/qinsi-localized/a.jpg"
    product.image_sha256 = "abcdef1234567890"
    product.display_image_url = "/product-local-images/7?v=abc"
    assert preferred_product_image_url(product) == "/product-local-images/7?v=abcdef123456"
    display = product_display_image(product)
    assert display.display_image_url == "/product-local-images/7?v=abcdef123456"
    assert display.status == "local"
    product.display_image_url = None
    assert preferred_product_image_url(product) == "/product-local-images/7?v=abcdef123456"
    product.local_image_path = None
    product.image_url = None
    main_path = tmp_path / "data" / "products" / "main" / "a.png"
    main_path.parent.mkdir(parents=True)
    main_path.write_bytes(b"main")
    product.main_image_path = "data/products/main/a.png"
    assert preferred_product_image_url(product) == "/product-images/7"
    product.main_image_source_url = "https://main.example.test/a.jpg"
    product.image_url = "https://images.qinsilk.com/source.png"
    product.main_image_path = None
    display = product_display_image(product)
    assert display.display_image_url == "https://main.example.test/a.jpg"
    assert display.status == "remote"
    product.main_image_source_url = None
    product.image_url = None
    display = product_display_image(product)
    assert display.display_image_url is None
    assert display.status == "placeholder"

    root = Path(__file__).resolve().parents[1]
    field_js = (root / "app" / "static" / "field_purchase.js").read_text(encoding="utf-8")
    worker = (root / "app" / "static" / "service-worker.js").read_text(encoding="utf-8")
    template = (root / "app" / "templates" / "field_purchase.html").read_text(encoding="utf-8")
    assert 'IMAGE_CACHE_NAME = "jba-product-images-v1"' in field_js
    assert "cached.image_blob" in field_js and "caches.open(IMAGE_CACHE_NAME)" in field_js
    assert "下载本批次商品图供离线查看" in template
    assert "[CACHE_NAME, IMAGE_CACHE_NAME]" in worker


def test_image_progress_and_jan_governance_pages_are_available(client):
    http, _db, _ = client
    image_page = http.get("/product-image-localization")
    jan_page = http.get("/jan-governance")
    more_page = http.get("/more")
    assert image_page.status_code == 200 and "重试失败" in image_page.text
    assert jan_page.status_code == 200 and "安全修复并导出 CSV" in jan_page.text
    assert "/product-image-localization" in more_page.text
    assert "/jan-governance" in more_page.text


def test_independent_worker_continues_batches_until_queue_empty(monkeypatch, db_session):
    results = iter([20, 20, 7, 0])
    calls = []
    monkeypatch.setattr(localization_worker, "is_testing", lambda: False)
    monkeypatch.setattr(localization_worker, "process_pending_jobs", lambda *_args, **kwargs: calls.append(kwargs) or next(results))
    run = localization_worker.run_image_localization_worker(db_session.get_bind(), once=True)
    assert (run.processed, run.batches) == (47, 3)
    assert len(calls) == 4 and all(call["job_type"] == image_localization.JOB_TYPE for call in calls)


def test_image_localization_buttons_return_immediate_json_feedback(client):
    http, _db, _ = client
    queue = http.post("/product-image-localization/queue", headers={"Accept": "application/json"})
    retry = http.post("/product-image-localization/retry-failed", headers={"Accept": "application/json"})
    status = http.get("/api/product-image-localization/status")
    assert queue.status_code == retry.status_code == 202 and status.status_code == 200
    assert queue.json()["status"] == retry.json()["status"] == "accepted"
    assert {"total", "completed", "failed", "pending", "disk_megabytes", "average_per_minute"} <= status.json().keys()
