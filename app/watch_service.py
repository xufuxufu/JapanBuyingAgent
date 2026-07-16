from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    PriceLookupHistory,
    PriceSearchRun,
    Product,
    ProductEnrichmentSource,
    ProductEnrichmentTask,
    ProductOffer,
    ProductWatchConfig,
    ProductWatchRecommendation,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiPurchaseExportJob,
    QinsiPurchaseExportLine,
)
from app.qinsi_inventory import ProductInventoryView, PurchaseAssistance, latest_inventory_for_product, purchase_assistance


FREQUENCY_HOURS = {"low": 24, "normal": 12, "high": 6, "urgent": 3}
REASON_LABELS = {
    "history_purchase_count": "历史采购次数不少于2次",
    "cumulative_purchase_quantity": "累计采购数量达到阈值",
    "scan_count": "扫码查价次数达到阈值",
    "restock_purchase": "曾发生补货采购",
    "purchase_enrichment": "采购流程商品丰富化已完成",
}
REASON_SOURCES = {
    "history_purchase_count": "purchase_recommendation",
    "cumulative_purchase_quantity": "purchase_recommendation",
    "scan_count": "scan_recommendation",
    "restock_purchase": "purchase_recommendation",
    "purchase_enrichment": "enrichment_recommendation",
}


@dataclass(slots=True)
class ProductWatchRow:
    product: Product
    config: ProductWatchConfig | None
    recommendation: ProductWatchRecommendation | None
    latest_purchase_price: int | None
    minimum_purchase_price: int | None
    online_price: int | None
    online_price_at: datetime | None
    enrichment_task_id: int | None
    recommended_target_price: int | None
    recommended_price_source: str | None
    inventory: ProductInventoryView
    purchase_assistance: PurchaseAssistance


