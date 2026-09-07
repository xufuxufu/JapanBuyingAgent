from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
from io import BytesIO
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, QINSI_PRODUCT_IMAGE_DIR, env_int
from app.models import DurableBackgroundJob, Product


JOB_TYPE = "PRODUCT_IMAGE_LOCALIZATION"
DEFAULT_ALLOWED_HOSTS = (
    "qinsilk.com",
    "thumbnail.image.rakuten.co.jp",
    "image.rakuten.co.jp",
    "r10s.jp",
    "item-shopping.c.yimg.jp",
    "shopping.c.yimg.jp",
)
ALLOWED_IMAGE_MIMES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class ImageLocalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    content: bytes
    sha256: str
    extension: str
    mime_type: str
    source_url: str
    width: int
    height: int
    quality: str


@dataclass(frozen=True, slots=True)
class ProductDisplayImage:
    display_image_url: str | None
    status: str
    source_field: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _product_source_image_url(product: Product | None) -> tuple[str | None, str | None]:
    if product is None:
        return None, None
    main_source = (product.main_image_source_url or "").strip()
    if main_source:
        return main_source, "main_image_source_url"
    legacy_source = (product.image_url or "").strip()
    if legacy_source:
        return legacy_source, "image_url"
    qinsi_source = (product.qinsi_image_url or "").strip()
    if qinsi_source:
        return qinsi_source, "qinsi_image_url"
    return None, None


def product_display_image(product: Product | None) -> ProductDisplayImage:
    if product is None:
        return ProductDisplayImage(None, "placeholder")
    if product.local_image_path and product.id and _stored_product_image_exists(product.local_image_path):
        version = f"?v={(product.image_sha256 or '')[:12]}" if product.image_sha256 else ""
        return ProductDisplayImage(f"/product-local-images/{product.id}{version}", "local", "local_image_path")
    if product.main_image_path and product.id and _stored_product_image_exists(product.main_image_path):
        return ProductDisplayImage(f"/product-images/{product.id}", "local", "main_image_path")
    if product.display_image_url and product.display_image_url.startswith(("http://", "https://")):
        return ProductDisplayImage(product.display_image_url, "remote", "display_image_url")
    source_url, source_field = _product_source_image_url(product)
    if source_url:
        return ProductDisplayImage(source_url, "remote", source_field)
    return ProductDisplayImage(None, "placeholder")


def preferred_product_image_url(product: Product | None) -> str | None:
    return product_display_image(product).display_image_url


def _stored_product_image_exists(value: str | None) -> bool:
    if not value:
        return False
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        resolved = path.resolve()
        resolved.relative_to(PROJECT_ROOT.resolve())
    except (OSError, ValueError):
        return False
    return resolved.is_file()


def product_image_summary(product: Product) -> dict[str, object]:
    return {
        "id": product.id,
        "internal_sku": product.internal_sku,
        "name": product.display_name or product.name_cn or product.name_ja or product.internal_sku,
        "name_cn": product.name_cn,
        "name_ja": product.name_ja,
        "specification": product.specification or product.model_spec,
        "image_url": preferred_product_image_url(product),
        "qinsi_product_code": product.qinsi_product_code,
        "qinsi_barcodes": [
            row.barcode for row in product.barcodes
            if row.source_system in {"qinsi", "qinsi_sku_derived"}
        ],
        "jan": product.jan,
    }


def _allowed_hosts() -> tuple[str, ...]:
    configured = os.getenv("JBA_QINSI_IMAGE_ALLOWED_HOSTS", "")
    values = tuple(
        item.strip().casefold().lstrip(".")
        for item in configured.split(",")
        if item.strip()
    )
    return values or DEFAULT_ALLOWED_HOSTS


def _host_allowed(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in _allowed_hosts())


def _resolved_addresses(hostname: str) -> list[str]:
    return list(dict.fromkeys(
        item[4][0]
        for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    ))


