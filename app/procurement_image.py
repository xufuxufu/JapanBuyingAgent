from __future__ import annotations

import uuid
from pathlib import Path

from app.config import PROCUREMENT_DEMAND_IMAGE_DIR, PROJECT_ROOT
from app.sales_order_shipping import _resolve_under_root, _validate_image

# Reuses the exact same image-security primitives Phase 7 built for sales-order
# manual-item photos (_validate_image / _resolve_under_root: Pillow verify,
# JPEG/PNG/WebP whitelist, size cap, UUID filename, path-traversal guard) --
# only the storage root differs, so this is a thin wrapper, not a second
# security implementation.


def save_procurement_demand_image_file(
    demand_id: int, *, content: bytes, original_filename: str | None,
) -> tuple[str, str, str, str | None, int]:
    """Validate and write an uploaded manual-restock-demand photo to disk.

    Returns (stored_filename, relative_path, content_type, safe_original_name, file_size).
    Raises ValueError on any validation failure; never writes a partial/invalid file.
    """
    extension, content_type = _validate_image(content)
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    directory = PROCUREMENT_DEMAND_IMAGE_DIR / str(demand_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / stored_filename
    path.write_bytes(content)
    relative_path = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    safe_original_name = Path(original_filename).name[:255] if original_filename else None
    return stored_filename, relative_path, content_type, safe_original_name, len(content)


def resolve_procurement_demand_image_path(relative_path: str) -> Path | None:
    """Same traversal guard as the sales-order image resolvers, scoped to the
    procurement-demand image root instead."""
    return _resolve_under_root(relative_path, PROCUREMENT_DEMAND_IMAGE_DIR)
