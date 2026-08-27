from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.local_product import resolve_local_product_by_jan
from app.models import (
    Marketplace, PlatformLookupResult, PlatformProviderState, PriceLookupHistory, PriceProviderAttempt, PriceSearchRun,
    Product, ProductOffer, PurchaseBatch, PurchaseBatchItem, Receipt, Store,
)
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse, get_default_price_providers
from app.product_identity import format_product_display_name
from app.schemas import PriceLookupInput


CACHE_TTL = timedelta(minutes=15)
PROVIDER_TIMEOUT_SECONDS = 4.0
MAX_TRUSTED_RESULTS = 3
SUBSCRIPTION_PATTERN = re.compile(r"定期(?:購入|便)|サブスク|subscription", re.IGNORECASE)
MULTIPACK_PATTERN = re.compile(r"(?:[x×*]\s*[2-9]\d*|[2-9]\d*\s*(?:個|本|袋|包|枚|箱|セット|パック))", re.IGNORECASE)
RELIABLE_PACK_PATTERNS = (
    re.compile(r"(?:^|[^\d])([2-9]\d{0,2})\s*(?:個|个|本|袋|包|枚|箱|錠|粒)\s*(?:装|裝|入り|入|セット|パック)", re.IGNORECASE),
    re.compile(r"(?:^|[^\d])([2-9]\d{0,2})\s*(?:セット|パック)", re.IGNORECASE),
    re.compile(r"(?:[x×*]\s*([2-9]\d{0,2})|([2-9]\d{0,2})\s*[x×])", re.IGNORECASE),
)
SPEC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|mL|l|L|g|kg|個|本|枚|袋|包|錠|粒)", re.IGNORECASE)
PROVIDER_SUMMARY_KEYS = {
    "brand", "brandName", "manufacturer", "maker", "category", "categoryName",
    "model", "modelNumber", "color", "capacity", "size", "janCode", "shopName", "seller",
    "specification", "spec_text", "net_weight_g", "volume_ml", "length_mm", "width_mm",
    "height_mm", "depth_mm", "pack_quantity", "source_url", "source_domain", "productImageUrl",
    "originalImageUrl", "price",
}
PROVIDER_INFLIGHT_LOCK = threading.Lock()
PROVIDER_INFLIGHT: dict[tuple[str, str], Future[ProviderResponse]] = {}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriceLookupView:
    history: PriceLookupHistory
    run: PriceSearchRun
    product: Product | None
    product_display_name: str | None
    recent_purchase_price: int | Decimal | None
    historical_lowest_purchase_price: int | Decimal | None
    latest_purchase_store_name: str | None
    latest_purchase_store_address: str | None
    trusted_offers: tuple[ProductOffer, ...]
    incomplete_offers: tuple[ProductOffer, ...]
    flagged_offers: tuple[ProductOffer, ...]
    result_offers: tuple[ProductOffer, ...]
    fallback_results: tuple[PlatformLookupResult, ...]
    attempts: tuple[PriceProviderAttempt, ...]
    current_store_price: int | None
    online_min_price: int | Decimal | None
    difference: int | Decimal | None
    comparison_status: str | None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _marketplace(session: Session, provider: PriceProvider) -> Marketplace:
    marketplace = session.scalar(select(Marketplace).where(Marketplace.code == provider.code))
    if marketplace is None:
        marketplace = Marketplace(code=provider.code, name=provider.display_name, base_url=provider.base_url, active=True)
        session.add(marketplace)
        session.flush()
    return marketplace


def _record_provider_state(
    session: Session,
    provider: PriceProvider,
    response: ProviderResponse,
    *,
    tested_at: datetime,
) -> PlatformProviderState:
    state = session.scalar(
        select(PlatformProviderState).where(PlatformProviderState.provider_code == provider.code)
    )
    if state is None:
        state = PlatformProviderState(provider_code=provider.code)
        session.add(state)
    state.configured = provider.is_configured()
    state.request_count = (state.request_count or 0) + 1
    state.last_tested_at = tested_at
    if response.status == "success":
        state.credentials_valid = True
        state.last_success_at = tested_at
        state.recent_error = None
    elif response.status == "unconfigured":
        state.credentials_valid = None
        state.recent_error = "UNCONFIGURED"
    elif response.status in {"error", "timeout"}:
        if response.error_code == "AUTH_FAILED":
            state.credentials_valid = False
        state.recent_error = response.error_code or response.status.upper()
    else:
        state.recent_error = response.error_code
    return state


