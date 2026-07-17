from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Product, ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch, PurchaseBatchItem,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, Receipt, RestockList, RestockListItem, Store,
)
from app.qinsi_inventory import inventory_settings
from app.store_service import PurchaseFact, purchase_facts


LIST_STATUSES = {"draft", "active", "completed", "cancelled"}
ITEM_STATUSES = {"to_check", "found", "not_found", "purchased", "skipped"}
SOURCE_TYPES = {"manual", "store_history", "watched_products", "purchase_analysis"}
LIST_STATUS_LABELS = {"draft": "草稿", "active": "进行中", "completed": "已完成", "cancelled": "已取消"}
ITEM_STATUS_LABELS = {
    "to_check": "待查看", "found": "已找到", "not_found": "未找到",
    "purchased": "已购买（临时）", "skipped": "跳过",
}
SOURCE_LABELS = {
    "manual": "手动", "store_history": "门店历史", "watched_products": "关注商品",
    "purchase_analysis": "采购分析",
}


@dataclass(frozen=True, slots=True)
class InventorySignal:
    snapshot_id: int | None
    quantity: int | None
    data_at: datetime | None
    status: str


@dataclass(frozen=True, slots=True)
class RestockCandidate:
    product: Product
    priority: int
    added_source: str
    reasons: tuple[str, ...]
    store_purchase_count: int
    latest_store_purchase_at: datetime | None
    latest_store_price: int | None
    store_lowest_price: int | None
    latest_purchase_price: int | None
    historical_lowest_price: int | None
    target_price: int | None
    target_reached: bool
    online_price: int | None
    online_price_at: datetime | None
    online_snapshot_id: int | None
    watch_config_id: int | None
    inventory: InventorySignal


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _fact_date(fact: PurchaseFact) -> datetime:
    return fact.batch.purchased_at or fact.batch.confirmed_at


def _yen(value: Decimal | None) -> int | None:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if value is not None else None


def _inventory_signals(session: Session, product_ids: set[int], now: datetime) -> dict[int, InventorySignal]:
    if not product_ids:
        return {}
    rows = session.execute(
        select(
            QinsiInventorySnapshotLine.product_id, QinsiInventorySnapshot.id,
            QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at,
            func.coalesce(func.sum(QinsiInventorySnapshotLine.quantity), 0),
        )
        .join(QinsiInventorySnapshot, QinsiInventorySnapshot.id == QinsiInventorySnapshotLine.snapshot_id)
        .where(
            QinsiInventorySnapshotLine.product_id.in_(product_ids),
            QinsiInventorySnapshotLine.matching_status == "matched",
            QinsiInventorySnapshotLine.quantity.is_not(None),
            QinsiInventorySnapshot.status.in_({"completed", "completed_with_issues"}),
        )
        .group_by(QinsiInventorySnapshotLine.product_id, QinsiInventorySnapshot.id)
        .order_by(
            QinsiInventorySnapshotLine.product_id,
            func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
            QinsiInventorySnapshot.id.desc(),
        )
    ).all()
    selected: dict[int, InventorySignal] = {}
    stale_hours = inventory_settings().stale_hours
    for product_id, snapshot_id, data_at, imported_at, quantity in rows:
        if product_id in selected:
            continue
        value = data_at or imported_at
        stale = _aware(now) - _aware(value) > timedelta(hours=stale_hours)
        selected[product_id] = InventorySignal(snapshot_id, int(quantity), value, "snapshot_stale" if stale else "available")
    return selected


def _online_snapshots(session: Session, product_ids: set[int]) -> dict[int, ProductWatchSnapshot]:
    if not product_ids:
        return {}
    rows = session.scalars(
        select(ProductWatchSnapshot).where(
            ProductWatchSnapshot.product_id.in_(product_ids),
            ProductWatchSnapshot.status == "success",
            ProductWatchSnapshot.total_price.is_not(None),
        ).order_by(ProductWatchSnapshot.product_id, ProductWatchSnapshot.checked_at.desc(), ProductWatchSnapshot.id.desc())
    )
    result = {}
    for row in rows:
        result.setdefault(row.product_id, row)
    return result