def validate_remote_image_url(
    value: str,
    *,
    resolver=_resolved_addresses,
) -> str:
    url = (value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageLocalizationError("图片地址只允许 HTTP/HTTPS")
    if parsed.username or parsed.password:
        raise ImageLocalizationError("图片地址不得包含账号信息")
    if parsed.port not in {None, 80, 443}:
        raise ImageLocalizationError("图片地址端口不在允许范围")
    if not _host_allowed(parsed.hostname):
        raise ImageLocalizationError(f"图片域名不在秦丝白名单：{parsed.hostname}")
    try:
        addresses = resolver(parsed.hostname)
    except OSError as exc:
        raise ImageLocalizationError("图片域名解析失败") from exc
    if not addresses:
        raise ImageLocalizationError("图片域名没有可用地址")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ImageLocalizationError("图片域名解析结果无效") from exc
        if not ip.is_global:
            raise ImageLocalizationError("图片地址解析到非公网地址，已阻止 SSRF")
    return url


def _sniff_image(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    raise ImageLocalizationError("下载内容不是真实支持的图片格式")


def _image_quality(width: int, height: int) -> str:
    minimum = min(width, height)
    if minimum < 300:
        return "thumbnail"
    if minimum < 600:
        return "low"
    return "normal"


def _quality_score(value: str | None) -> int:
    return {"thumbnail": 0, "low": 1, "normal": 2, "original": 3}.get(value or "", 2)


def _verify_decodable_image(content: bytes, expected_mime: str) -> tuple[int, int, str]:
    format_mimes = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
        "GIF": "image/gif",
    }
    try:
        with Image.open(BytesIO(content)) as image:
            actual_mime = format_mimes.get(str(image.format or "").upper())
            if actual_mime != expected_mime:
                raise ImageLocalizationError("图片解码格式与文件头不一致")
            if image.width * image.height > 50_000_000:
                raise ImageLocalizationError("图片像素尺寸超过安全上限")
            width, height = image.width, image.height
            image.verify()
            return width, height, _image_quality(width, height)
    except ImageLocalizationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageLocalizationError("图片文件损坏或无法解码") from exc


def download_remote_image(
    source_url: str,
    *,
    client: httpx.Client | None = None,
    resolver=_resolved_addresses,
) -> DownloadedImage:
    max_bytes = env_int("JBA_QINSI_IMAGE_MAX_MB", 10, 1, 50) * 1024 * 1024
    timeout_seconds = env_int("JBA_QINSI_IMAGE_TIMEOUT_SECONDS", 15, 3, 60)
    owns_client = client is None
    context = (
        httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"Accept": "image/jpeg,image/png,image/webp,image/gif", "User-Agent": "JBA/1.0"},
        )
        if owns_client else nullcontext(client)
    )
    current_url = validate_remote_image_url(source_url, resolver=resolver)
    with context as http:
        for _ in range(4):
            with http.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageLocalizationError("图片重定向缺少目标地址")
                    current_url = validate_remote_image_url(
                        urljoin(current_url, location), resolver=resolver,
                    )
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise ImageLocalizationError("图片超过允许大小")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageLocalizationError("图片超过允许大小")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise ImageLocalizationError("图片响应为空")
                sniffed_mime, extension = _sniff_image(content)
                width, height, quality = _verify_decodable_image(content, sniffed_mime)
                declared_mime = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                if declared_mime == "image/jpg":
                    declared_mime = "image/jpeg"
                if declared_mime not in ALLOWED_IMAGE_MIMES or declared_mime != sniffed_mime:
                    raise ImageLocalizationError("图片 MIME 与真实格式不一致")
                return DownloadedImage(
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    extension=extension,
                    mime_type=sniffed_mime,
                    source_url=current_url,
                    width=width,
                    height=height,
                    quality=quality,
                )
    raise ImageLocalizationError("图片重定向次数过多")


