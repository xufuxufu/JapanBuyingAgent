from __future__ import annotations

import io
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import PROJECT_ROOT, SALES_ORDER_ITEM_IMAGE_DIR, SALES_ORDER_SHIPPING_LABEL_DIR, env_int


# Pillow format name -> (file extension, MIME type). Deliberately narrow: this is a
# phone-photo-of-a-shipping-label use case, not a general image pipeline.
ALLOWED_IMAGE_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}
UNSUPPORTED_FORMAT_MESSAGE = "当前请使用 JPG/PNG/WebP 图片"


def max_upload_bytes() -> int:
    return env_int("JBA_SHIPPING_LABEL_MAX_UPLOAD_MB", 10, 1, 30) * 1024 * 1024


def _validate_image(content: bytes) -> tuple[str, str]:
    if not content:
        raise ValueError("面单图片不能为空")
    if len(content) > max_upload_bytes():
        raise ValueError(f"面单图片不能超过 {max_upload_bytes() // 1024 // 1024} MB")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            image_format = (opened.format or "").upper()
            opened.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError(UNSUPPORTED_FORMAT_MESSAGE) from exc
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValueError(UNSUPPORTED_FORMAT_MESSAGE)
    try:
        with Image.open(io.BytesIO(content)) as reopened:
            reopened.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("图片已损坏或无法解码，请重新拍摄/选择") from exc
    extension, content_type = ALLOWED_IMAGE_FORMATS[image_format]
    return extension, content_type


def save_shipping_label_file(
    sales_order_id: int, *, content: bytes, original_filename: str | None,
) -> tuple[str, str, str, str | None, int]:
    """Validate and write an uploaded shipping label image to disk.

    Returns (stored_filename, relative_path, content_type, safe_original_name, file_size).
    Raises ValueError on any validation failure; never writes a partial/invalid file.
    """
    extension, content_type = _validate_image(content)
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    directory = SALES_ORDER_SHIPPING_LABEL_DIR / str(sales_order_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stored_filename
    path.write_bytes(content)
    relative_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    # The original filename is only ever used for display; .name strips any path
    # components so it can never influence where anything is written or read.
    safe_original_name = Path(original_filename).name[:255] if original_filename else None
    return stored_filename, relative_path, content_type, safe_original_name, len(content)


def resolve_shipping_label_path(relative_path: str) -> Path | None:
    """Resolve a stored relative path to an absolute file, refusing anything outside
    the shipping-label root. Returns None if the path is invalid, escapes the root,
    or the file no longer exists."""
    return _resolve_under_root(relative_path, SALES_ORDER_SHIPPING_LABEL_DIR)


def delete_shipping_label_file(relative_path: str) -> None:
    """Best-effort delete; a missing or already-removed file is not an error."""
    path = resolve_shipping_label_path(relative_path)
    if path is not None:
        path.unlink(missing_ok=True)


def _resolve_under_root(relative_path: str, allowed_root: Path) -> Path | None:
    try:
        path = (PROJECT_ROOT / relative_path).resolve()
    except (OSError, ValueError):
        return None
    if not path.is_relative_to(allowed_root.resolve()) or not path.is_file():
        return None
    return path


def save_sales_order_item_image_file(
    sales_order_id: int, *, content: bytes, original_filename: str | None,
) -> tuple[str, str, str, str | None, int]:
    """Validate and write an uploaded manual-item photo to disk. Same security
    checks as save_shipping_label_file (Pillow verify, UUID filename, format
    whitelist) -- only the storage root differs, so both reuse _validate_image.

    Returns (stored_filename, relative_path, content_type, safe_original_name, file_size).
    Raises ValueError on any validation failure; never writes a partial/invalid file.
    """
    extension, content_type = _validate_image(content)
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    directory = SALES_ORDER_ITEM_IMAGE_DIR / str(sales_order_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stored_filename
    path.write_bytes(content)
    relative_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    safe_original_name = Path(original_filename).name[:255] if original_filename else None
    return stored_filename, relative_path, content_type, safe_original_name, len(content)


def resolve_sales_order_item_image_path(relative_path: str) -> Path | None:
    """Same traversal guard as resolve_shipping_label_path, scoped to the
    item-image root instead."""
    return _resolve_under_root(relative_path, SALES_ORDER_ITEM_IMAGE_DIR)


def delete_sales_order_item_image_file(relative_path: str) -> None:
    """Best-effort delete; a missing or already-removed file is not an error.

    Not called from any normal order flow -- manual-item photos are a
    permanent historical snapshot of the order line and are never replaced or
    deleted, even if the line later gets linked to a real Product. This
    exists only for symmetry / potential future admin cleanup tooling.
    """
    path = resolve_sales_order_item_image_path(relative_path)
    if path is not None:
        path.unlink(missing_ok=True)