def restock_candidates(
    session: Session, store_id: int, *, include_all: bool = False, now: datetime | None = None,
) -> list[RestockCandidate]:
    store = session.get(Store, store_id)
    if store is None:
        raise LookupError("具体门店不存在")
    now = now or datetime.now(timezone.utc)
    products = list(session.scalars(
        select(Product).where(Product.status == "active").order_by(Product.updated_at.desc()).limit(1000)
    ))
    product_ids = {product.id for product in products}
    facts_by_product: dict[int, list[PurchaseFact]] = {}
    for fact in purchase_facts(session):
        facts_by_product.setdefault(fact.item.product_id, []).append(fact)
    watches = {row.product_id: row for row in session.scalars(
        select(ProductWatchConfig).where(ProductWatchConfig.product_id.in_(product_ids))
    )} if product_ids else {}
    online = _online_snapshots(session, product_ids)
    inventories = _inventory_signals(session, product_ids, now)
    default_threshold = inventory_settings().default_low_stock_threshold
    candidates = []
    for product in products:
        facts = facts_by_product.get(product.id, [])
        store_facts = [fact for fact in facts if fact.store is not None and fact.store.id == store_id]
        latest_fact = max(facts, key=lambda fact: (_fact_date(fact), fact.item.id), default=None)
        latest_store = max(store_facts, key=lambda fact: (_fact_date(fact), fact.item.id), default=None)
        prices = [fact.reference_unit_price for fact in facts if fact.reference_unit_price is not None]
        store_prices = [fact.reference_unit_price for fact in store_facts if fact.reference_unit_price is not None]
        watch = watches.get(product.id)
        snapshot = online.get(product.id)
        online_price = snapshot.total_price if snapshot is not None else (watch.current_lowest_price if watch else None)
        online_at = snapshot.checked_at if snapshot is not None else (watch.last_check_at if watch else None)
        target = (watch.user_target_price or watch.effective_target_price) if watch else None
        target_reached = bool(target is not None and online_price is not None and online_price <= target)
        inventory = inventories.get(product.id, InventorySignal(None, None, None, "no_snapshot"))
        if inventory.status != "snapshot_stale" and inventory.quantity is not None:
            if inventory.quantity <= 0:
                inventory = InventorySignal(inventory.snapshot_id, inventory.quantity, inventory.data_at, "out_of_stock")
            elif inventory.quantity < (product.low_stock_threshold or default_threshold):
                inventory = InventorySignal(inventory.snapshot_id, inventory.quantity, inventory.data_at, "stock_low")
            else:
                inventory = InventorySignal(inventory.snapshot_id, inventory.quantity, inventory.data_at, "stock_available")
        recent = bool(latest_fact and _aware(now) - _aware(_fact_date(latest_fact)) <= timedelta(days=90))
        low = inventory.status in {"stock_low", "out_of_stock"}
        watched = watch is not None
        if not include_all and not (store_facts or watched or recent or low or target_reached):
            continue
        latest_store_price = _yen(latest_store.reference_unit_price) if latest_store else None
        reasons = []
        if target_reached:
            reasons.append("已达到目标价")
        if inventory.status == "stock_low":
            reasons.append("秦丝库存偏低")
        elif inventory.status == "out_of_stock":
            reasons.append("秦丝快照缺货")
        elif inventory.status == "snapshot_stale":
            reasons.append("秦丝快照已过期")
        elif inventory.status == "no_snapshot":
            reasons.append("暂无秦丝快照")
        if len(store_facts) > 1:
            reasons.append(f"曾在该店购买{len({fact.batch.id for fact in store_facts})}次")
        elif store_facts:
            reasons.append("曾在该店购买")
        if latest_store:
            days = max(0, (_aware(now).date() - _aware(_fact_date(latest_store)).date()).days)
            reasons.append(f"最近一次在该店购买为{days}天前")
        store_min = _yen(min(store_prices)) if store_prices else None
        if store_min is not None:
            reasons.append(f"该店历史最低价为¥{store_min}")
        if watched:
            reasons.append("已关注商品")
        price_not_higher = bool(online_price is not None and latest_store_price is not None and online_price <= latest_store_price)
        if target_reached and low:
            priority = 10
        elif low:
            priority = 20
        elif latest_store and price_not_higher:
            priority = 30
        elif len({fact.batch.id for fact in store_facts}) > 1:
            priority = 40
        elif watched:
            priority = 50
        else:
            priority = 60
        source = "store_history" if store_facts else ("watched_products" if watched else ("purchase_analysis" if recent or low or target_reached else "manual"))
        candidates.append(RestockCandidate(
            product=product, priority=priority, added_source=source, reasons=tuple(reasons or ["手动查看"]),
            store_purchase_count=len({fact.batch.id for fact in store_facts}),
            latest_store_purchase_at=_fact_date(latest_store) if latest_store else None,
            latest_store_price=latest_store_price, store_lowest_price=store_min,
            latest_purchase_price=_yen(latest_fact.reference_unit_price) if latest_fact else None,
            historical_lowest_price=_yen(min(prices)) if prices else None,
            target_price=target, target_reached=target_reached, online_price=online_price,
            online_price_at=online_at, online_snapshot_id=snapshot.id if snapshot else None,
            watch_config_id=watch.id if watch else None, inventory=inventory,
        ))
    return sorted(candidates, key=lambda row: (row.priority, -row.store_purchase_count, row.product.id))