def _safe_image_stem(product: Product, image: DownloadedImage) -> str:
    raw = str(product.jan or image.sha256)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("._-")
    return stem or image.sha256[:12]


def _content_path(product: Product, image: DownloadedImage) -> Path:
    root = QINSI_PRODUCT_IMAGE_DIR.resolve()
    filename = f"{_safe_image_stem(product, image)}-{image.sha256[:16]}{image.extension}"
    destination = (root / image.sha256[:2] / filename).resolve()
    if not destination.is_relative_to(root):
        raise ImageLocalizationError("本地图片路径无效")
    return destination


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        return
    handle, temporary_name = tempfile.mkstemp(prefix=".jba-image-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def queue_product_image_localization(
    session: Session,
    product: Product,
    *,
    force_retry: bool = False,
) -> DurableBackgroundJob | None:
    source_url, _source_field = _product_source_image_url(product)
    if not source_url:
        return None
    source_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    dedupe_key = f"product-image:{product.id}:{source_hash[:24]}"
    existing = session.scalar(
        select(DurableBackgroundJob).where(DurableBackgroundJob.dedupe_key == dedupe_key)
    )
    if existing is not None:
        if force_retry and existing.status in {"COMPLETED", "FAILED_RETRYABLE", "FAILED_MANUAL"}:
            existing.status = "PENDING"
            existing.attempts = 0
            existing.available_at = utcnow()
            existing.locked_at = None
            existing.last_error = None
            existing.completed_at = None
            product.image_localization_status = "PENDING"
            product.image_localization_error = None
            return existing
        if (
            existing.status == "COMPLETED"
            and product.image_localization_status == "COMPLETED"
            and product.image_localization_source_url == source_url
            and product.local_image_path
        ):
            return None
        return None
    if (
        not force_retry
        and product.local_image_path
        and _stored_product_image_exists(product.local_image_path)
        and product.image_localization_source_url == source_url
    ):
        return None
    if (
        product.image_localization_status == "COMPLETED"
        and product.image_localization_source_url == source_url
        and product.local_image_path
    ):
        return None
    job = DurableBackgroundJob(
        dedupe_key=dedupe_key,
        job_type=JOB_TYPE,
        payload_json=json.dumps(
            {"product_id": product.id, "source_url": source_url}, ensure_ascii=False,
        ),
        status="PENDING",
        max_attempts=env_int("JBA_QINSI_IMAGE_MAX_ATTEMPTS", 3, 1, 10),
    )
    session.add(job)
    product.image_localization_status = "PENDING"
    product.image_localization_error = None
    return job


def queue_missing_product_images(session: Session) -> int:
    count = 0
    products = list(session.scalars(
        select(Product)
        .where((Product.main_image_source_url.is_not(None)) | (Product.image_url.is_not(None)))
        .order_by(Product.id)
    ))
    for product in products:
        if queue_product_image_localization(session, product) is not None:
            count += 1
    session.commit()
    return count


def retry_failed_product_images(session: Session) -> int:
    products = list(
        session.scalars(
            select(Product).where(
                (Product.main_image_source_url.is_not(None)) | (Product.image_url.is_not(None)),
                Product.image_localization_status.in_({"FAILED_RETRYABLE", "FAILED_MANUAL"}),
            )
        )
    )
    count = 0
    for product in products:
        if queue_product_image_localization(session, product, force_retry=True) is not None:
            count += 1
    session.commit()
    return count


