from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Numeric, cast, exists, func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Product, ProductWatchConfig, PurchaseBatch, PurchaseBatchItem,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine,
    QinsiPurchaseExportLine, QinsiPurchaseExportLineSource, Receipt, Store,
)
from app.qinsi_inventory import inventory_settings


TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")
RANGE_LABELS = {
    "30d": "最近30天", "90d": "最近90天", "month": "本月",
    "last_month": "上月", "year": "今年", "custom": "自定义日期",
}
INVENTORY_LABELS = {
    "no_snapshot": "无快照", "snapshot_stale": "快照过期",
    "stock_available": "库存充足", "stock_low": "低库存",
    "out_of_stock": "缺货", "incoming_or_pending": "有待提交采购",
}


@dataclass(frozen=True, slots=True)
class DateRange:
    key: str
    label: str
    start: date
    end: date
    start_at: datetime
    end_at: datetime


def resolve_date_range(key: str, start: date | None, end: date | None, *, today: date | None = None) -> DateRange:
    today = today or datetime.now(TOKYO).date()
    key = key if key in RANGE_LABELS else "30d"
    if key == "90d":
        start, end = today - timedelta(days=89), today
    elif key == "month":
        start, end = today.replace(day=1), today
    elif key == "last_month":
        this_month = today.replace(day=1)
        end = this_month - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "year":
        start, end = today.replace(month=1, day=1), today
    elif key == "custom":
        start, end = start or today - timedelta(days=29), end or today
        if start > end:
            start, end = end, start
    else:
        key, start, end = "30d", today - timedelta(days=29), today
    start_at = datetime.combine(start, time.min, TOKYO).astimezone(timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), time.min, TOKYO).astimezone(timezone.utc)
    return DateRange(key, RANGE_LABELS[key], start, end, start_at, end_at)


def _valid_range(period: DateRange):
    return (
        PurchaseBatch.status != "cancelled",
        PurchaseBatch.purchased_at.is_not(None),
        PurchaseBatch.purchased_at >= period.start_at,
        PurchaseBatch.purchased_at < period.end_at,
        PurchaseBatchItem.quantity > 0,
    )


def _pending_item_filter():
    imported = exists(
        select(QinsiPurchaseExportLineSource.id)
        .join(QinsiPurchaseExportLine, QinsiPurchaseExportLine.id == QinsiPurchaseExportLineSource.export_line_id)
        .where(
            QinsiPurchaseExportLineSource.purchase_batch_item_id == PurchaseBatchItem.id,
            QinsiPurchaseExportLineSource.is_active.is_(True),
            QinsiPurchaseExportLine.status == "imported",
        )
    )
    return ~imported


def _granularity(period: DateRange) -> str:
    days = (period.end - period.start).days + 1
    return "day" if days <= 45 else ("week" if days <= 180 else "month")


def _bucket_expression(granularity: str):
    local_time = func.datetime(PurchaseBatch.purchased_at, "+9 hours")
    if granularity == "month":
        return func.strftime("%Y-%m", local_time)
    if granularity == "week":
        return func.strftime("%Y-W%W", local_time)
    return func.strftime("%Y-%m-%d", local_time)


def _chart_points(rows: list[dict], value_key: str, *, top: float = 160.0) -> list[dict]:
    if not rows:
        return []
    maximum = max(float(row[value_key] or 0) for row in rows) or 1.0
    count = len(rows)
    return [dict(row, x=30 if count == 1 else 30 + index * 540 / (count - 1),
                 y=185 - float(row[value_key] or 0) * top / maximum)
            for index, row in enumerate(rows)]


def _trend_rows(session: Session, period: DateRange) -> tuple[str, list[dict]]:
    granularity = _granularity(period)
    bucket = _bucket_expression(granularity).label("bucket")
    rows = session.execute(
        select(
            bucket,
            func.coalesce(func.sum(PurchaseBatchItem.actual_line_amount), 0),
            func.count(func.distinct(PurchaseBatch.id)),
            func.coalesce(func.sum(PurchaseBatchItem.quantity), 0),
        )
        .join(PurchaseBatchItem, PurchaseBatchItem.purchase_batch_id == PurchaseBatch.id)
        .where(*_valid_range(period))
        .group_by(bucket).order_by(bucket).limit(370)
    ).all()
    return granularity, [
        {"bucket": bucket_value, "amount": int(amount), "purchase_count": int(count), "quantity": int(quantity)}
        for bucket_value, amount, count, quantity in rows
    ]