def create_restock_list(
    session: Session, *, name: str, store_id: int, product_ids: set[int],
    source_type: str, notes: str | None = None, status: str = "active",
) -> RestockList:
    store = session.get(Store, store_id)
    if store is None:
        raise LookupError("具体门店不存在")
    name = name.strip()
    if not name:
        name = f"{store.display_name} 补货清单 {datetime.now(timezone.utc).date().isoformat()}"
    if source_type not in SOURCE_TYPES or status not in LIST_STATUSES:
        raise ValueError("清单来源或状态无效")
    unique_ids = {int(value) for value in product_ids}
    if not unique_ids:
        raise ValueError("请至少选择一个商品")
    candidates = {row.product.id: row for row in restock_candidates(session, store_id, include_all=True)}
    missing = unique_ids - candidates.keys()
    if missing:
        raise ValueError("选择的商品不存在或已停用")
    restock_list = RestockList(
        name=name[:255], store_id=store_id, status=status, source_type=source_type,
        notes=(notes or "").strip() or None,
    )
    session.add(restock_list)
    session.flush()
    for index, product_id in enumerate(sorted(unique_ids, key=lambda value: (candidates[value].priority, value)), 1):
        row = candidates[product_id]
        added_source = "watched_products" if source_type == "watched_products" else row.added_source
        restock_list.items.append(RestockListItem(
            product_id=product_id, added_source=added_source, sort_value=row.priority * 1000 + index,
            target_purchase_price_snapshot=row.target_price,
            latest_purchase_price_snapshot=row.latest_purchase_price,
            historical_lowest_purchase_price_snapshot=row.historical_lowest_price,
            latest_store_purchase_price_snapshot=row.latest_store_price,
            store_lowest_purchase_price_snapshot=row.store_lowest_price,
            latest_store_purchase_at=row.latest_store_purchase_at,
            qinsi_quantity_snapshot=row.inventory.quantity, qinsi_snapshot_at=row.inventory.data_at,
            qinsi_snapshot_id=row.inventory.snapshot_id,
            online_lowest_price_snapshot=row.online_price, online_price_checked_at=row.online_price_at,
            online_snapshot_id=row.online_snapshot_id, watch_config_id=row.watch_config_id,
            recommendation_reason="；".join(row.reasons), status="to_check",
        ))
    session.commit()
    return get_restock_list(session, restock_list.id)