def _positive_int(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("目标采购价必须是整数日元") from exc
    if parsed <= 0:
        raise ValueError("目标采购价必须大于0")
    return parsed


def _threshold(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _purchase_prices(session: Session, product_id: int) -> list[tuple[int, datetime, int]]:
    rows = session.execute(
        select(PurchaseBatchItem.unit_price, PurchaseBatch.purchased_at, PurchaseBatchItem.id)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .where(
            PurchaseBatchItem.product_id == product_id,
            PurchaseBatch.status != "cancelled",
            PurchaseBatchItem.quantity > 0,
            PurchaseBatchItem.unit_price.is_not(None),
            PurchaseBatchItem.unit_price > 0,
        )
    ).all()
    return [(price, purchased_at or datetime.min, item_id) for price, purchased_at, item_id in rows]


def _latest_online_price(session: Session, product_id: int) -> tuple[int | None, datetime | None]:
    run = session.scalar(
        select(PriceSearchRun)
        .where(PriceSearchRun.product_id == product_id, PriceSearchRun.status == "completed")
        .order_by(PriceSearchRun.completed_at.desc(), PriceSearchRun.id.desc())
        .limit(1)
    )
    if run is None:
        return None, None
    offer = session.scalar(
        select(ProductOffer)
        .where(
            ProductOffer.search_run_id == run.id,
            ProductOffer.product_id == product_id,
            ProductOffer.is_trusted.is_(True),
            ProductOffer.total_price > 0,
        )
        .order_by(ProductOffer.total_price, ProductOffer.id)
        .limit(1)
    )
    return (offer.total_price, offer.fetched_at) if offer else (None, run.completed_at)


def calculate_recommended_target(session: Session, product_id: int) -> tuple[int | None, str | None, datetime]:
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError("商品不存在")
    calculated_at = datetime.now(timezone.utc)
    prices = _purchase_prices(session, product_id)
    if len(prices) >= 2:
        return min(price for price, _, _ in prices), "historical_lowest_purchase", calculated_at
    if prices:
        latest = max(prices, key=lambda row: (row[1], row[2]))
        return latest[0], "latest_purchase", calculated_at
    if product.purchase_price is not None and product.purchase_price > 0:
        return product.purchase_price, "latest_purchase", calculated_at
    online_price, _ = _latest_online_price(session, product_id)
    if online_price is not None:
        return online_price, "trusted_online_lowest", calculated_at
    return None, None, calculated_at


def refresh_recommended_target(session: Session, config: ProductWatchConfig) -> ProductWatchConfig:
    price, source, calculated_at = calculate_recommended_target(session, config.product_id)
    config.recommended_target_price = price
    config.recommended_price_source = source
    config.recommended_calculated_at = calculated_at
    config.effective_target_price = config.user_target_price if config.user_target_price is not None else price
    if config.enabled and config.effective_target_price is None:
        config.enabled = False
        config.pause_reason = "no_valid_target_price"
    return config


def get_watch(session: Session, product_id: int) -> ProductWatchConfig | None:
    return session.scalar(select(ProductWatchConfig).where(ProductWatchConfig.product_id == product_id))


def add_watch(
    session: Session,
    product_id: int,
    *,
    source: str = "manual",
    user_target_price: int | str | None = None,
    frequency_tier: str | None = None,
) -> ProductWatchConfig:
    if session.get(Product, product_id) is None:
        raise LookupError("商品不存在")
    config = get_watch(session, product_id)
    if config is None:
        config = ProductWatchConfig(
            product_id=product_id,
            source=source,
            frequency_tier=frequency_tier or ("low" if source != "manual" else "normal"),
            enabled=False,
        )
        session.add(config)
        session.flush()
    if frequency_tier is not None:
        if frequency_tier not in FREQUENCY_HOURS:
            raise ValueError("监控频率档位无效")
        config.frequency_tier = frequency_tier
    if user_target_price not in (None, ""):
        config.user_target_price = _positive_int(user_target_price)
    refresh_recommended_target(session, config)
    session.commit()
    session.refresh(config)
    return config


def update_watch(
    session: Session,
    product_id: int,
    *,
    user_target_price: int | str | None,
    frequency_tier: str,
    monitor_restock: bool,
) -> ProductWatchConfig:
    config = get_watch(session, product_id)
    if config is None:
        raise LookupError("关注配置不存在")
    if frequency_tier not in FREQUENCY_HOURS:
        raise ValueError("监控频率档位无效")
    config.user_target_price = _positive_int(user_target_price)
    config.frequency_tier = frequency_tier
    config.monitor_restock = monitor_restock
    refresh_recommended_target(session, config)
    if config.enabled:
        config.next_check_at = datetime.now(timezone.utc) + timedelta(hours=FREQUENCY_HOURS[frequency_tier])
    session.commit()
    session.refresh(config)
    return config


def set_watch_enabled(session: Session, product_id: int, enabled: bool) -> ProductWatchConfig:
    config = get_watch(session, product_id)
    if config is None:
        raise LookupError("关注配置不存在")
    refresh_recommended_target(session, config)
    if enabled and config.effective_target_price is None:
        raise ValueError("没有有效目标价，不能启用监控")
    config.enabled = enabled
    config.pause_reason = None if enabled else "user_paused"
    if enabled:
        config.next_check_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(config)
    return config


def _pending_recommendation(session: Session, product_id: int, reason: str) -> ProductWatchRecommendation | None:
    return session.scalar(
        select(ProductWatchRecommendation).where(
            ProductWatchRecommendation.product_id == product_id,
            ProductWatchRecommendation.reason == reason,
            ProductWatchRecommendation.ignored.is_(False),
            ProductWatchRecommendation.accepted.is_(False),
        )
    )


def generate_watch_recommendations(session: Session) -> list[ProductWatchRecommendation]:
    quantity_threshold = _threshold("JBA_WATCH_PURCHASE_QUANTITY_THRESHOLD", 5)
    scan_threshold = _threshold("JBA_WATCH_SCAN_THRESHOLD", 3)
    watched_ids = set(session.scalars(select(ProductWatchConfig.product_id)))
    created: list[ProductWatchRecommendation] = []
    for product in session.scalars(select(Product).where(Product.status == "active")):
        if product.id in watched_ids:
            continue
        purchase_count, quantity = session.execute(
            select(func.count(func.distinct(PurchaseBatchItem.purchase_batch_id)), func.coalesce(func.sum(PurchaseBatchItem.quantity), 0))
            .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
            .where(
                PurchaseBatchItem.product_id == product.id,
                PurchaseBatch.status != "cancelled",
                PurchaseBatchItem.quantity > 0,
            )
        ).one()
        scan_count = session.scalar(
            select(func.count(PriceLookupHistory.id)).where(PriceLookupHistory.product_id == product.id)
        ) or 0
        has_restock = session.scalar(
            select(QinsiPurchaseExportLine.id)
            .join(QinsiPurchaseExportJob, QinsiPurchaseExportJob.id == QinsiPurchaseExportLine.export_job_id)
            .where(
                QinsiPurchaseExportLine.product_id == product.id,
                QinsiPurchaseExportJob.export_type == "restock",
                QinsiPurchaseExportLine.status != "cancelled",
            )
            .limit(1)
        ) is not None
        has_purchase_enrichment = session.scalar(
            select(ProductEnrichmentTask.id)
            .join(ProductEnrichmentSource, ProductEnrichmentSource.task_id == ProductEnrichmentTask.id)
            .where(
                ProductEnrichmentTask.product_id == product.id,
                ProductEnrichmentTask.status.in_(("completed", "completed_with_warnings")),
                ProductEnrichmentSource.source_type == "receipt_item",
            )
            .limit(1)
        ) is not None
        reasons = []
        if purchase_count >= 2:
            reasons.append("history_purchase_count")
        if quantity >= quantity_threshold:
            reasons.append("cumulative_purchase_quantity")
        if scan_count >= scan_threshold:
            reasons.append("scan_count")
        if has_restock:
            reasons.append("restock_purchase")
        if has_purchase_enrichment:
            reasons.append("purchase_enrichment")
        for reason in reasons:
            if _pending_recommendation(session, product.id, reason) is None:
                item = ProductWatchRecommendation(product_id=product.id, reason=reason)
                session.add(item)
                created.append(item)
    session.commit()
    return created


def accept_recommendations(session: Session, recommendation_ids: set[int]) -> list[ProductWatchConfig]:
    accepted: list[ProductWatchConfig] = []
    recommendations = list(session.scalars(
        select(ProductWatchRecommendation).where(
            ProductWatchRecommendation.id.in_(recommendation_ids),
            ProductWatchRecommendation.accepted.is_(False),
            ProductWatchRecommendation.ignored.is_(False),
        )
    )) if recommendation_ids else []
    for recommendation in recommendations:
        config = get_watch(session, recommendation.product_id)
        if config is None:
            config = ProductWatchConfig(
                product_id=recommendation.product_id,
                enabled=False,
                frequency_tier="low",
                source=REASON_SOURCES[recommendation.reason],
            )
            session.add(config)
            session.flush()
        refresh_recommended_target(session, config)
        recommendation.accepted = True
        accepted.append(config)
    session.commit()
    return accepted


def ignore_recommendation(session: Session, recommendation_id: int) -> ProductWatchRecommendation:
    recommendation = session.get(ProductWatchRecommendation, recommendation_id)
    if recommendation is None or recommendation.accepted:
        raise LookupError("待处理推荐不存在")
    recommendation.ignored = True
    session.commit()
    return recommendation


def bulk_enable_watches(session: Session, product_ids: set[int]) -> list[ProductWatchConfig]:
    enabled: list[ProductWatchConfig] = []
    configs = list(session.scalars(
        select(ProductWatchConfig).where(ProductWatchConfig.product_id.in_(product_ids))
    )) if product_ids else []
    for config in configs:
        refresh_recommended_target(session, config)
        if config.effective_target_price is not None:
            config.enabled = True
            config.pause_reason = None
            config.next_check_at = datetime.now(timezone.utc)
            enabled.append(config)
    session.commit()
    return enabled


def _row(session: Session, product: Product, config=None, recommendation=None) -> ProductWatchRow:
    prices = _purchase_prices(session, product.id)
    latest = max(prices, key=lambda item: (item[1], item[2]))[0] if prices else (
        product.purchase_price if product.purchase_price and product.purchase_price > 0 else None
    )
    minimum = min((item[0] for item in prices), default=None)
    online_price, online_at = _latest_online_price(session, product.id)
    enrichment_task_id = session.scalar(
        select(ProductEnrichmentTask.id)
        .where(ProductEnrichmentTask.product_id == product.id)
        .order_by(ProductEnrichmentTask.completed_at.desc(), ProductEnrichmentTask.id.desc())
        .limit(1)
    )
    if config is not None:
        recommended_target_price = config.recommended_target_price
        recommended_price_source = config.recommended_price_source
    else:
        recommended_target_price, recommended_price_source, _ = calculate_recommended_target(session, product.id)
    inventory = latest_inventory_for_product(session, product.id)
    assistance = purchase_assistance(session, product, inventory=inventory)
    return ProductWatchRow(
        product, config, recommendation, latest, minimum, online_price, online_at,
        enrichment_task_id, recommended_target_price, recommended_price_source, inventory, assistance,
    )


def list_watch_groups(session: Session) -> dict[str, list[ProductWatchRow]]:
    configs = list(session.scalars(
        select(ProductWatchConfig)
        .options(selectinload(ProductWatchConfig.product))
        .order_by(ProductWatchConfig.updated_at.desc(), ProductWatchConfig.id.desc())
    ))
    recommendations = list(session.scalars(
        select(ProductWatchRecommendation)
        .where(ProductWatchRecommendation.accepted.is_(False), ProductWatchRecommendation.ignored.is_(False))
        .options(selectinload(ProductWatchRecommendation.product))
        .order_by(ProductWatchRecommendation.recommended_at.desc(), ProductWatchRecommendation.id.desc())
    ))
    groups = {"enabled": [], "watched": [], "recommended": [], "paused": []}
    for config in configs:
        refresh_recommended_target(session, config)
        accepted_recommendation = session.scalar(
            select(ProductWatchRecommendation)
            .where(
                ProductWatchRecommendation.product_id == config.product_id,
                ProductWatchRecommendation.accepted.is_(True),
            )
            .order_by(ProductWatchRecommendation.recommended_at.desc(), ProductWatchRecommendation.id.desc())
            .limit(1)
        )
        row = _row(session, config.product, config=config, recommendation=accepted_recommendation)
        if config.enabled:
            groups["enabled"].append(row)
        elif config.pause_reason:
            groups["paused"].append(row)
        else:
            groups["watched"].append(row)
    for recommendation in recommendations:
        groups["recommended"].append(_row(session, recommendation.product, recommendation=recommendation))
    session.commit()
    return groups
