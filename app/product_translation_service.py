from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.config import get_deepseek_config, is_deepseek_configured
from app.deepseek_service import DeepSeekServiceError, translate_name_with_deepseek
from app.product_identity import format_product_display_name, normalize_product_name_whitespace


MISSING_CN_PLACEHOLDER = "中文名待补"
LEGACY_MISSING_CN_PLACEHOLDERS = {
    MISSING_CN_PLACEHOLDER,
    "中文名待补全",
    "缺中文名",
    "笢恅靡渾硃",
    "笢恅待补",
}
GIBBERISH_CN_MARKERS = set("笢恅靡渾硃髪斌瞳針赤匿廾圻龍院塞滅")
TRANSLATABLE_STATUSES = {"new_pending_completion", "new_pending_review", "pending_qinsi_product_import"}
ATTEMPT_COOLDOWN = timedelta(minutes=10)
MAX_PRODUCT_DISPLAY_LENGTH = 128
_recent_attempts: dict[tuple[int, str], datetime] = {}


@dataclass(frozen=True, slots=True)
class ProductTranslationResult:
    status: str
    product_id: int | None = None
    before_name: str | None = None
    after_name: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProductTranslationBatchResult:
    candidate_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    stopped_reason: str | None
    examples: tuple[dict[str, str | int | None], ...]


def clean_missing_cn_placeholder(product: Product) -> None:
    if (product.name_cn or "").strip() in LEGACY_MISSING_CN_PLACEHOLDERS:
        product.name_cn = None
        if product.name_ja:
            product.display_name = (normalize_product_name_whitespace(product.name_ja) or "")[:MAX_PRODUCT_DISPLAY_LENGTH]


def is_invalid_chinese_name(value: str | None) -> bool:
    text = (value or "").strip()
    return not text or text in LEGACY_MISSING_CN_PLACEHOLDERS or sum(1 for char in text if char in GIBBERISH_CN_MARKERS) >= 2


def is_missing_chinese_name(product: Product) -> bool:
    return bool(
        (product.name_ja or "").strip()
        and (product.name_ja or "").strip() != "缺商品"
        and is_invalid_chinese_name(product.name_cn)
    )


def product_display_label(product: Product) -> str:
    name_cn = None if (product.name_cn or "").strip() in LEGACY_MISSING_CN_PLACEHOLDERS else normalize_product_name_whitespace(product.name_cn)
    name_ja = normalize_product_name_whitespace(product.name_ja)
    if name_cn and name_ja:
        return format_product_display_name(name_cn, name_ja)
    if name_ja:
        return name_ja
    if name_cn:
        return name_cn
    return f"{product.jan or product.internal_sku}|缺商品"


def missing_chinese_name_query(include_imported: bool = False):
    query = select(Product).where(
        Product.name_ja.is_not(None),
        Product.name_ja != "",
        Product.name_ja != "缺商品",
    )
    if include_imported:
        query = query.where(Product.status.in_(TRANSLATABLE_STATUSES | {"qinsi_product_imported"}))
    else:
        query = query.where(Product.status.in_(TRANSLATABLE_STATUSES))
    query = query.where(
        (Product.name_cn.is_(None))
        | (Product.name_cn == "")
        | (Product.name_cn.in_(LEGACY_MISSING_CN_PLACEHOLDERS))
    )
    return query.order_by(Product.updated_at.desc(), Product.id.desc())


def count_missing_chinese_name_products(session: Session) -> int:
    return len(list(session.scalars(missing_chinese_name_query())))


def _name_hash(name_ja: str) -> str:
    return hashlib.sha256(name_ja.encode("utf-8")).hexdigest()


def _should_skip_recent_attempt(product: Product, name_hash: str, *, force: bool) -> bool:
    if force:
        return False
    last = _recent_attempts.get((product.id, name_hash))
    return bool(last and datetime.now(timezone.utc) - last < ATTEMPT_COOLDOWN)


def _mark_attempt(product: Product, name_hash: str) -> None:
    _recent_attempts[(product.id, name_hash)] = datetime.now(timezone.utc)


