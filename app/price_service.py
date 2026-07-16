from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Marketplace, PriceLookupHistory, PriceProviderAttempt, PriceSearchRun,
    Product, ProductOffer, PurchaseBatch, PurchaseBatchItem,
)
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse, get_default_price_providers
from app.product_identity import format_product_display_name
from app.schemas import PriceLookupInput


CACHE_TTL = timedelta(minutes=15)
PROVIDER_TIMEOUT_SECONDS = 4.0
MAX_TRUSTED_RESULTS = 3
SUBSCRIPTION_PATTERN = re.compile(r"定期(?:購入|便)|サブスク|subscription", re.IGNORECASE)
MULTIPACK_PATTERN = re.compile(r"(?:[x×*]\s*[2-9]\d*|[2-9]\d*\s*(?:個|本|袋|包|枚|箱|セット|パック))", re.IGNORECASE)
SPEC_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|mL|l|L|g|kg|個|本|枚|袋|包|錠|粒)", re.IGNORECASE)
PROVIDER_SUMMARY_KEYS = {
    "brand", "brandName", "manufacturer", "maker", "category", "categoryName",
    "model", "modelNumber", "color", "capacity", "size", "janCode", "shopName", "seller",
}


@dataclass(frozen=True, slots=True)
class PriceLookupView:
    history: PriceLookupHistory
    run: PriceSearchRun
    product: Product | None
    product_display_name: str | None
    recent_purchase_price: int | None
    trusted_offers: tuple[ProductOffer, ...]
    flagged_offers: tuple[ProductOffer, ...]
    attempts: tuple[PriceProviderAttempt, ...]
    current_store_price: int | None
    online_min_price: int | None
    difference: int | None
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
    if candidate.jan == jan:
        jan_status = "exact"
    elif candidate.jan:
        jan_status = "mismatch"
        reasons.append("JAN 不一致")
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
    if not candidate.shipping_known:
        reasons.append("运费未知")
    if candidate.item_price <= 0:
        reasons.append("商品价格无效")
    return jan_status, spec_status, subscription, reasons


def _provider_summary(raw_data: dict) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in PROVIDER_SUMMARY_KEYS:
        value = raw_data.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            summary[key] = str(value).strip()[:255]
    return summary


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
) -> PriceLookupView:
    now = now or datetime.now(timezone.utc)
    product = session.scalar(select(Product).where(Product.jan == lookup.jan))
    if not lookup.force_refresh:
        cached = _cached_run(session, lookup.jan, now)
        if cached is not None:
            history = _history(session, cached, lookup, True, lookup_source)
            if product is None:
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
    for provider in providers if providers is not None else get_default_price_providers():
        marketplace = _marketplace(session, provider)
        started_at = datetime.now(timezone.utc)
        try:
            response = provider.search(lookup.jan, provider_timeout_seconds)
            if response.status == "success" and not response.offers:
                response = ProviderResponse("empty", message="Provider 返回空结果")
        except (httpx.TimeoutException, TimeoutError):
            response = ProviderResponse("timeout", message=f"{provider.display_name} 查询超时")
        except Exception as exc:
            response = ProviderResponse("error", message=f"{provider.display_name} 查询失败：{type(exc).__name__}")
        completed_at = datetime.now(timezone.utc)
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
        for candidate in response.offers:
            jan_status, spec_status, subscription, reasons = _offer_quality(lookup.jan, product, candidate)
            total_price = candidate.item_price + candidate.shipping_price
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
                raw_data_json=json.dumps(_provider_summary(candidate.raw_data), ensure_ascii=False, default=str),
            ))
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    run.provider_summary_json = json.dumps(summary, ensure_ascii=False)
    session.commit()
    history = _history(session, run, lookup, False, lookup_source)
    if product is None:
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
        )
    )
    if history is None:
        raise LookupError("查价历史不存在")
    run = history.search_run
    product = run.product
    trusted = tuple(sorted((offer for offer in run.offers if offer.is_trusted), key=lambda item: (item.total_price, item.item_price, item.id))[:MAX_TRUSTED_RESULTS])
    flagged = tuple(sorted((offer for offer in run.offers if not offer.is_trusted), key=lambda item: (item.total_price, item.id)))
    online_min = trusted[0].total_price if trusted else None
    difference = history.current_store_price - online_min if history.current_store_price is not None and online_min is not None else None
    comparison = None
    if difference is not None:
        comparison = "store_cheaper" if difference < 0 else ("online_cheaper" if difference > 0 else "same")
    return PriceLookupView(
        history=history,
        run=run,
        product=product,
        product_display_name=format_product_display_name(product.name_cn, product.name_ja) if product else None,
        recent_purchase_price=_latest_purchase_price(session, product),
        trusted_offers=trusted,
        flagged_offers=flagged,
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
