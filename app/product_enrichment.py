from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import PRODUCT_IMAGE_DIR, PROJECT_ROOT
from app.models import (
    PriceLookupHistory, PriceSearchRun, Product, ProductEnrichmentCandidate,
    ProductEnrichmentSource, ProductEnrichmentTask, ProductMatchLog,
    ProductOffer, ProductTranslationCache, ReceiptItem,
)
from app.product_identity import assert_jan_available, format_product_display_name
from app.product_matching import validate_jan


ACTIVE_OR_SUCCESS_STATUSES = {"pending", "running", "completed", "completed_with_warnings", "needs_review"}
CAPACITY_PATTERN = re.compile(r"(?i)(\d+(?:\.\d+)?\s*(?:ml|l|g|kg|錠|粒|枚))")
PACKAGE_PATTERN = re.compile(r"(?i)(\d+\s*(?:個|本|袋|包|箱|セット|パック))")
MODEL_PATTERN = re.compile(r"(?i)\b(?=[A-Z0-9_-]*[A-Z])(?=[A-Z0-9_-]*\d)[A-Z0-9][A-Z0-9_-]{2,24}\b")
COLOR_WORDS = ("ブラック", "ホワイト", "レッド", "ブルー", "グリーン", "ピンク", "パープル", "黒", "白", "赤", "青", "緑", "粉色", "黑色", "白色")
SEVERE_WARNING_PATTERN = re.compile(r"冲突|不一致|编造|不同规格|严重", re.IGNORECASE)
SUMMARY_KEYS = {
    "brand", "brandName", "manufacturer", "maker", "category", "categoryName",
    "model", "modelNumber", "color", "capacity", "size", "janCode", "shopName", "seller",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class EnrichmentSettings:
    enabled: bool
    deepseek_enabled: bool
    image_download_enabled: bool
    auto_create_enabled: bool
    max_translation_batch: int
    max_retries: int
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str


def get_enrichment_settings() -> EnrichmentSettings:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return EnrichmentSettings(
        enabled=_env_bool("JBA_PRODUCT_ENRICHMENT_ENABLED", True),
        deepseek_enabled=_env_bool("JBA_DEEPSEEK_ENABLED", bool(key)) and bool(key),
        image_download_enabled=_env_bool("JBA_PRODUCT_IMAGE_DOWNLOAD_ENABLED", True),
        auto_create_enabled=_env_bool("JBA_AUTO_CREATE_PRODUCT_ENABLED", False),
        max_translation_batch=_env_int("JBA_MAX_TRANSLATION_BATCH", 20, 1, 100),
        max_retries=_env_int("JBA_ENRICHMENT_MAX_RETRIES", 3, 0, 10),
        deepseek_api_key=key,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
    )


class DeepSeekProductName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name_cn: str = Field(min_length=1, max_length=128)
    name_ja: str = Field(min_length=1, max_length=128)
    brand_cn: str = Field(default="", max_length=128)
    category_cn: str = Field(default="", max_length=128)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


def _json_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _safe_summary(raw_json: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in SUMMARY_KEYS:
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            summary[key] = str(value).strip()[:255]
    return summary


def _first(summary: dict[str, Any], *keys: str) -> str | None:
    return next((str(summary[key]).strip() for key in keys if summary.get(key)), None)


def _title_fields(title: str, summary: dict[str, Any]) -> dict[str, str | None]:
    capacity_match = CAPACITY_PATTERN.search(title)
    package_match = PACKAGE_PATTERN.search(title)
    model_match = MODEL_PATTERN.search(title)
    color = _first(summary, "color") or next((word for word in COLOR_WORDS if word in title), None)
    capacity = _first(summary, "capacity", "size") or (capacity_match.group(1).replace(" ", "") if capacity_match else None)
    package_count = package_match.group(1).replace(" ", "") if package_match else None
    specification = " ".join(filter(None, (capacity, package_count))) or None
    return {
        "brand": _first(summary, "brand", "brandName"),
        "manufacturer": _first(summary, "manufacturer", "maker"),
        "category": _first(summary, "category", "categoryName"),
        "capacity": capacity,
        "package_count": package_count,
        "color": color,
        "model_number": _first(summary, "model", "modelNumber") or (model_match.group(0) if model_match else None),
        "specification": specification,
    }


def _task_query(jan: str):
    return select(ProductEnrichmentTask).where(
        ProductEnrichmentTask.jan == jan,
        ProductEnrichmentTask.status.in_(ACTIVE_OR_SUCCESS_STATUSES),
    ).options(selectinload(ProductEnrichmentTask.sources), selectinload(ProductEnrichmentTask.candidates))


def ensure_enrichment_task(
    session: Session, jan: str | None, trigger_source: str, *,
    source_type: str | None = None, source_id: int | None = None, commit: bool = True,
) -> ProductEnrichmentTask | None:
    settings = get_enrichment_settings()
    jan = (jan or "").strip()
    if not settings.enabled or not validate_jan(jan):
        return None
    existing_product = session.scalar(select(Product).where(Product.jan == jan))
    if existing_product is not None:
        return None
    task = session.scalar(_task_query(jan))
    if task is None:
        task = ProductEnrichmentTask(jan=jan, trigger_source=trigger_source)
        session.add(task)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            task = session.scalar(_task_query(jan))
            if task is None:
                raise
    existing_source = None
    if source_type and source_id is not None:
        existing_source = session.scalar(select(ProductEnrichmentSource.id).where(
            ProductEnrichmentSource.task_id == task.id,
            ProductEnrichmentSource.source_type == source_type,
            ProductEnrichmentSource.source_id == source_id,
        ))
    if source_type and source_id is not None and existing_source is None:
        session.add(ProductEnrichmentSource(task_id=task.id, source_type=source_type, source_id=source_id))
    if commit:
        session.commit()
        session.refresh(task)
        session.expire(task, ["sources"])
    return task


def ensure_receipt_item_tasks(session: Session, items: list[ReceiptItem], trigger_source: str, *, commit: bool = True) -> list[ProductEnrichmentTask]:
    tasks: dict[int, ProductEnrichmentTask] = {}
    for item in items:
        task = ensure_enrichment_task(
            session, item.jan_candidate, trigger_source,
            source_type="receipt_item", source_id=item.id, commit=False,
        )
        if task is not None:
            tasks[task.id] = task
    if commit and tasks:
        session.commit()
    return list(tasks.values())


def attach_lookup_source(session: Session, task: ProductEnrichmentTask, history_id: int) -> None:
    if not session.scalar(select(ProductEnrichmentSource).where(
        ProductEnrichmentSource.task_id == task.id,
        ProductEnrichmentSource.source_type == "price_lookup",
        ProductEnrichmentSource.source_id == history_id,
    )):
        session.add(ProductEnrichmentSource(task_id=task.id, source_type="price_lookup", source_id=history_id))


def _candidate_from_offer(task: ProductEnrichmentTask, offer: ProductOffer) -> ProductEnrichmentCandidate | None:
    if offer.jan != task.jan or offer.jan_match_status != "exact":
        return None
    condition = (offer.condition or "").casefold()
    if condition in {"used", "中古", "second_hand"} or offer.is_subscription or offer.listing_type != "single":
        return None
    summary = _safe_summary(offer.raw_data_json)
    fields = _title_fields(offer.title or "", summary)
    warnings = _json_list(None)
    if offer.spec_match_status == "suspected_mismatch":
        warnings.append("规格疑似不一致")
    score = .55 + (.1 if offer.image_url else 0) + (.1 if offer.shipping_known else 0) + (.1 if offer.stock_status != "out_of_stock" else 0)
    return ProductEnrichmentCandidate(
        task_id=task.id, jan=task.jan, name_ja=(offer.title or "").strip() or None,
        image_url=offer.image_url, source_url=offer.url, platform=offer.marketplace.code,
        item_price=offer.item_price, shipping_price=offer.shipping_price,
        total_price=offer.total_price, fetched_at=offer.fetched_at, score=min(score, 1.0),
        warnings_json=json.dumps(warnings, ensure_ascii=False) if warnings else None,
        provider_summary_json=json.dumps(summary, ensure_ascii=False), **fields,
    )


def aggregate_candidates(session: Session, task: ProductEnrichmentTask, run: PriceSearchRun) -> list[ProductEnrichmentCandidate]:
    session.execute(delete(ProductEnrichmentCandidate).where(ProductEnrichmentCandidate.task_id == task.id))
    session.flush()
    candidates = [candidate for offer in run.offers if (candidate := _candidate_from_offer(task, offer)) is not None]
    image_counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.image_url:
            image_counts[candidate.image_url] = image_counts.get(candidate.image_url, 0) + 1
    for candidate in candidates:
        if candidate.image_url and image_counts.get(candidate.image_url, 0) > 1:
            candidate.score = min(1.0, candidate.score + .1)
        session.add(candidate)
    session.flush()
    if candidates:
        def selection_priority(item: ProductEnrichmentCandidate):
            source_text = f"{item.name_ja or ''} {item.source_url}".casefold()
            official = any(marker in source_text for marker in ("公式", "official", (item.brand or "").casefold(), (item.manufacturer or "").casefold()) if marker)
            repeated_image = bool(item.image_url and image_counts.get(item.image_url, 0) > 1)
            return official, repeated_image, item.score, bool(item.image_url), item.total_price is not None, -item.id
        selected = max(candidates, key=selection_priority)
        selected.selected = True
    task.provider_codes_json = run.provider_summary_json
    session.flush()
    return candidates


def _latest_run(session: Session, jan: str) -> PriceSearchRun | None:
    return session.scalar(
        select(PriceSearchRun).where(PriceSearchRun.jan == jan, PriceSearchRun.status == "completed")
        .options(selectinload(PriceSearchRun.offers).selectinload(ProductOffer.marketplace))
        .order_by(PriceSearchRun.completed_at.desc(), PriceSearchRun.id.desc()).limit(1)
    )


def _parse_json_object(value: str) -> dict[str, Any]:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("DeepSeek response is not a JSON object")
    return parsed


def _append_required(value: str, required: list[str], limit: int = 128) -> str:
    missing = [item for item in required if item.casefold() not in value.casefold()]
    if not missing:
        return value[:limit]
    suffix = " ".join(dict.fromkeys(missing))
    if len(suffix) >= limit - 2:
        return suffix[:limit]
    return f"{value[:limit - len(suffix) - 1].rstrip()} {suffix}".strip()


def translate_candidate(
    session: Session, task: ProductEnrichmentTask, candidate: ProductEnrichmentCandidate,
    *, client: Any | None = None, settings: EnrichmentSettings | None = None,
) -> DeepSeekProductName | None:
    settings = settings or get_enrichment_settings()
    if not settings.deepseek_enabled or not settings.deepseek_api_key:
        task.deepseek_status = "pending_configuration"
        return None
    name_ja = (candidate.name_ja or "").strip()
    if not name_ja:
        task.deepseek_status = "skipped_no_name"
        return None
    name_hash = hashlib.sha256(name_ja.encode("utf-8")).hexdigest()
    task.deepseek_name_key = name_hash
    cached = session.scalar(select(ProductTranslationCache).where(
        ProductTranslationCache.jan == task.jan, ProductTranslationCache.name_ja_hash == name_hash,
    ))
    if cached is not None:
        task.deepseek_status = "completed_cached"
        return DeepSeekProductName.model_validate_json(cached.response_json)
    structured = {
        "jan": task.jan, "name_ja": name_ja, "brand": candidate.brand,
        "manufacturer": candidate.manufacturer, "category": candidate.category,
        "specification": candidate.specification, "capacity": candidate.capacity,
        "color": candidate.color, "model_number": candidate.model_number,
        "package_count": candidate.package_count,
    }
    prompt = (
        "根据输入的结构化日本商品字段生成中文商品名。只输出合法 JSON，键必须为 "
        "name_cn,name_ja,brand_cn,category_cn,confidence,warnings。不得编造；不确定的专有名词保留原文；"
        "型号、数字、容量、尺寸、颜色和数量不得丢失；name_cn/name_ja 各不超过128字符。\n"
        + json.dumps(structured, ensure_ascii=False)
    )
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": "你是日本商品主数据翻译助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"}
    try:
        if client is None:
            with httpx.Client(timeout=35) as http:
                response = http.post(f"{settings.deepseek_base_url}/chat/completions", headers=headers, json=payload)
        else:
            response = client.post(f"{settings.deepseek_base_url}/chat/completions", headers=headers, json=payload, timeout=35)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = DeepSeekProductName.model_validate(_parse_json_object(str(content)))
        required = [
            value for value in (candidate.model_number, candidate.capacity, candidate.color, candidate.package_count)
            if value
        ]
        name_cn = _append_required(result.name_cn, required)
        name_ja = result.name_ja
        if any(value.casefold() not in name_ja.casefold() for value in required):
            name_ja = _append_required(name_ja, required)
        result = result.model_copy(update={"name_cn": name_cn, "name_ja": name_ja})
    except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        task.deepseek_status = "failed"
        task.last_error = f"DeepSeek {type(exc).__name__}"
        return None
    session.add(ProductTranslationCache(
        jan=task.jan, name_ja_hash=name_hash, name_ja=name_ja,
        response_json=result.model_dump_json(),
    ))
    task.deepseek_status = "completed"
    session.flush()
    return result


def translate_pending_batch(session: Session, *, client: Any | None = None) -> int:
    settings = get_enrichment_settings()
    tasks = list(session.scalars(
        select(ProductEnrichmentTask).where(
            ProductEnrichmentTask.status.in_({"pending", "running", "needs_review"}),
            ProductEnrichmentTask.deepseek_status.in_({"pending", "failed", "pending_configuration"}),
        ).options(selectinload(ProductEnrichmentTask.candidates)).limit(settings.max_translation_batch)
    ))
    completed = 0
    for task in tasks:
        selected = next((item for item in task.candidates if item.selected), None)
        if selected and translate_candidate(session, task, selected, client=client, settings=settings):
            completed += 1
    session.commit()
    return completed


def _image_extension(image: Image.Image) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get((image.format or "").upper(), ".img")


def download_main_image(
    task: ProductEnrichmentTask, candidate: ProductEnrichmentCandidate, *, client: Any | None = None,
) -> dict[str, str] | None:
    if not candidate.image_url:
        task.image_status = "missing"
        return None
    try:
        if client is None:
            with httpx.Client(timeout=20, follow_redirects=True) as http:
                response = http.get(candidate.image_url)
        else:
            response = client.get(candidate.image_url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        content = response.content
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError("invalid image size")
        image = Image.open(io.BytesIO(content))
        image.verify()
        image = Image.open(io.BytesIO(content))
        if image.width < 200 or image.height < 200:
            raise ValueError("image dimensions too small")
        digest = hashlib.sha256(content).hexdigest()
        directory = Path(PRODUCT_IMAGE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{task.jan}-{digest[:16]}{_image_extension(image)}"
        if not path.exists():
            path.write_bytes(content)
        try:
            relative = path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except ValueError:
            relative = path.resolve().as_posix()
        task.image_status = "completed"
        return {"path": relative, "url": candidate.image_url, "platform": candidate.platform, "hash": digest}
    except (httpx.HTTPError, OSError, ValueError) as exc:
        task.image_status = "failed_remote_available"
        task.last_error = f"Image {type(exc).__name__}"
        return None


def _selected_payload(candidate: ProductEnrichmentCandidate, translation: DeepSeekProductName | None, image: dict[str, str] | None) -> dict[str, Any]:
    payload = {
        "jan": candidate.jan, "name_ja": candidate.name_ja, "brand": candidate.brand,
        "manufacturer": candidate.manufacturer, "category": candidate.category,
        "specification": candidate.specification, "capacity": candidate.capacity,
        "color": candidate.color, "model_number": candidate.model_number,
        "package_count": candidate.package_count, "image_url": candidate.image_url,
        "source_url": candidate.source_url, "platform": candidate.platform,
        "item_price": candidate.item_price, "shipping_price": candidate.shipping_price,
        "total_price": candidate.total_price, "fetched_at": candidate.fetched_at.isoformat(),
    }
    if translation:
        payload["translation"] = translation.model_dump()
    if image:
        payload["local_image"] = image
    return payload


def _spec_conflict(candidates: list[ProductEnrichmentCandidate]) -> bool:
    capacities = {(item.capacity or "").casefold() for item in candidates}
    packages = {(item.package_count or "").casefold() for item in candidates}
    colors = {item.color.casefold() for item in candidates if item.color}
    models = {item.model_number.casefold() for item in candidates if item.model_number}
    refill_flags = {bool(re.search(r"詰め替え|詰替|レフィル|refill", item.name_ja or "", re.IGNORECASE)) for item in candidates}
    return len(capacities) > 1 or len(packages) > 1 or len(colors) > 1 or len(models) > 1 or len(refill_flags) > 1


def _confidence(candidates: list[ProductEnrichmentCandidate], translation: DeepSeekProductName | None, image_available: bool, conflict: bool) -> float:
    if not candidates:
        return 0.0
    value = .45 + .1
    if len(candidates) >= 2:
        value += .1
    if not conflict:
        value += .1
    if translation:
        value += .15 * translation.confidence
    if image_available:
        value += .1
    return round(min(value, 1.0), 3)


def _bind_sources(session: Session, task: ProductEnrichmentTask, product: Product) -> None:
    receipts = set()
    sources = list(session.scalars(select(ProductEnrichmentSource).where(ProductEnrichmentSource.task_id == task.id)))
    for source in sources:
        if source.source_type != "receipt_item":
            continue
        item = session.get(ReceiptItem, source.source_id)
        if item is None:
            continue
        old_id = item.product_id
        if old_id not in {None, product.id}:
            continue
        item.product_id = product.id
        item.match_status = "matched_existing" if old_id == product.id else "new_product"
        item.match_method = "auto_enrichment"
        item.match_confidence = task.confidence
        item.matched_at = datetime.now(timezone.utc)
        session.add(ProductMatchLog(
            receipt_item_id=item.id, old_product_id=old_id, new_product_id=product.id,
            method="auto_enrichment", decision="new_product" if old_id is None else "matched_existing",
        ))
        if item.receipt.confirmation_status == "confirmed":
            receipts.add(item.receipt)
    session.flush()
    if receipts:
        from app.purchase_service import ensure_purchase_batch_for_receipt
        for receipt in receipts:
            ensure_purchase_batch_for_receipt(session, receipt)


def create_product_from_task(
    session: Session, task: ProductEnrichmentTask, *, manual: bool = False,
    name_cn: str | None = None, name_ja: str | None = None,
) -> Product:
    existing = session.scalar(select(Product).where(Product.jan == task.jan))
    if existing is not None:
        task.product_id = existing.id
        _bind_sources(session, task, existing)
        return existing
    payload = json.loads(task.selected_data_json or "{}")
    translation = payload.get("translation") or {}
    chosen_cn = (name_cn or translation.get("name_cn") or "").strip()
    chosen_ja = (name_ja or translation.get("name_ja") or payload.get("name_ja") or "").strip()
    if not chosen_cn or not chosen_ja:
        raise ValueError("创建商品前必须确认中日文名称")
    product = Product(
        jan=assert_jan_available(session, task.jan), name_cn=chosen_cn[:128], name_ja=chosen_ja[:128],
        display_name=format_product_display_name(chosen_cn[:128], chosen_ja[:128]),
        brand=(translation.get("brand_cn") or payload.get("brand") or None),
        manufacturer=payload.get("manufacturer"), category=(translation.get("category_cn") or payload.get("category") or None),
        specification=payload.get("specification"), capacity=payload.get("capacity"), color=payload.get("color"),
        model_number=payload.get("model_number"), package_count=payload.get("package_count"),
        main_image_path=(payload.get("local_image") or {}).get("path"),
        main_image_source_url=payload.get("image_url"),
        main_image_source_platform=payload.get("platform"),
        main_image_hash=(payload.get("local_image") or {}).get("hash"),
        main_image_downloaded_at=datetime.now(timezone.utc) if payload.get("local_image") else None,
        product_data_confirmed=manual, name_locked=manual, main_image_locked=manual and bool(payload.get("image_url")),
        purchase_price=payload.get("item_price"), source="product_enrichment", product_origin="enrichment",
    )
    session.add(product)
    session.flush()
    task.product_id = product.id
    _bind_sources(session, task, product)
    return product


def process_enrichment_task(
    session: Session, task: ProductEnrichmentTask, *, providers=None,
    deepseek_client: Any | None = None, image_client: Any | None = None,
    force: bool = False,
) -> ProductEnrichmentTask:
    settings = get_enrichment_settings()
    if not settings.enabled:
        return task
    if task.retry_count >= settings.max_retries and not force:
        task.status = "failed"
        task.last_error = "已达到最大重试次数"
        session.commit()
        return task
    existing = session.scalar(select(Product).where(Product.jan == task.jan))
    if existing is not None:
        task.product_id = existing.id
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)
        _bind_sources(session, task, existing)
        session.commit()
        return task
    task.status = "running"
    task.last_error = None
    session.flush()
    warnings: list[str] = []
    try:
        previous_payload = json.loads(task.selected_data_json or "{}")
        candidates = list(session.scalars(
            select(ProductEnrichmentCandidate).where(ProductEnrichmentCandidate.task_id == task.id)
            .order_by(ProductEnrichmentCandidate.score.desc(), ProductEnrichmentCandidate.id)
        )) if force else []
        if not candidates:
            run = _latest_run(session, task.jan)
            if run is None or providers is not None:
                from app.price_service import query_prices
                from app.schemas import PriceLookupInput
                view = query_prices(session, PriceLookupInput(jan=task.jan, force_refresh=providers is not None), providers=providers)
                run = view.run
                attach_lookup_source(session, task, view.history.id)
            candidates = aggregate_candidates(session, task, run)
        if not candidates:
            warnings.append("Provider 未返回 JAN 精确一致的可信新品候选")
            task.status = "needs_review"
            task.confidence = 0
            task.deepseek_status = "skipped_no_candidate"
            task.image_status = "missing"
            task.warnings_json = json.dumps(warnings, ensure_ascii=False)
            session.commit()
            return task
        selected = next(item for item in candidates if item.selected)
        conflict = _spec_conflict(candidates)
        if conflict:
            warnings.append("候选核心容量或套装规格冲突")
        previous_translation = previous_payload.get("translation")
        if task.deepseek_status in {"completed", "completed_cached"} and previous_translation:
            translation = DeepSeekProductName.model_validate(previous_translation)
        else:
            translation = translate_candidate(session, task, selected, client=deepseek_client, settings=settings)
        if translation is None:
            warnings.append("DeepSeek 未完成，保留待处理状态")
        else:
            warnings.extend(translation.warnings)
        image = previous_payload.get("local_image") if task.image_status == "completed" else None
        if settings.image_download_enabled:
            if image is None:
                image = download_main_image(task, selected, client=image_client)
            if image is None and selected.image_url:
                warnings.append("主图下载失败，已保留远程 URL")
        else:
            task.image_status = "disabled"
        payload = _selected_payload(selected, translation, image)
        task.selected_data_json = json.dumps(payload, ensure_ascii=False)
        task.confidence = _confidence(candidates, translation, bool(image or selected.image_url), conflict)
        severe = conflict or any(SEVERE_WARNING_PATTERN.search(item) for item in (translation.warnings if translation else []))
        high_confidence = (
            task.confidence >= .85 and not severe and translation is not None
            and bool((translation.name_ja or selected.name_ja).strip()) and bool(image or selected.image_url)
        )
        if high_confidence and settings.auto_create_enabled:
            create_product_from_task(session, task)
            task.status = "completed_with_warnings" if warnings else "completed"
            task.completed_at = datetime.now(timezone.utc)
        else:
            if high_confidence and not settings.auto_create_enabled:
                warnings.append("高置信度自动建品开关关闭，等待人工批量接受")
            task.status = "needs_review"
        task.warnings_json = json.dumps(list(dict.fromkeys(warnings)), ensure_ascii=False) if warnings else None
        session.commit()
        session.refresh(task)
        return task
    except Exception as exc:
        session.rollback()
        task = session.get(ProductEnrichmentTask, task.id)
        task.retry_count += 1
        task.status = "failed" if task.retry_count >= settings.max_retries else "needs_review"
        task.last_error = f"{type(exc).__name__}"
        task.warnings_json = json.dumps(["商品资料丰富化失败，主流程未受影响"], ensure_ascii=False)
        session.commit()
        return task


def safe_trigger_receipt_items(session: Session, items: list[ReceiptItem], trigger_source: str) -> list[ProductEnrichmentTask]:
    try:
        tasks = ensure_receipt_item_tasks(session, items, trigger_source)
        for task in tasks:
            process_enrichment_task(session, task)
        return tasks
    except Exception:
        session.rollback()
        return []


def accept_task(
    session: Session, task_id: int, *, name_cn: str | None = None,
    name_ja: str | None = None, candidate_id: int | None = None,
) -> Product:
    task = get_task(session, task_id)
    if candidate_id is not None:
        candidate = next((item for item in task.candidates if item.id == candidate_id), None)
        if candidate is None:
            raise ValueError("主图候选不存在")
        for item in task.candidates:
            item.selected = item.id == candidate_id
        payload = json.loads(task.selected_data_json or "{}")
        payload.update(_selected_payload(candidate, None, None))
        task.selected_data_json = json.dumps(payload, ensure_ascii=False)
    product = create_product_from_task(session, task, manual=True, name_cn=name_cn, name_ja=name_ja)
    task.status = "completed_with_warnings" if _json_list(task.warnings_json) else "completed"
    task.completed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(product)
    return product


def bind_task_to_existing(session: Session, task_id: int, product_id: int) -> Product:
    task = get_task(session, task_id)
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("商品不存在")
    _bind_sources(session, task, product)
    task.product_id = product.id
    task.status = "completed"
    task.completed_at = datetime.now(timezone.utc)
    session.commit()
    return product


def get_task(session: Session, task_id: int) -> ProductEnrichmentTask:
    task = session.scalar(
        select(ProductEnrichmentTask).where(ProductEnrichmentTask.id == task_id)
        .options(selectinload(ProductEnrichmentTask.candidates), selectinload(ProductEnrichmentTask.sources))
    )
    if task is None:
        raise LookupError("商品丰富化任务不存在")
    return task


def list_review_tasks(session: Session) -> list[ProductEnrichmentTask]:
    return list(session.scalars(
        select(ProductEnrichmentTask).where(ProductEnrichmentTask.status.in_({"needs_review", "failed", "pending"}))
        .options(selectinload(ProductEnrichmentTask.candidates))
        .order_by(ProductEnrichmentTask.created_at.desc(), ProductEnrichmentTask.id.desc())
    ))


def enrichment_summary_for_receipt(session: Session, receipt_id: int) -> dict[str, int]:
    rows = session.execute(
        select(ProductEnrichmentTask.status, func.count(func.distinct(ProductEnrichmentTask.id)))
        .join(ProductEnrichmentSource, ProductEnrichmentSource.task_id == ProductEnrichmentTask.id)
        .join(ReceiptItem, ReceiptItem.id == ProductEnrichmentSource.source_id)
        .where(ProductEnrichmentSource.source_type == "receipt_item", ReceiptItem.receipt_id == receipt_id)
        .group_by(ProductEnrichmentTask.status)
    ).all()
    counts = dict(rows)
    return {
        "completed": counts.get("completed", 0) + counts.get("completed_with_warnings", 0),
        "review": counts.get("needs_review", 0) + counts.get("pending", 0) + counts.get("running", 0),
        "failed": counts.get("failed", 0),
    }
