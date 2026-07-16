from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session


TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")
INTERNAL_SKU_PATTERN = re.compile(r"^NJ-(\d{8})-(\d{6})$")
PRODUCT_NAME_MAX_LENGTH = 128
DISPLAY_NAME_MAX_LENGTH = 128
PRODUCT_NAME_SEPARATOR = "｜"
IMPORTANT_TOKEN_PATTERN = re.compile(
    r"(?i)(?:\d+(?:\.\d+)?\s*(?:ml|l|g|kg|mm|cm|個|本|枚|袋|包|錠|粒)|"
    r"[A-Z]{1,8}[-_/]?[A-Z0-9]{1,16}|黑|白|红|蓝|绿|粉|紫|金|银|黒|赤|青|緑|桃|白)"
)
_events_installed = False


def normalize_optional_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_product_name(value: str | None, label: str) -> str | None:
    value = normalize_optional_identifier(value)
    if value is not None and len(value) > PRODUCT_NAME_MAX_LENGTH:
        raise ValueError(f"{label}最长 {PRODUCT_NAME_MAX_LENGTH} 字符")
    return value


def format_product_display_name(name_cn: str | None, name_ja: str | None) -> str:
    cn = normalize_optional_identifier(name_cn) or "中文名待补"
    ja = normalize_optional_identifier(name_ja) or "日文名待补"
    raw = f"{cn}{PRODUCT_NAME_SEPARATOR}{ja}"
    if len(raw) <= DISPLAY_NAME_MAX_LENGTH:
        return raw
    cn_budget = (DISPLAY_NAME_MAX_LENGTH - 1) // 2
    ja_budget = DISPLAY_NAME_MAX_LENGTH - 1 - cn_budget
    return f"{_truncate_preserving_tokens(cn, cn_budget)}{PRODUCT_NAME_SEPARATOR}{_truncate_preserving_tokens(ja, ja_budget)}"


def _truncate_preserving_tokens(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    tokens = list(dict.fromkeys(match.group(0).strip() for match in IMPORTANT_TOKEN_PATTERN.finditer(value)))
    suffix = " ".join(tokens[-4:])
    if suffix and len(suffix) < limit - 3:
        return f"{value[:limit - len(suffix) - 2].rstrip()}… {suffix}"[:limit]
    return value[:limit - 1].rstrip() + "…"


def _sku_date(value: datetime | None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(TOKYO).strftime("%Y%m%d")


def assign_pending_internal_skus(session: Session) -> None:
    from app.models import Product

    pending = [item for item in session.new if isinstance(item, Product) and not item.internal_sku]
    grouped: dict[str, list[Product]] = defaultdict(list)
    for product in pending:
        grouped[_sku_date(product.created_at)].append(product)
    for day, products in grouped.items():
        prefix = f"NJ-{day}-"
        current = session.scalar(select(func.max(Product.internal_sku)).where(Product.internal_sku.like(f"{prefix}%")))
        sequence = int(current.rsplit("-", 1)[1]) if current and INTERNAL_SKU_PATTERN.fullmatch(current) else 0
        for product in products:
            sequence += 1
            product.internal_sku = f"{prefix}{sequence:06d}"


def install_product_identity_events() -> None:
    global _events_installed
    if _events_installed:
        return
    event.listen(Session, "before_flush", _assign_before_flush)
    _events_installed = True


def _assign_before_flush(session: Session, _flush_context, _instances) -> None:
    assign_pending_internal_skus(session)


def assert_jan_available(session: Session, jan: str | None, product_id: int | None = None) -> str | None:
    from app.models import Product

    jan = normalize_optional_identifier(jan)
    if not jan:
        return None
    query = select(Product).where(Product.jan == jan)
    if product_id is not None:
        query = query.where(Product.id != product_id)
    conflict = session.scalar(query)
    if conflict:
        raise ValueError(f"JAN {jan} 已被商品 {conflict.internal_sku} 使用，不能重复保存")
    return jan


def assert_qinsi_code_available(session: Session, code: str | None, product_id: int | None = None) -> str | None:
    from app.models import Product

    code = normalize_optional_identifier(code)
    if not code:
        return None
    query = select(Product).where(Product.qinsi_product_code == code)
    if product_id is not None:
        query = query.where(Product.id != product_id)
    conflict = session.scalar(query)
    if conflict:
        raise ValueError(f"秦丝商品编码 {code} 已被商品 {conflict.internal_sku} 使用，不能重复保存")
    return code


def update_product_identifiers(session: Session, product, *, jan: str | None, qinsi_product_code: str | None) -> None:
    original_sku = product.internal_sku
    product.jan = assert_jan_available(session, jan, product.id)
    product.qinsi_product_code = assert_qinsi_code_available(session, qinsi_product_code, product.id)
    product.internal_sku = original_sku


def create_product_record(
    session: Session, *, name_cn: str, jan: str | None = None, qinsi_product_code: str | None = None,
    product_origin: str = "manual", source: str = "manual",
):
    from app.models import Product

    product = Product(
        jan=assert_jan_available(session, jan),
        qinsi_product_code=assert_qinsi_code_available(session, qinsi_product_code),
        name_cn=normalize_product_name(name_cn, "中文名"),
        display_name=format_product_display_name(name_cn, None),
        product_data_confirmed=True, name_locked=True,
        product_origin=product_origin, source=source,
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product