def _store_ranking(session: Session, period: DateRange) -> list[dict]:
    store_id = func.coalesce(PurchaseBatch.store_id, Receipt.store_id)
    rows = session.execute(
        select(
            Store, func.count(func.distinct(PurchaseBatch.id)),
            func.coalesce(func.sum(PurchaseBatchItem.quantity), 0),
            func.coalesce(func.sum(PurchaseBatchItem.actual_line_amount), 0),
            func.max(PurchaseBatch.purchased_at),
        )
        .select_from(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .join(Receipt, Receipt.id == PurchaseBatch.receipt_id)
        .join(Store, Store.id == store_id)
        .where(*_valid_range(period))
        .options(selectinload(Store.brand))
        .group_by(Store.id).order_by(func.sum(PurchaseBatchItem.actual_line_amount).desc(), Store.id).limit(20)
    ).all()
    return [{"store": store, "purchase_count": int(count), "quantity": int(quantity),
             "amount": int(amount), "latest_date": latest}
            for store, count, quantity, amount, latest in rows]


def _product_ranking(session: Session, period: DateRange) -> list[dict]:
    rows = session.execute(
        select(
            Product, func.count(func.distinct(PurchaseBatch.id)),
            func.coalesce(func.sum(PurchaseBatchItem.quantity), 0),
            func.min(cast(PurchaseBatchItem.actual_line_amount, Numeric(18, 4)) / PurchaseBatchItem.quantity),
            func.max(PurchaseBatch.purchased_at),
        )
        .select_from(PurchaseBatchItem)
        .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id)
        .join(Product, Product.id == PurchaseBatchItem.product_id)
        .where(*_valid_range(period))
        .group_by(Product.id).order_by(func.sum(PurchaseBatchItem.quantity).desc(), Product.id).limit(20)
    ).all()
    if not rows:
        return []
    product_ids = [row[0].id for row in rows]
    ranked = select(
        PurchaseBatchItem.product_id.label("product_id"),
        func.coalesce(PurchaseBatch.store_id, Receipt.store_id).label("store_id"),
        PurchaseBatchItem.actual_line_amount.label("amount"), PurchaseBatchItem.quantity.label("quantity"),
        func.row_number().over(partition_by=PurchaseBatchItem.product_id,
                               order_by=(PurchaseBatch.purchased_at.desc(), PurchaseBatchItem.id.desc())).label("rn"),
    ).join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchItem.purchase_batch_id).join(
        Receipt, Receipt.id == PurchaseBatch.receipt_id
    ).where(*_valid_range(period), PurchaseBatchItem.product_id.in_(product_ids)).subquery()
    latest_rows = session.execute(
        select(ranked.c.product_id, Store, ranked.c.amount, ranked.c.quantity)
        .outerjoin(Store, Store.id == ranked.c.store_id).where(ranked.c.rn == 1)
    ).all()
    latest = {product_id: (store, amount, quantity) for product_id, store, amount, quantity in latest_rows}
    result = []
    for product, count, quantity, minimum, latest_date in rows:
        store, amount, latest_quantity = latest.get(product.id, (None, None, None))
        result.append({
            "product": product, "purchase_count": int(count), "quantity": int(quantity),
            "minimum_price": Decimal(str(minimum)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if minimum is not None else None,
            "latest_price": (Decimal(amount) / latest_quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amount is not None and latest_quantity else None,
            "latest_store": store, "latest_date": latest_date,
        })
    return result


def inventory_distribution(session: Session, *, now: datetime | None = None) -> tuple[QinsiInventorySnapshot | None, list[dict]]:
    now = now or datetime.now(timezone.utc)
    products = list(session.scalars(select(Product).where(Product.status == "active")))
    counts = {key: 0 for key in INVENTORY_LABELS}
    if not products:
        return None, [{"key": key, "label": label, "count": 0} for key, label in INVENTORY_LABELS.items()]
    pending_ids = set(session.scalars(
        select(PurchaseBatchItem.product_id).join(PurchaseBatch).where(
            PurchaseBatch.status != "cancelled", PurchaseBatchItem.quantity > 0, _pending_item_filter()
        ).distinct()
    ))
    snapshot = session.scalar(select(QinsiInventorySnapshot).where(
        QinsiInventorySnapshot.status.in_({"completed", "completed_with_issues"})
    ).order_by(func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
               QinsiInventorySnapshot.id.desc()).limit(1))
    quantities = {}
    if snapshot:
        quantities = dict(session.execute(
            select(QinsiInventorySnapshotLine.product_id, func.sum(QinsiInventorySnapshotLine.quantity))
            .where(QinsiInventorySnapshotLine.snapshot_id == snapshot.id,
                   QinsiInventorySnapshotLine.matching_status == "matched",
                   QinsiInventorySnapshotLine.product_id.is_not(None),
                   QinsiInventorySnapshotLine.quantity.is_not(None))
            .group_by(QinsiInventorySnapshotLine.product_id)
        ).all())
    stale = False
    if snapshot:
        value = snapshot.data_at or snapshot.imported_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        stale = now - value > timedelta(hours=inventory_settings().stale_hours)
    default_threshold = inventory_settings().default_low_stock_threshold
    for product in products:
        if product.id in pending_ids:
            key = "incoming_or_pending"
        elif product.id not in quantities:
            key = "no_snapshot"
        elif stale:
            key = "snapshot_stale"
        elif quantities[product.id] <= 0:
            key = "out_of_stock"
        elif quantities[product.id] < (product.low_stock_threshold or default_threshold):
            key = "stock_low"
        else:
            key = "stock_available"
        counts[key] += 1
    return snapshot, [{"key": key, "label": label, "count": counts[key]} for key, label in INVENTORY_LABELS.items()]


def analytics_dashboard(session: Session, period: DateRange, *, selected_bucket: str | None = None) -> dict:
    amount, purchase_count, quantity, store_count = session.execute(
        select(
            func.coalesce(func.sum(PurchaseBatchItem.actual_line_amount), 0),
            func.count(func.distinct(PurchaseBatch.id)),
            func.coalesce(func.sum(PurchaseBatchItem.quantity), 0),
            func.count(func.distinct(func.coalesce(PurchaseBatch.store_id, Receipt.store_id))),
        ).select_from(PurchaseBatchItem).join(PurchaseBatch).join(Receipt, Receipt.id == PurchaseBatch.receipt_id)
        .where(*_valid_range(period))
    ).one()
    first_purchase = select(
        PurchaseBatchItem.product_id.label("product_id"), func.min(PurchaseBatch.purchased_at).label("first_at")
    ).join(PurchaseBatch).where(PurchaseBatch.status != "cancelled", PurchaseBatchItem.quantity > 0,
                               PurchaseBatch.purchased_at.is_not(None)).group_by(PurchaseBatchItem.product_id).subquery()
    new_products = session.scalar(select(func.count()).select_from(first_purchase).where(
        first_purchase.c.first_at >= period.start_at, first_purchase.c.first_at < period.end_at
    )) or 0
    pending_quantity = session.scalar(
        select(func.coalesce(func.sum(PurchaseBatchItem.quantity), 0)).join(PurchaseBatch).where(
            PurchaseBatch.status != "cancelled", PurchaseBatchItem.quantity > 0, _pending_item_filter()
        )
    ) or 0
    target_reached = session.scalar(select(func.count()).select_from(ProductWatchConfig).where(
        ProductWatchConfig.enabled.is_(True), ProductWatchConfig.current_lowest_price.is_not(None),
        ProductWatchConfig.effective_target_price.is_not(None),
        ProductWatchConfig.current_lowest_price <= ProductWatchConfig.effective_target_price,
    )) or 0
    granularity, trends = _trend_rows(session, period)
    snapshot, inventory_rows = inventory_distribution(session)
    details = []
    if selected_bucket:
        bucket = _bucket_expression(granularity)
        details = list(session.scalars(
            select(PurchaseBatch).join(PurchaseBatchItem).where(*_valid_range(period), bucket == selected_bucket)
            .options(selectinload(PurchaseBatch.store))
            .group_by(PurchaseBatch.id).order_by(PurchaseBatch.purchased_at.desc()).limit(100)
        ))
    return {
        "period": period, "granularity": granularity, "trends": trends,
        "amount_points": _chart_points(trends, "amount"),
        "count_points": _chart_points(trends, "purchase_count", top=135),
        "quantity_points": _chart_points(trends, "quantity", top=135),
        "metrics": {
            "amount": int(amount), "purchase_count": int(purchase_count), "quantity": int(quantity),
            "new_products": int(new_products), "store_count": int(store_count),
            "average_amount": (Decimal(amount) / purchase_count).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if purchase_count else Decimal(0),
            "pending_quantity": int(pending_quantity), "target_reached": int(target_reached),
        },
        "stores": _store_ranking(session, period), "products": _product_ranking(session, period),
        "inventory_snapshot": snapshot, "inventory_rows": inventory_rows,
        "selected_bucket": selected_bucket, "detail_batches": details,
    }