def get_restock_list(session: Session, list_id: int) -> RestockList | None:
    return session.scalar(
        select(RestockList).where(RestockList.id == list_id).options(
            selectinload(RestockList.store).selectinload(Store.brand),
            selectinload(RestockList.items).selectinload(RestockListItem.product),
            selectinload(RestockList.items).selectinload(RestockListItem.watch_config),
            selectinload(RestockList.items).selectinload(RestockListItem.online_snapshot),
            selectinload(RestockList.items).selectinload(RestockListItem.qinsi_snapshot),
            selectinload(RestockList.items).selectinload(RestockListItem.purchase_batch_item)
            .selectinload(PurchaseBatchItem.purchase_batch).selectinload(PurchaseBatch.receipt),
        )
    )


def list_restock_lists(session: Session, *, store_id: int | None = None, status: str | None = None) -> list[RestockList]:
    query = select(RestockList).options(
        selectinload(RestockList.store), selectinload(RestockList.items),
    )
    if store_id is not None:
        query = query.where(RestockList.store_id == store_id)
    if status in LIST_STATUSES:
        query = query.where(RestockList.status == status)
    return list(session.scalars(query.order_by(RestockList.updated_at.desc(), RestockList.id.desc())))


def list_statistics(restock_list: RestockList) -> dict:
    items = list(restock_list.items)
    counts = {status: sum(item.status == status for item in items) for status in ITEM_STATUSES}
    resolved = sum(item.status != "to_check" for item in items)
    estimated = None
    if items and all(item.planned_quantity is not None and item.target_purchase_price_snapshot is not None for item in items):
        estimated = sum(item.planned_quantity * item.target_purchase_price_snapshot for item in items)
    actual_rows = [item for item in items if item.actual_purchase_quantity is not None and item.actual_purchase_price is not None]
    return {
        "total": len(items), **counts,
        "completion_rate": round(resolved * 100 / len(items)) if items else 0,
        "estimated_amount": estimated,
        "actual_amount": sum(item.actual_purchase_quantity * item.actual_purchase_price for item in actual_rows) if actual_rows else None,
    }