def translate_product_chinese_name(
    session: Session,
    product: Product,
    *,
    client: Any | None = None,
    force: bool = False,
    overwrite_existing: bool = False,
) -> ProductTranslationResult:
    clean_missing_cn_placeholder(product)
    before_name = product_display_label(product)
    if not is_invalid_chinese_name(product.name_cn) and not overwrite_existing:
        return ProductTranslationResult("skipped", product.id, before_name, before_name, "已有中文名")
    name_ja = normalize_product_name_whitespace(product.name_ja) or ""
    if not name_ja or name_ja == "缺商品":
        return ProductTranslationResult("skipped", product.id, before_name, before_name, "缺少可翻译日文名")
    if not is_deepseek_configured():
        return ProductTranslationResult("skipped", product.id, before_name, before_name, "DeepSeek未配置")
    name_hash = _name_hash(name_ja)
    if _should_skip_recent_attempt(product, name_hash, force=force):
        return ProductTranslationResult("skipped", product.id, before_name, before_name, "短时间内已尝试")
    try:
        result = translate_name_with_deepseek(name_ja, client=client, config=get_deepseek_config(), timeout_seconds=10)
    except DeepSeekServiceError as exc:
        _mark_attempt(product, name_hash)
        if exc.category == "rate_limited":
            return ProductTranslationResult("rate_limited", product.id, before_name, before_name, exc.message)
        status = "skipped" if exc.category == "unconfigured" else "failed"
        return ProductTranslationResult(status, product.id, before_name, before_name, exc.message)
    if is_invalid_chinese_name(result.name_cn):
        _mark_attempt(product, name_hash)
        return ProductTranslationResult("failed", product.id, before_name, before_name, "DeepSeek输出疑似历史乱码")
    product.name_cn = (normalize_product_name_whitespace(result.name_cn) or "")[:128]
    product.name_ja = name_ja[:128]
    product.display_name = format_product_display_name(product.name_cn, product.name_ja)
    product.product_data_confirmed = False
    if product.status == "new_pending_completion":
        product.status = "new_pending_review"
    after_name = product_display_label(product)
    session.flush()
    return ProductTranslationResult("success", product.id, before_name, after_name)


def auto_translate_new_product_once(
    session: Session,
    product: Product,
    *,
    client: Any | None = None,
) -> ProductTranslationResult:
    if product.status == "qinsi_product_imported" or product.qinsi_product_code:
        return ProductTranslationResult("skipped", product.id, product_display_label(product), product_display_label(product), "已导入秦丝")
    if not is_missing_chinese_name(product):
        return ProductTranslationResult("skipped", product.id, product_display_label(product), product_display_label(product), "已有有效中文名")
    if not is_deepseek_configured():
        return ProductTranslationResult("skipped", product.id, product_display_label(product), product_display_label(product), "DeepSeek未配置")
    return translate_product_chinese_name(session, product, client=client, force=True, overwrite_existing=False)


def translate_missing_chinese_names(
    session: Session,
    *,
    product_ids: set[int] | None = None,
    client: Any | None = None,
    force: bool = False,
    limit: int = 50,
) -> ProductTranslationBatchResult:
    query = missing_chinese_name_query()
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    products = list(session.scalars(query.limit(limit)))
    success = failed = skipped = 0
    stopped_reason = None
    examples: list[dict[str, str | int | None]] = []
    for product in products:
        result = translate_product_chinese_name(session, product, client=client, force=force)
        if result.status == "success":
            success += 1
            if len(examples) < 3:
                examples.append({"product_id": product.id, "before": result.before_name, "after": result.after_name})
        elif result.status == "rate_limited":
            failed += 1
            stopped_reason = result.error or "DeepSeek限流"
            break
        elif result.status == "failed":
            failed += 1
        else:
            skipped += 1
    session.commit()
    return ProductTranslationBatchResult(
        candidate_count=len(products),
        success_count=success,
        failed_count=failed,
        skipped_count=skipped,
        stopped_reason=stopped_reason,
        examples=tuple(examples),
    )


def reset_translation_attempt_cache() -> None:
    _recent_attempts.clear()