def process_product_image_job(
    session: Session,
    job: DurableBackgroundJob,
    *,
    client: httpx.Client | None = None,
    resolver=_resolved_addresses,
) -> str:
    payload = json.loads(job.payload_json)
    product = session.get(Product, int(payload["product_id"]))
    if product is None:
        raise LookupError("商品不存在")
    source_url = str(payload["source_url"])
    current_source_url, _source_field = _product_source_image_url(product)
    if current_source_url != source_url:
        return "STALE"
    image = download_remote_image(source_url, client=client, resolver=resolver)
    if (
        product.local_image_path
        and _stored_product_image_exists(product.local_image_path)
        and _quality_score(image.quality) < _quality_score(product.image_quality)
    ):
        product.image_localization_status = "COMPLETED"
        product.image_localization_source_url = source_url
        product.image_localized_at = utcnow()
        product.image_localization_error = None
        return "COMPLETED"
    destination = _content_path(product, image)
    _atomic_write(destination, image.content)
    product.local_image_path = destination.relative_to(PROJECT_ROOT.resolve()).as_posix()
    product.image_sha256 = image.sha256
    product.image_width = image.width
    product.image_height = image.height
    product.image_quality = image.quality
    product.display_image_url = f"/product-local-images/{product.id}?v={image.sha256[:12]}"
    product.image_localization_status = "COMPLETED"
    product.image_localization_source_url = source_url
    product.image_localized_at = utcnow()
    product.image_localization_error = None
    return "COMPLETED"


def mark_product_image_failure(
    session: Session,
    job: DurableBackgroundJob,
    error: Exception,
) -> None:
    try:
        payload = json.loads(job.payload_json)
        product = session.get(Product, int(payload["product_id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    current_source_url, _source_field = _product_source_image_url(product)
    if product is None or current_source_url != str(payload.get("source_url") or ""):
        return
    product.image_localization_status = job.status
    product.image_localization_error = f"{type(error).__name__}: {str(error)[:300]}"


def image_localization_dashboard(session: Session) -> dict[str, object]:
    counts = dict(
        session.execute(
            select(Product.image_localization_status, func.count(Product.id))
            .where((Product.main_image_source_url.is_not(None)) | (Product.image_url.is_not(None)))
            .group_by(Product.image_localization_status)
        ).all()
    )
    recent_jobs = list(
        session.scalars(
            select(DurableBackgroundJob)
            .where(DurableBackgroundJob.job_type == JOB_TYPE)
            .order_by(DurableBackgroundJob.updated_at.desc(), DurableBackgroundJob.id.desc())
            .limit(100)
        )
    )
    completed_count, first_started, last_completed = session.execute(
        select(
            func.count(DurableBackgroundJob.id),
            func.min(DurableBackgroundJob.created_at),
            func.max(DurableBackgroundJob.completed_at),
        ).where(
            DurableBackgroundJob.job_type == JOB_TYPE,
            DurableBackgroundJob.status == "COMPLETED",
            DurableBackgroundJob.completed_at.is_not(None),
        )
    ).one()
    elapsed_seconds = (
        max(1.0, (last_completed - first_started).total_seconds())
        if first_started and last_completed
        else 0.0
    )
    localized_paths = {
        (PROJECT_ROOT / path).resolve()
        for path in session.scalars(
            select(Product.local_image_path).where(Product.local_image_path.is_not(None))
        )
        if path
    }
    disk_bytes = sum(
        path.stat().st_size
        for path in localized_paths
        if path.is_relative_to(QINSI_PRODUCT_IMAGE_DIR.resolve()) and path.is_file()
    )
    normalized_counts = {
        str(key or "NOT_QUEUED"): int(value) for key, value in counts.items()
    }
    return {
        "total": sum(int(value) for value in counts.values()),
        "counts": normalized_counts,
        "completed": normalized_counts.get("COMPLETED", 0),
        "failed": normalized_counts.get("FAILED_RETRYABLE", 0)
        + normalized_counts.get("FAILED_MANUAL", 0),
        "pending": normalized_counts.get("PENDING", 0)
        + normalized_counts.get("RUNNING", 0),
        "disk_bytes": disk_bytes,
        "disk_megabytes": round(disk_bytes / 1024 / 1024, 2),
        "average_per_minute": (
            round(int(completed_count) * 60 / elapsed_seconds, 2)
            if elapsed_seconds
            else 0.0
        ),
        "recent_jobs": recent_jobs,
    }