def _spec_tokens(value: str | None) -> set[str]:
    return {f"{number.lower()}{unit.lower()}" for number, unit in SPEC_PATTERN.findall(value or "")}


def _spec_status(product: Product | None, candidate: PriceCandidate) -> str:
    if product is None:
        return "unknown"
    local_text = " ".join(filter(None, (product.name_cn, product.name_ja, product.specification, product.model_spec)))
    local_specs = _spec_tokens(local_text)
    offer_specs = _spec_tokens(candidate.title)
    if local_specs and offer_specs and local_specs.isdisjoint(offer_specs):
        return "suspected_mismatch"
    if MULTIPACK_PATTERN.search(candidate.title) and not MULTIPACK_PATTERN.search(local_text):
        return "suspected_mismatch"
    return "matched" if local_specs and offer_specs else "unknown"


def _offer_quality(jan: str, product: Product | None, candidate: PriceCandidate) -> tuple[str, str, bool, list[str]]:
    reasons: list[str] = []
    if candidate.jan == jan and candidate.jan_verified:
        jan_status = "exact"
    elif candidate.jan:
        jan_status = "mismatch" if candidate.jan != jan else "unverified"
        reasons.append("JAN 不一致" if candidate.jan != jan else "JAN 未验证")
    else:
        jan_status = "unverified"
        reasons.append("JAN 未验证")
    condition = (candidate.condition or "unknown").casefold()
    if condition in {"used", "中古", "second_hand"}:
        reasons.append("二手商品")
    if candidate.stock_status == "out_of_stock":
        reasons.append("缺货")
    subscription = candidate.listing_type == "subscription" or bool(SUBSCRIPTION_PATTERN.search(candidate.title))
    if subscription:
        reasons.append("定期购买价格")
    spec_status = _spec_status(product, candidate)
    if spec_status == "suspected_mismatch":
        reasons.append("数量或容量疑似不同")
    if candidate.item_price <= 0:
        reasons.append("商品价格无效")
    if candidate.link_type != "product":
        reasons.append("搜索页不是商品详情页")
    return jan_status, spec_status, subscription, reasons


def _compact_text(value: str | None) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold())


def _dedupe_candidates(provider_code: str, candidates: tuple[PriceCandidate, ...]) -> tuple[PriceCandidate, ...]:
    seen: set[tuple[str, str, str]] = set()
    output: list[PriceCandidate] = []
    for candidate in candidates:
        key = (
            provider_code,
            (candidate.url or "").split("#", 1)[0].rstrip("/"),
            _compact_text(candidate.seller),
        )
        title_key = (
            provider_code,
            _compact_text(candidate.title),
            _compact_text(candidate.seller),
            str(candidate.item_price),
        )
        if key in seen or title_key in seen:
            continue
        seen.add(key)
        seen.add(title_key)
        output.append(candidate)
    return tuple(output)


def _provider_summary(raw_data: dict) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in PROVIDER_SUMMARY_KEYS:
        value = raw_data.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            summary[key] = str(value).strip()[:255]
    return summary


def _collect_image_urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        output: list[str] = []
        for key in (
            "original", "originalUrl", "originalImageUrl", "large", "largeUrl",
            "productImageUrl", "url", "imageUrl", "medium", "small",
        ):
            output.extend(_collect_image_urls(value.get(key)))
        return output
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_collect_image_urls(item))
        return output
    return []


def _provider_image_candidates(raw_data: dict, fallback_url: str | None) -> list[dict[str, str]]:
    buckets: list[tuple[str, object]] = [
        ("provider_original", raw_data.get("originalImageUrl") or raw_data.get("productImageUrl") or raw_data.get("largeImageUrl")),
        ("provider_detail", raw_data.get("exImage") or raw_data.get("mediumImageUrl") or raw_data.get("mediumImageUrls")),
        ("search_thumbnail", raw_data.get("image") or raw_data.get("smallImageUrl") or raw_data.get("smallImageUrls") or fallback_url),
    ]
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, value in buckets:
        for url in _collect_image_urls(value):
            if url and url not in seen:
                seen.add(url)
                output.append({"kind": kind, "url": url})
    return output