def _optional_positive(value: str | int | None, label: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{label}必须是正整数")
    return parsed


def update_restock_item(
    session: Session, item_id: int, *, status: str, planned_quantity=None,
    actual_quantity=None, actual_price=None, notes: str | None = None,
) -> RestockListItem:
    item = session.scalar(select(RestockListItem).where(RestockListItem.id == item_id).options(selectinload(RestockListItem.restock_list)))
    if item is None:
        raise LookupError("补货清单明细不存在")
    if item.restock_list.status in {"completed", "cancelled"}:
        raise ValueError("已完成或已取消清单只能查看")
    if status not in ITEM_STATUSES:
        raise ValueError("明细状态无效")
    item.status = status
    item.planned_quantity = _optional_positive(planned_quantity, "计划数量")
    item.actual_purchase_quantity = _optional_positive(actual_quantity, "实际购买数量")
    item.actual_purchase_price = _optional_positive(actual_price, "实际购买价格")
    item.notes = (notes or "").strip() or None
    session.commit()
    return item


def update_restock_list_status(session: Session, list_id: int, status: str) -> RestockList:
    restock_list = session.get(RestockList, list_id)
    if restock_list is None:
        raise LookupError("补货清单不存在")
    if status not in LIST_STATUSES:
        raise ValueError("清单状态无效")
    restock_list.status = status
    restock_list.completed_at = datetime.now(timezone.utc) if status == "completed" else None
    session.commit()
    return restock_list


def add_product_to_list(session: Session, list_id: int, product_id: int) -> RestockListItem:
    restock_list = get_restock_list(session, list_id)
    if restock_list is None:
        raise LookupError("补货清单不存在")
    if restock_list.status != "active":
        raise ValueError("只能向进行中的清单添加商品")
    if any(item.product_id == product_id for item in restock_list.items):
        return next(item for item in restock_list.items if item.product_id == product_id)
    row = next((item for item in restock_candidates(session, restock_list.store_id, include_all=True) if item.product.id == product_id), None)
    if row is None:
        raise LookupError("商品不存在或已停用")
    item = RestockListItem(
        restock_list_id=list_id, product_id=product_id, added_source="manual",
        sort_value=max((entry.sort_value for entry in restock_list.items), default=60000) + 1,
        target_purchase_price_snapshot=row.target_price,
        latest_purchase_price_snapshot=row.latest_purchase_price,
        historical_lowest_purchase_price_snapshot=row.historical_lowest_price,
        latest_store_purchase_price_snapshot=row.latest_store_price,
        store_lowest_purchase_price_snapshot=row.store_lowest_price,
        latest_store_purchase_at=row.latest_store_purchase_at,
        qinsi_quantity_snapshot=row.inventory.quantity, qinsi_snapshot_at=row.inventory.data_at,
        qinsi_snapshot_id=row.inventory.snapshot_id,
        online_lowest_price_snapshot=row.online_price, online_price_checked_at=row.online_price_at,
        online_snapshot_id=row.online_snapshot_id, watch_config_id=row.watch_config_id,
        recommendation_reason="手动加入；" + "；".join(row.reasons),
    )
    session.add(item)
    session.commit()
    return item


def copy_restock_list(session: Session, list_id: int) -> RestockList:
    original = get_restock_list(session, list_id)
    if original is None:
        raise LookupError("补货清单不存在")
    return create_restock_list(
        session, name=f"{original.name} 副本", store_id=original.store_id,
        product_ids={item.product_id for item in original.items}, source_type="purchase_analysis",
        notes=original.notes, status="draft",
    )


def link_purchase_item(session: Session, restock_item_id: int, purchase_item_id: int) -> RestockListItem:
    item = session.scalar(select(RestockListItem).where(RestockListItem.id == restock_item_id).options(
        selectinload(RestockListItem.restock_list),
    ))
    purchase_item = session.scalar(select(PurchaseBatchItem).where(PurchaseBatchItem.id == purchase_item_id).options(
        selectinload(PurchaseBatchItem.purchase_batch).selectinload(PurchaseBatch.receipt),
    ))
    if item is None or purchase_item is None:
        raise LookupError("清单明细或正式采购明细不存在")
    batch = purchase_item.purchase_batch
    store_id = batch.store_id or batch.receipt.store_id
    if purchase_item.product_id != item.product_id or store_id != item.restock_list.store_id or batch.status == "cancelled":
        raise ValueError("正式采购与清单商品或具体门店不匹配")
    item.purchase_batch_item_id = purchase_item.id
    item.status = "purchased"
    item.actual_purchase_quantity = purchase_item.quantity
    item.actual_purchase_price = _yen(Decimal(purchase_item.actual_line_amount) / purchase_item.quantity) if purchase_item.actual_line_amount is not None else purchase_item.unit_price
    session.commit()
    return item


def trace_rows(session: Session, restock_list: RestockList) -> dict[int, list[PurchaseFact]]:
    grouped: dict[int, list[PurchaseFact]] = {item.id: [] for item in restock_list.items}
    by_product = {item.product_id: item.id for item in restock_list.items}
    for fact in purchase_facts(session, store_id=restock_list.store_id):
        item_id = by_product.get(fact.item.product_id)
        if item_id is not None:
            grouped[item_id].append(fact)
    for rows in grouped.values():
        rows.sort(key=lambda fact: (_fact_date(fact), fact.item.id), reverse=True)
    return grouped


def active_lists_for_product(session: Session, product_id: int) -> list[RestockList]:
    return list(session.scalars(
        select(RestockList).where(RestockList.status == "active")
        .options(selectinload(RestockList.store)).order_by(RestockList.updated_at.desc())
    ))


def lists_for_product(session: Session, product_id: int) -> list[RestockListItem]:
    return list(session.scalars(
        select(RestockListItem).join(RestockList).where(RestockListItem.product_id == product_id)
        .options(selectinload(RestockListItem.restock_list).selectinload(RestockList.store))
        .order_by(RestockList.updated_at.desc()).limit(20)
    ))


def recent_lists_for_store(session: Session, store_id: int) -> list[RestockList]:
    return list(session.scalars(
        select(RestockList).where(RestockList.store_id == store_id, RestockList.status != "cancelled")
        .options(selectinload(RestockList.items)).order_by(RestockList.updated_at.desc()).limit(10)
    ))