def _reliable_pack_quantity(title: str | None, raw_data: dict | None = None) -> int | None:
    raw_data = raw_data or {}
    raw_quantity = raw_data.get("pack_quantity") or raw_data.get("quantity_per_pack") or raw_data.get("lot_quantity")
    if isinstance(raw_quantity, int) and raw_quantity > 1:
        return raw_quantity
    if isinstance(raw_quantity, str) and raw_quantity.isdigit() and int(raw_quantity) > 1:
        return int(raw_quantity)
    for pattern in RELIABLE_PACK_PATTERNS:
        match = pattern.search(title or "")
        if match:
            value = next((item for item in match.groups() if item), None)
            if value and int(value) > 1:
                return int(value)
    return None


def _normalized_unit_price(listing_price: int, pack_quantity: int | None) -> Decimal | None:
    if pack_quantity is None or pack_quantity <= 1:
        return None
    return (Decimal(listing_price) / Decimal(pack_quantity)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _format_yen_amount(value: int | Decimal | None) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _platform_display_name(offer: ProductOffer) -> str:
    code = (offer.marketplace.code if offer.marketplace else "").casefold()
    name = offer.marketplace.name if offer.marketplace else ""
    if code == "rakuten":
        return "乐天"
    if code == "yahoo_shopping":
        return "Yahoo"
    if code == "amazon":
        return "Amazon"
    if code in {"official", "brand_site"}:
        return "官网"
    return name or code or "平台"


def _stock_label(stock_status: str | None) -> str:
    if stock_status in {"in_stock", "limited"}:
        return "有货"
    if stock_status == "out_of_stock":
        return "无货"
    return "库存未知"


def _hydrate_offer_display(offer: ProductOffer) -> ProductOffer:
    try:
        raw = json.loads(offer.raw_data_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    pack_quantity = raw.get("pack_quantity") if raw.get("pack_quantity_reliable") else None
    normalized_unit_price = raw.get("normalized_unit_price") if pack_quantity else None
    if isinstance(normalized_unit_price, str):
        normalized_unit_price = Decimal(normalized_unit_price)
    elif isinstance(normalized_unit_price, (int, float)):
        normalized_unit_price = Decimal(str(normalized_unit_price))
    else:
        normalized_unit_price = None
    listing_price = int(raw.get("listing_price") or offer.item_price or 0)
    offer.listing_price = listing_price
    offer.pack_quantity = int(pack_quantity) if pack_quantity else None
    offer.normalized_unit_price = normalized_unit_price
    offer.display_price = normalized_unit_price if normalized_unit_price is not None else (listing_price if listing_price > 0 else None)
    offer.display_price_text = _format_yen_amount(offer.display_price)
    offer.listing_price_text = _format_yen_amount(listing_price)
    offer.platform_display_name = _platform_display_name(offer)
    offer.stock_label = _stock_label(offer.stock_status)
    offer.seller_display_name = offer.seller or "店铺未提供"
    return offer


def _offer_sort_price(offer: ProductOffer) -> Decimal:
    display_price = getattr(offer, "display_price", None)
    if display_price is not None:
        return Decimal(str(display_price))
    if not offer.item_price:
        return Decimal(10**12)
    return Decimal(offer.item_price or 10**12)


def _latest_purchase_price(session: Session, product: Product | None) -> int | None:
    if product is None:
        return None
    latest = session.scalar(
        select(PurchaseBatchItem.unit_price)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .where(PurchaseBatchItem.product_id == product.id, PurchaseBatchItem.unit_price.is_not(None))
        .order_by(PurchaseBatch.confirmed_at.desc(), PurchaseBatchItem.id.desc())
        .limit(1)
    )
    return latest if latest is not None else product.purchase_price


def _purchase_history_summary(session: Session, product: Product | None) -> dict[str, object]:
    if product is None:
        return {"lowest": None, "latest_price": None, "store_name": None, "store_address": None}
    rows = list(session.execute(
        select(
            PurchaseBatchItem.actual_line_amount,
            PurchaseBatchItem.quantity,
            PurchaseBatch.store_name,
            PurchaseBatch.purchased_at,
            PurchaseBatch.confirmed_at,
            PurchaseBatchItem.id,
            Store.name_cn,
            Store.name_ja,
            Store.address,
            Receipt.raw_store_address,
        )
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .join(Receipt, Receipt.id == PurchaseBatch.receipt_id)
        .outerjoin(Store, Store.id == PurchaseBatch.store_id)
        .where(
            PurchaseBatchItem.product_id == product.id,
            PurchaseBatchItem.actual_line_amount.is_not(None),
            PurchaseBatchItem.quantity > 0,
            Receipt.confirmation_status == "confirmed",
            PurchaseBatch.status != "cancelled",
        )
        .order_by(PurchaseBatch.purchased_at.desc(), PurchaseBatch.confirmed_at.desc(), PurchaseBatchItem.id.desc())
    ).all())
    if not rows:
        return {"lowest": None, "latest_price": None, "store_name": None, "store_address": None}

    def actual_unit_price(row) -> int | Decimal:
        value = (Decimal(row.actual_line_amount) / Decimal(row.quantity)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        return int(value) if value == value.to_integral_value() else value

    latest = rows[0]
    prices = [actual_unit_price(row) for row in rows]
    latest_price = prices[0]
    raw_store = latest.store_name
    store_name_cn = latest.name_cn
    store_name_ja = latest.name_ja
    store_name = "｜".join(part for part in (store_name_cn, store_name_ja) if part) or None
    return {
        "lowest": min(prices),
        "latest_price": latest_price,
        "store_name": store_name or raw_store,
        "store_address": latest.address or latest.raw_store_address,
    }


def _history(
    session: Session, run: PriceSearchRun, lookup: PriceLookupInput, cache_hit: bool,
    lookup_source: str,
) -> PriceLookupHistory:
    item = PriceLookupHistory(
        search_run_id=run.id,
        product_id=run.product_id,
        jan=lookup.jan,
        current_store_price=lookup.current_store_price,
        cache_hit=cache_hit,
        lookup_source=lookup_source,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def offer_reference_price(offer: ProductOffer) -> int | Decimal:
    return offer.total_price if offer.shipping_known else offer.item_price


def online_reference_price_from_offers(offers: list[ProductOffer] | tuple[ProductOffer, ...], limit: int = MAX_TRUSTED_RESULTS) -> tuple[int | None, tuple[ProductOffer, ...]]:
    valid = [
        offer for offer in offers
        if offer.is_trusted
        and offer.stock_status in {"in_stock", "limited", "unknown", None}
        and offer_reference_price(offer) > 0
    ]
    ordered = tuple(sorted(
        valid,
        key=lambda offer: (_offer_sort_price(offer), offer_reference_price(offer), offer.item_price, offer.id),
    )[:limit])
    if not ordered:
        return None, ()
    total = sum(Decimal(offer_reference_price(offer)) for offer in ordered)
    average = (total / Decimal(len(ordered))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(average), ordered


def _cached_run(session: Session, jan: str, now: datetime) -> PriceSearchRun | None:
    runs = list(session.scalars(
        select(PriceSearchRun)
        .where(PriceSearchRun.jan == jan, PriceSearchRun.status == "completed")
        .options(
            selectinload(PriceSearchRun.product),
            selectinload(PriceSearchRun.offers).selectinload(ProductOffer.marketplace),
            selectinload(PriceSearchRun.provider_attempts),
        )
        .order_by(PriceSearchRun.completed_at.desc(), PriceSearchRun.id.desc())
        .limit(5)
    ))
    return next((run for run in runs if _utc(run.cache_expires_at) and _utc(run.cache_expires_at) > now), None)


def _provider_request_id(provider: PriceProvider, jan: str, started_at: datetime) -> str:
    return f"{provider.code}:{jan}:{int(started_at.timestamp() * 1000)}"


def _response_has_enrichment_data(jan: str, response: ProviderResponse) -> bool:
    return response.status == "success" and any(
        candidate.jan == jan
        and candidate.title
        and (candidate.url or candidate.image_url or candidate.item_price > 0)
        for candidate in response.offers
    )


def _search_provider_coalesced(provider: PriceProvider, jan: str, timeout_seconds: float) -> ProviderResponse:
    key = (provider.code, jan)
    owner = False
    with PROVIDER_INFLIGHT_LOCK:
        future = PROVIDER_INFLIGHT.get(key)
        if future is None:
            future = Future()
            PROVIDER_INFLIGHT[key] = future
            owner = True
    if owner:
        try:
            response = provider.search(jan, timeout_seconds)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(response)
            return response
        finally:
            with PROVIDER_INFLIGHT_LOCK:
                if PROVIDER_INFLIGHT.get(key) is future:
                    del PROVIDER_INFLIGHT[key]
    try:
        return future.result(timeout=max(timeout_seconds + 1.0, 1.0))
    except FutureTimeoutError as exc:
        raise TimeoutError(f"{provider.display_name} in-flight request timed out") from exc


def _trigger_enrichment_for_lookup(session: Session, jan: str, history_id: int) -> None:
    try:
        from app.product_enrichment import attach_lookup_source, ensure_enrichment_task, process_enrichment_task
        task = ensure_enrichment_task(
            session, jan, "price_lookup", source_type="price_lookup", source_id=history_id,
        )
        if task is not None and task.status != "running":
            attach_lookup_source(session, task, history_id)
            session.commit()
            process_enrichment_task(session, task)
    except Exception:
        session.rollback()


def query_prices(
    session: Session,
    lookup: PriceLookupInput,
    providers: list[PriceProvider] | None = None,
    *,
    now: datetime | None = None,
    lookup_source: str = "manual",
    provider_timeout_seconds: float = PROVIDER_TIMEOUT_SECONDS,
    trigger_enrichment: bool = True,
) -> PriceLookupView:
    now = now or datetime.now(timezone.utc)
    local_resolution = resolve_local_product_by_jan(session, lookup.jan)
    if local_resolution.is_conflict:
        candidates = "、".join(
            f"{product.display_name or product.name_cn or product.name_ja or product.internal_sku}"
            f"（{product.specification or product.model_spec or '规格未填'}；"
            f"秦丝货号 {product.qinsi_product_code or '—'}）"
            for product in local_resolution.candidate_products
        )
        raise ValueError(f"JAN 多匹配，已禁止查价自动选品：{candidates}")
    product = local_resolution.product
    if not lookup.force_refresh:
        cached = _cached_run(session, lookup.jan, now)
        if cached is not None:
            if product is not None and cached.product_id != product.id:
                cached.product = product
                cached.product_id = product.id
                cached.is_new_candidate = False
                for offer in cached.offers:
                    offer.product = product
                    offer.product_id = product.id
            history = _history(session, cached, lookup, True, lookup_source)
            if product is None and trigger_enrichment:
                _trigger_enrichment_for_lookup(session, lookup.jan, history.id)
            return build_lookup_view(session, history.id)

    run = PriceSearchRun(
        product_id=product.id if product else None,
        jan=lookup.jan,
        status="running",
        is_new_candidate=product is None,
        started_at=now,
        cache_expires_at=now + CACHE_TTL,
    )
    session.add(run)
    session.flush()
    summary: dict[str, dict[str, object]] = {}
    has_enrichment_data = False
    for provider in providers if providers is not None else get_default_price_providers():
        if provider.code == "web_fallback" and has_enrichment_data:
            summary[provider.code] = {"status": "skipped", "count": 0, "message": "已有正式平台资料，未执行 Web fallback"}
            continue
        marketplace = _marketplace(session, provider)
        started_at = datetime.now(timezone.utc)
        request_id = _provider_request_id(provider, lookup.jan, started_at)
        try:
            logger.info(
                "price_provider_request provider=%s endpoint=search request_id=%s jan=%s cache=miss",
                provider.code, request_id, lookup.jan,
            )
            response = _search_provider_coalesced(provider, lookup.jan, provider_timeout_seconds)
            if response.status == "success" and not response.offers:
                response = ProviderResponse("empty", message="Provider 返回空结果")
        except (httpx.TimeoutException, TimeoutError):
            response = ProviderResponse("timeout", message=f"{provider.display_name} 查询超时")
        except Exception as exc:
            response = ProviderResponse("error", message=f"{provider.display_name} 查询失败：{type(exc).__name__}")
        completed_at = datetime.now(timezone.utc)
        elapsed_ms = int((completed_at - started_at).total_seconds() * 1000)
        logger.info(
            "price_provider_result provider=%s endpoint=search request_id=%s jan=%s http_status=%r elapsed_ms=%s status=%s error_code=%r",
            provider.code, request_id, lookup.jan, response.http_status, elapsed_ms,
            response.status, response.error_code,
        )
        _record_provider_state(session, provider, response, tested_at=completed_at)
        if _response_has_enrichment_data(lookup.jan, response):
            has_enrichment_data = True
        attempt = PriceProviderAttempt(
            search_run_id=run.id,
            marketplace_id=marketplace.id,
            provider_code=provider.code,
            status=response.status if response.status in {"success", "empty", "timeout", "error", "unconfigured", "manual_only"} else "error",
            result_count=len(response.offers),
            message=response.message,
            started_at=started_at,
            completed_at=completed_at,
        )
        session.add(attempt)
        summary[provider.code] = {"status": attempt.status, "count": attempt.result_count, "message": attempt.message}
        if response.search_url and response.status != "success":
            session.add(PlatformLookupResult(
                price_search_run_id=run.id,
                platform=provider.code,
                jan=lookup.jan,
                product_url=response.search_url,
                link_type="search",
                jan_verified=False,
                match_type="SEARCH_ONLY",
                confidence=0,
                fetched_at=completed_at,
                error_code=response.error_code or response.status.upper(),
            ))
        for candidate in _dedupe_candidates(provider.code, response.offers):
            jan_status, spec_status, subscription, reasons = _offer_quality(lookup.jan, product, candidate)
            total_price = candidate.item_price + candidate.shipping_price
            unified = candidate.unified(provider.code)
            pack_quantity = _reliable_pack_quantity(candidate.title, candidate.raw_data)
            normalized_unit_price = _normalized_unit_price(candidate.item_price, pack_quantity)
            session.add(PlatformLookupResult(
                price_search_run_id=run.id,
                platform=provider.code,
                jan=candidate.jan,
                title=candidate.title or None,
                brand=candidate.brand,
                price=unified["price"],
                shipping_fee=unified["shipping_fee"],
                total_price=unified["total_price"],
                currency=candidate.currency,
                availability=candidate.stock_status,
                seller=candidate.seller,
                product_url=candidate.url or None,
                image_url=candidate.image_url,
                link_type=candidate.link_type,
                jan_verified=candidate.jan == lookup.jan and candidate.jan_verified,
                match_type=candidate.match_type,
                confidence=candidate.confidence,
                fetched_at=candidate.fetched_at or completed_at,
                error_code=candidate.error_code,
            ))
            session.add(ProductOffer(
                search_run_id=run.id,
                marketplace_id=marketplace.id,
                product_id=product.id if product else None,
                jan=candidate.jan,
                title=candidate.title,
                image_url=candidate.image_url,
                seller=candidate.seller,
                url=candidate.url,
                item_price=candidate.item_price,
                shipping_price=candidate.shipping_price,
                shipping_known=candidate.shipping_known,
                total_price=total_price,
                currency=candidate.currency,
                stock_status=candidate.stock_status,
                listing_type=candidate.listing_type,
                condition=candidate.condition,
                match_status="matched" if not reasons else "flagged",
                jan_match_status=jan_status,
                spec_match_status=spec_status,
                is_subscription=subscription,
                is_trusted=not reasons,
                exclusion_reason="；".join(reasons) if reasons else None,
                fetched_at=completed_at,
                raw_data_json=json.dumps(
                    {
                        **_provider_summary(candidate.raw_data),
                        "listing_price": candidate.item_price,
                        "pack_quantity": pack_quantity,
                        "pack_quantity_reliable": pack_quantity is not None,
                        "normalized_unit_price": str(normalized_unit_price) if normalized_unit_price is not None else None,
                        "image_candidates": _provider_image_candidates(candidate.raw_data, candidate.image_url),
                        "link_type": candidate.link_type,
                        "jan_verified": candidate.jan_verified,
                        "match_type": candidate.match_type,
                        "confidence": candidate.confidence,
                        "error_code": candidate.error_code,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            ))
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    run.provider_summary_json = json.dumps(summary, ensure_ascii=False)
    session.commit()
    history = _history(session, run, lookup, False, lookup_source)
    if product is None and trigger_enrichment:
        _trigger_enrichment_for_lookup(session, lookup.jan, history.id)
    return build_lookup_view(session, history.id)


def build_lookup_view(session: Session, history_id: int) -> PriceLookupView:
    history = session.scalar(
        select(PriceLookupHistory)
        .where(PriceLookupHistory.id == history_id)
        .options(
            selectinload(PriceLookupHistory.product),
            selectinload(PriceLookupHistory.search_run).selectinload(PriceSearchRun.product),
            selectinload(PriceLookupHistory.search_run).selectinload(PriceSearchRun.offers).selectinload(ProductOffer.marketplace),
            selectinload(PriceLookupHistory.search_run).selectinload(PriceSearchRun.provider_attempts),
            selectinload(PriceLookupHistory.search_run).selectinload(PriceSearchRun.platform_results),
        )
    )
    if history is None:
        raise LookupError("查价历史不存在")
    run = history.search_run
    product = run.product
    offers = tuple(_hydrate_offer_display(offer) for offer in run.offers)

    def offer_sort_key(offer: ProductOffer) -> tuple[int, Decimal, int, int]:
        stock_rank = 1 if offer.stock_status == "out_of_stock" else 0
        return (stock_rank, _offer_sort_price(offer), offer.item_price or 10**12, offer.id)

    trusted = tuple(sorted((
        offer for offer in offers
        if offer.is_trusted and offer.stock_status in {"in_stock", "limited", "unknown", None}
    ), key=offer_sort_key))
    trusted_ids = {offer.id for offer in trusted}
    incomplete = tuple(sorted((
        offer for offer in offers
        if offer.is_trusted
        and offer.id not in trusted_ids
        and (
            offer.jan_match_status != "exact"
            or offer.spec_match_status == "unknown"
        )
    ), key=offer_sort_key))
    flagged = tuple(sorted((offer for offer in offers if not offer.is_trusted), key=offer_sort_key))
    result_offers = tuple(sorted(offers, key=offer_sort_key))
    online_min = _offer_sort_price(trusted[0]) if trusted else None
    difference = history.current_store_price - online_min if history.current_store_price is not None and online_min is not None else None
    comparison = None
    if difference is not None:
        comparison = "store_cheaper" if difference < 0 else ("online_cheaper" if difference > 0 else "same")
    purchase_history = _purchase_history_summary(session, product)
    return PriceLookupView(
        history=history,
        run=run,
        product=product,
        product_display_name=format_product_display_name(product.name_cn, product.name_ja) if product else None,
        recent_purchase_price=purchase_history["latest_price"],
        historical_lowest_purchase_price=purchase_history["lowest"],
        latest_purchase_store_name=purchase_history["store_name"],
        latest_purchase_store_address=purchase_history["store_address"],
        trusted_offers=trusted,
        incomplete_offers=incomplete,
        flagged_offers=flagged,
        result_offers=result_offers,
        fallback_results=tuple(item for item in run.platform_results if item.link_type == "search"),
        attempts=tuple(run.provider_attempts),
        current_store_price=history.current_store_price,
        online_min_price=online_min,
        difference=difference,
        comparison_status=comparison,
    )


def recent_price_lookup_histories(session: Session, limit: int = 20, jan: str | None = None) -> list[PriceLookupHistory]:
    query = (
        select(PriceLookupHistory)
        .options(selectinload(PriceLookupHistory.product), selectinload(PriceLookupHistory.search_run))
        .order_by(PriceLookupHistory.created_at.desc(), PriceLookupHistory.id.desc())
        .limit(limit)
    )
    if jan:
        query = query.where(PriceLookupHistory.jan == jan)
    return list(session.scalars(query))
