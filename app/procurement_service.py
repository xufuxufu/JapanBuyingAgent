from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.local_product import resolve_local_product_by_jan
from app.models import (
    Location, Product, ProcurementDemand, ProcurementDemandPlan, ProcurementDemandPlanSource,
    ProcurementExecutionReceiptMatch, ProcurementPurchaseExecution, QinsiInventorySnapshot,
    QinsiInventorySnapshotLine, Receipt, ReceiptItem, SalesOrder, SalesOrderItem, Store,
)
from app.qinsi_inventory import INVENTORY_REGION_CHINA, INVENTORY_REGION_JAPAN, inventory_region_for_location
from app.store_service import product_store_summaries_bulk


DEMAND_TYPES = {"sales_confirmed", "channel_shortage", "manual_restock", "investigation", "system_restock"}
SOURCE_PERSONS = {"秀", "丈母娘", "老婆", "系统"}
SOURCE_TYPES = {"sales_order", "channel_shortage", "manual", "investigation", "system_restock"}
DEMAND_STATUSES = {"open", "planned", "closed", "cancelled"}
PLAN_STATUSES = {"planned", "cancelled"}

# Only these demand types count toward the "confirmed need" total shown on a
# product's aggregated card; investigation and system_restock never do.
CONFIRMED_DEMAND_TYPES = {"sales_confirmed", "channel_shortage", "manual_restock"}

DEMAND_TYPE_LABELS: dict[str, str] = {
    "sales_confirmed": "明确销售需求",
    "channel_shortage": "渠道缺货",
    "manual_restock": "人工补货",
    "investigation": "调查看货",
    "system_restock": "系统补货建议",
}
SOURCE_TYPE_LABELS: dict[str, str] = {
    "sales_order": "微信销售订单",
    "channel_shortage": "渠道缺货上报",
    "manual": "人工登记",
    "investigation": "调查任务",
    "system_restock": "系统预警",
}
STATUS_LABELS: dict[str, str] = {"open": "待处理", "planned": "已安排采购", "closed": "已处理", "cancelled": "已取消"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------- sales-order-driven demand (source: 秀 / 微信) ----------------


def sync_demand_for_sales_order_item(session: Session, item: SalesOrderItem, *, commit: bool = True) -> ProcurementDemand:
    """Idempotently create the one procurement_demand backing a sales_order_item.

    Safe to call more than once for the same item: the unique index on
    sales_order_item_id backs this up at the database level too.
    """
    existing = session.scalar(select(ProcurementDemand).where(ProcurementDemand.sales_order_item_id == item.id))
    if existing is not None:
        return existing
    demand = ProcurementDemand(
        product_id=item.product_id, product_name_snapshot=item.product_name_snapshot,
        jan_snapshot=item.jan_snapshot, demand_type="sales_confirmed", source_person="秀",
        source_channel="微信", source_type="sales_order", sales_order_item_id=item.id,
        requested_quantity=item.quantity, status="open",
    )
    session.add(demand)
    if commit:
        session.commit()
    else:
        session.flush()
    return demand


def sync_demands_for_sales_order(session: Session, order: SalesOrder, *, commit: bool = True) -> list[ProcurementDemand]:
    demands = [sync_demand_for_sales_order_item(session, item, commit=False) for item in order.items]
    if commit:
        session.commit()
    return demands


# ---------------- manual entry points (丈母娘 / 秀 / 老婆) ----------------


def _resolve_product_or_manual_name(session: Session, *, product_id: int | None, manual_name: str | None) -> tuple[Product | None, str, str | None]:
    product = None
    if product_id is not None:
        product = session.get(Product, product_id)
        if product is None:
            raise LookupError("所选商品不存在")
    if product is not None:
        name_snapshot = product.display_name or product.name_cn or product.name_ja or product.internal_sku
        jan_snapshot = product.jan
    else:
        name_snapshot = (manual_name or "").strip()
        jan_snapshot = None
    if not name_snapshot:
        raise ValueError("商品/关键词不能为空")
    return product, name_snapshot[:255], (jan_snapshot[:32] if jan_snapshot else None)


def create_channel_shortage_demand(
    session: Session, *, product_id: int | None = None, manual_name: str | None = None,
    quantity: int | None = None, note: str | None = None, source_person: str = "丈母娘", commit: bool = True,
) -> ProcurementDemand:
    if source_person not in SOURCE_PERSONS:
        raise ValueError("未知来源人")
    product, name_snapshot, jan_snapshot = _resolve_product_or_manual_name(session, product_id=product_id, manual_name=manual_name)
    if quantity is not None and quantity <= 0:
        raise ValueError("数量必须大于0")
    demand = ProcurementDemand(
        product_id=product.id if product else None, product_name_snapshot=name_snapshot, jan_snapshot=jan_snapshot,
        demand_type="channel_shortage", source_person=source_person, source_channel="国内销售渠道",
        source_type="channel_shortage", requested_quantity=quantity, note=(note or "").strip() or None, status="open",
    )
    session.add(demand)
    if commit:
        session.commit()
    else:
        session.flush()
    return demand


def create_investigation_demand(
    session: Session, *, product_id: int | None = None, manual_name: str | None = None,
    quantity: int | None = None, note: str | None = None, commit: bool = True,
) -> ProcurementDemand:
    product, name_snapshot, jan_snapshot = _resolve_product_or_manual_name(session, product_id=product_id, manual_name=manual_name)
    if quantity is not None and quantity <= 0:
        raise ValueError("数量必须大于0")
    demand = ProcurementDemand(
        product_id=product.id if product else None, product_name_snapshot=name_snapshot, jan_snapshot=jan_snapshot,
        demand_type="investigation", source_person="秀", source_channel="微信",
        source_type="investigation", requested_quantity=quantity, note=(note or "").strip() or None, status="open",
    )
    session.add(demand)
    if commit:
        session.commit()
    else:
        session.flush()
    return demand


def create_manual_restock_demand(
    session: Session, *, product_id: int | None = None, manual_name: str | None = None,
    quantity: int | None = None, note: str | None = None, source_person: str = "老婆", commit: bool = True,
) -> ProcurementDemand:
    if source_person not in SOURCE_PERSONS:
        raise ValueError("未知来源人")
    product, name_snapshot, jan_snapshot = _resolve_product_or_manual_name(session, product_id=product_id, manual_name=manual_name)
    if quantity is not None and quantity <= 0:
        raise ValueError("数量必须大于0")
    demand = ProcurementDemand(
        product_id=product.id if product else None, product_name_snapshot=name_snapshot, jan_snapshot=jan_snapshot,
        demand_type="manual_restock", source_person=source_person, source_channel=None,
        source_type="manual", requested_quantity=quantity, note=(note or "").strip() or None, status="open",
    )
    session.add(demand)
    if commit:
        session.commit()
    else:
        session.flush()
    return demand


def close_investigation_demand(session: Session, demand_id: int, *, note: str | None = None) -> ProcurementDemand:
    demand = session.get(ProcurementDemand, demand_id)
    if demand is None:
        raise LookupError("需求不存在")
    if demand.demand_type != "investigation":
        raise ValueError("仅调查类需求支持此操作")
    if demand.status != "open":
        raise ValueError("该调查需求当前状态不支持关闭")
    demand.status = "closed"
    if note and note.strip():
        demand.note = f"{demand.note}\n{note.strip()}" if demand.note else note.strip()
    session.commit()
    return demand


# ---------------- aggregation ----------------


@dataclass(frozen=True, slots=True)
class DemandGroup:
    kind: str  # "product" | "jan" | "demand"
    key: str
    product: Product | None
    display_name: str
    jan: str | None
    confirmed_quantity: int
    confirmed_demands: list[ProcurementDemand]
    investigation_demands: list[ProcurementDemand]
    all_demands: list[ProcurementDemand]

    @property
    def group_ref(self) -> str:
        return f"{self.kind}:{self.key}"


def _order_still_active(demand: ProcurementDemand) -> bool:
    """A demand backed by a cancelled sales order is not a valid need anymore.

    Checked dynamically at read time rather than syncing demand.status on
    cancellation, so the sales-order cancellation flow never needs to know
    about procurement demands.
    """
    if demand.sales_order_item_id is None:
        return True
    item = demand.sales_order_item
    order = item.sales_order if item else None
    return order is None or order.status != "cancelled"


def _load_demands(session: Session, *, status: str | None) -> list[ProcurementDemand]:
    query = select(ProcurementDemand).options(
        selectinload(ProcurementDemand.product),
        selectinload(ProcurementDemand.sales_order_item).selectinload(SalesOrderItem.sales_order),
    ).order_by(ProcurementDemand.created_at)
    if status is not None:
        query = query.where(ProcurementDemand.status == status)
    rows = session.scalars(query).all()
    return [demand for demand in rows if _order_still_active(demand)]


def list_open_demands(session: Session) -> list[ProcurementDemand]:
    return _load_demands(session, status="open")


def list_investigation_demands(session: Session, *, status: str = "open") -> list[ProcurementDemand]:
    return [demand for demand in _load_demands(session, status=status) if demand.demand_type == "investigation"]


def list_all_demands(session: Session) -> list[ProcurementDemand]:
    return _load_demands(session, status=None)


def _group_key(demand: ProcurementDemand) -> tuple[str, str]:
    if demand.product_id is not None:
        return ("product", str(demand.product_id))
    if demand.jan_snapshot:
        return ("jan", demand.jan_snapshot)
    return ("demand", str(demand.id))


def _build_groups(demands: list[ProcurementDemand]) -> list[DemandGroup]:
    grouped: dict[tuple[str, str], list[ProcurementDemand]] = {}
    order: list[tuple[str, str]] = []
    for demand in demands:
        key = _group_key(demand)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(demand)
    groups: list[DemandGroup] = []
    for key in order:
        items = grouped[key]
        confirmed = [d for d in items if d.demand_type in CONFIRMED_DEMAND_TYPES]
        investigation = [d for d in items if d.demand_type == "investigation"]
        product = next((d.product for d in items if d.product is not None), None)
        display_name = (product.display_name if product else None) or items[0].product_name_snapshot
        jan = product.jan if product else items[0].jan_snapshot
        groups.append(DemandGroup(
            kind=key[0], key=key[1], product=product, display_name=display_name or "未命名商品", jan=jan,
            confirmed_quantity=sum(d.requested_quantity or 0 for d in confirmed),
            confirmed_demands=confirmed, investigation_demands=investigation, all_demands=items,
        ))
    return groups


def aggregate_open_demand_groups(session: Session) -> list[DemandGroup]:
    """Groups open demands by product (falling back to JAN, then standing alone) for the demand center's default view."""
    return _build_groups(list_open_demands(session))


def aggregate_all_demand_groups(session: Session) -> list[DemandGroup]:
    return _build_groups(list_all_demands(session))


def get_group(session: Session, kind: str, key: str, *, demands: list[ProcurementDemand] | None = None) -> DemandGroup | None:
    groups = _build_groups(demands if demands is not None else list_all_demands(session))
    for group in groups:
        if group.kind == kind and group.key == key:
            return group
    return None


def default_planned_quantity_for_group(group: DemandGroup) -> int:
    return max(group.confirmed_quantity, 0)


# ---------------- reference inventory (QinSi snapshot, Phase 2B) ----------------
#
# QinSi stays the authority on real inventory. Everything here reads the most
# recent qinsi_inventory_snapshot(s) already imported and never writes to them
# -- this module must never call QinSi, import an Excel, or create a snapshot.
# "China" vs "Japan" is about which side can fulfil a domestic order right now;
# they are never added together (see inventory_region_for_location).

FRESHNESS_NO_SNAPSHOT = "no_snapshot"
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_EXPIRED = "expired"
FRESHNESS_LABELS: dict[str, str] = {
    FRESHNESS_NO_SNAPSHOT: "暂无库存快照",
    FRESHNESS_FRESH: "",
    FRESHNESS_STALE: "库存快照较旧",
    FRESHNESS_EXPIRED: "库存数据可能已过期",
}
_FRESH_HOURS = 24
_STALE_HOURS = 72


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _snapshot_freshness(snapshot_at: datetime, now: datetime) -> str:
    age = _aware(now) - _aware(snapshot_at)
    if age <= timedelta(hours=_FRESH_HOURS):
        return FRESHNESS_FRESH
    if age <= timedelta(hours=_STALE_HOURS):
        return FRESHNESS_STALE
    return FRESHNESS_EXPIRED


@dataclass(frozen=True, slots=True)
class ReferenceInventory:
    """A read-only snapshot-derived stock signal for one product -- never "current stock"."""

    snapshot_id: int | None
    snapshot_at: datetime | None
    china_quantity: int | None
    china_known: bool
    japan_quantity: int | None
    japan_known: bool
    freshness: str


UNKNOWN_REFERENCE_INVENTORY = ReferenceInventory(
    snapshot_id=None, snapshot_at=None, china_quantity=None, china_known=False,
    japan_quantity=None, japan_known=False, freshness=FRESHNESS_NO_SNAPSHOT,
)


def reference_inventory_for_products(
    session: Session, product_ids: list[int], *, now: datetime | None = None,
) -> dict[int, ReferenceInventory]:
    """Reference stock for each product from the single latest snapshot, split by region.

    Always the same snapshot for every product/warehouse -- never blends lines
    from different import times together. Batches into a couple of queries
    regardless of how many products are asked for for (no N+1 per card).
    """
    now = now or utcnow()
    if not product_ids:
        return {}
    unique_ids = list(dict.fromkeys(product_ids))
    latest_snapshot = session.scalar(
        select(QinsiInventorySnapshot).order_by(
            func.coalesce(QinsiInventorySnapshot.data_at, QinsiInventorySnapshot.imported_at).desc(),
            QinsiInventorySnapshot.id.desc(),
        ).limit(1)
    )
    if latest_snapshot is None:
        return {product_id: UNKNOWN_REFERENCE_INVENTORY for product_id in unique_ids}
    snapshot_at = latest_snapshot.data_at or latest_snapshot.imported_at
    freshness = _snapshot_freshness(snapshot_at, now)
    rows = session.execute(
        select(QinsiInventorySnapshotLine.product_id, QinsiInventorySnapshotLine.warehouse_id, QinsiInventorySnapshotLine.quantity)
        .where(
            QinsiInventorySnapshotLine.snapshot_id == latest_snapshot.id,
            QinsiInventorySnapshotLine.product_id.in_(unique_ids),
            QinsiInventorySnapshotLine.matching_status == "matched",
            QinsiInventorySnapshotLine.quantity.is_not(None),
        )
    ).all()
    warehouse_ids = {warehouse_id for _, warehouse_id, _ in rows if warehouse_id is not None}
    warehouses = (
        {location.id: location for location in session.scalars(select(Location).where(Location.id.in_(warehouse_ids)))}
        if warehouse_ids else {}
    )
    china_totals: dict[int, int] = {}
    japan_totals: dict[int, int] = {}
    for product_id, warehouse_id, quantity in rows:
        if warehouse_id is None:
            continue
        region = inventory_region_for_location(warehouses.get(warehouse_id))
        if region == INVENTORY_REGION_CHINA:
            china_totals[product_id] = china_totals.get(product_id, 0) + quantity
        elif region == INVENTORY_REGION_JAPAN:
            japan_totals[product_id] = japan_totals.get(product_id, 0) + quantity
    return {
        product_id: ReferenceInventory(
            snapshot_id=latest_snapshot.id, snapshot_at=snapshot_at,
            china_quantity=china_totals.get(product_id), china_known=product_id in china_totals,
            japan_quantity=japan_totals.get(product_id), japan_known=product_id in japan_totals,
            freshness=freshness,
        )
        for product_id in unique_ids
    }


@dataclass(frozen=True, slots=True)
class DemandInventoryContext:
    """Reference inventory plus the domestic-shortage judgement for one demand group/plan."""

    inventory: ReferenceInventory
    confirmed_demand_quantity: int
    domestic_shortage: int | None
    domestic_shortage_known: bool
    default_planned_quantity: int
    default_planned_quantity_basis: str  # "shortage" | "confirmed_demand"


def _inventory_context(confirmed_demand_quantity: int, inventory: ReferenceInventory) -> DemandInventoryContext:
    if inventory.china_known:
        shortage = max(confirmed_demand_quantity - (inventory.china_quantity or 0), 0)
        return DemandInventoryContext(
            inventory=inventory, confirmed_demand_quantity=confirmed_demand_quantity,
            domestic_shortage=shortage, domestic_shortage_known=True,
            default_planned_quantity=shortage, default_planned_quantity_basis="shortage",
        )
    return DemandInventoryContext(
        inventory=inventory, confirmed_demand_quantity=confirmed_demand_quantity,
        domestic_shortage=None, domestic_shortage_known=False,
        default_planned_quantity=confirmed_demand_quantity, default_planned_quantity_basis="confirmed_demand",
    )


def resolve_group_product_id(session: Session, group: DemandGroup) -> int | None:
    """product_id if one can be safely resolved (existing product, or a JAN with exactly one match)."""
    if group.product is not None:
        return group.product.id
    if group.kind == "jan" and group.jan:
        resolution = resolve_local_product_by_jan(session, group.jan)
        if resolution.is_unique and resolution.product is not None:
            return resolution.product.id
    return None


def build_group_inventory_contexts(
    session: Session, groups: list[DemandGroup], *, now: datetime | None = None,
) -> dict[str, DemandInventoryContext]:
    """One inventory lookup for every group in the list -- callers must not query per-card."""
    now = now or utcnow()
    product_id_by_ref: dict[str, int] = {}
    for group in groups:
        product_id = resolve_group_product_id(session, group)
        if product_id is not None:
            product_id_by_ref[group.group_ref] = product_id
    inventories = reference_inventory_for_products(session, list(product_id_by_ref.values()), now=now)
    contexts: dict[str, DemandInventoryContext] = {}
    for group in groups:
        product_id = product_id_by_ref.get(group.group_ref)
        inventory = inventories.get(product_id, UNKNOWN_REFERENCE_INVENTORY) if product_id else UNKNOWN_REFERENCE_INVENTORY
        contexts[group.group_ref] = _inventory_context(group.confirmed_quantity, inventory)
    return contexts


def build_plan_inventory_contexts(
    session: Session, plans: list[ProcurementDemandPlan], *, now: datetime | None = None,
) -> dict[int, ReferenceInventory]:
    """Read-only current reference stock for already-planned rows -- never touches planned_quantity."""
    now = now or utcnow()
    product_ids = [plan.product_id for plan in plans if plan.product_id is not None]
    inventories = reference_inventory_for_products(session, product_ids, now=now)
    return {plan.id: inventories.get(plan.product_id, UNKNOWN_REFERENCE_INVENTORY) for plan in plans}


# ---------------- purchase decisions (老婆 checkbox -> 已安排采购) ----------------


@dataclass(frozen=True, slots=True)
class PlanSelectionInput:
    kind: str
    key: str
    planned_quantity: int
    note: str | None = None


def create_plans(session: Session, selections: list[PlanSelectionInput], *, created_by: str = "老婆") -> list[ProcurementDemandPlan]:
    if not selections:
        raise ValueError("请至少选择一个商品")
    groups_by_ref = {(g.kind, g.key): g for g in aggregate_open_demand_groups(session)}
    for selection in selections:
        if selection.planned_quantity <= 0:
            raise ValueError("计划采购数量必须大于0")
        group = groups_by_ref.get((selection.kind, selection.key))
        if group is None or not group.confirmed_demands:
            raise ValueError("所选商品没有待处理的明确采购需求")
    plans: list[ProcurementDemandPlan] = []
    try:
        for selection in selections:
            group = groups_by_ref[(selection.kind, selection.key)]
            plan = ProcurementDemandPlan(
                product_id=group.product.id if group.product else None,
                product_name_snapshot=group.display_name[:255], jan_snapshot=group.jan[:32] if group.jan else None,
                planned_quantity=selection.planned_quantity, confirmed_demand_quantity_snapshot=group.confirmed_quantity,
                status="planned", created_by=created_by, note=(selection.note or "").strip() or None,
            )
            session.add(plan)
            session.flush()
            for demand in group.confirmed_demands:
                session.add(ProcurementDemandPlanSource(plan_id=plan.id, demand_id=demand.id, quantity_snapshot=demand.requested_quantity))
                demand.status = "planned"
            plans.append(plan)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return plans


def list_plans(session: Session, *, status: str | None = "planned") -> list[ProcurementDemandPlan]:
    query = select(ProcurementDemandPlan).options(
        selectinload(ProcurementDemandPlan.product),
        selectinload(ProcurementDemandPlan.selected_store),
        selectinload(ProcurementDemandPlan.sources).selectinload(ProcurementDemandPlanSource.demand),
    ).order_by(ProcurementDemandPlan.created_at.desc())
    if status is not None:
        query = query.where(ProcurementDemandPlan.status == status)
    return list(session.scalars(query))


# ---------------- purchase source recommendation (Phase 2C) ----------------
#
# "Where did we actually buy this before" is read straight from PurchaseBatch/
# PurchaseBatchItem/Store history via store_service -- never a hand-maintained
# product-store mapping table. Recommendations and coverage are computed fresh
# on every read, never persisted: only 老婆's own selected_store_id is a stored
# fact (see ProcurementDemandPlan.selected_store_id).

STORE_HISTORY_PREVIEW_COUNT = 2


@dataclass(frozen=True, slots=True)
class StoreHistoryEntry:
    store_id: int
    store_name: str
    purchase_count: int
    last_purchase_at: datetime | None
    last_unit_price: Decimal | None
    min_unit_price: Decimal | None


def get_purchase_source_history(session: Session, product_ids: list[int]) -> dict[int, list[StoreHistoryEntry]]:
    """Every store each product was historically bought from, most recent first. Batched: one query set for all products."""
    unique_ids = list(dict.fromkeys(product_ids))
    bulk = product_store_summaries_bulk(session, unique_ids)
    return {
        product_id: [
            StoreHistoryEntry(
                store_id=row["store"].id, store_name=row["store"].display_name,
                purchase_count=row["purchase_count"], last_purchase_at=row["latest_date"],
                last_unit_price=row["latest_price"], min_unit_price=row["minimum_price"],
            )
            for row in rows
        ]
        for product_id, rows in bulk.items()
    }


def recommended_store_entry(entries: list[StoreHistoryEntry]) -> StoreHistoryEntry | None:
    """The single best-guess store for one product's own history: most recent, then most frequent, then cheapest.

    This never considers other products on the page -- see build_store_coverage
    for the cross-product "can one store cover several of these" picture, which
    老婆 weighs herself rather than the system silently overriding this pick.
    """
    if not entries:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    def sort_key(entry: StoreHistoryEntry) -> tuple:
        price_rank = -(entry.min_unit_price if entry.min_unit_price is not None else Decimal("Infinity"))
        return (entry.last_purchase_at or epoch, entry.purchase_count, price_rank)
    return max(entries, key=sort_key)


@dataclass(frozen=True, slots=True)
class StoreCoverage:
    """How many of the currently-listed products a store has ANY purchase history for -- not real-time stock."""

    store_id: int
    store_name: str
    covered_product_ids: frozenset[int]

    @property
    def covered_count(self) -> int:
        return len(self.covered_product_ids)


def build_store_coverage(history_by_product: dict[int, list[StoreHistoryEntry]]) -> list[StoreCoverage]:
    by_store: dict[int, tuple[str, set[int]]] = {}
    for product_id, entries in history_by_product.items():
        for entry in entries:
            _, covered = by_store.setdefault(entry.store_id, (entry.store_name, set()))
            covered.add(product_id)
    coverages = [
        StoreCoverage(store_id=store_id, store_name=name, covered_product_ids=frozenset(covered))
        for store_id, (name, covered) in by_store.items()
    ]
    return sorted(coverages, key=lambda coverage: (-coverage.covered_count, coverage.store_name))


@dataclass(frozen=True, slots=True)
class PlanStoreContext:
    history: list[StoreHistoryEntry]
    recommended_store_id: int | None
    recommended_store_name: str | None


def build_plan_store_contexts(session: Session, plans: list[ProcurementDemandPlan]) -> dict[int, PlanStoreContext]:
    """One batched history lookup for every plan on the page -- callers must not query per-card."""
    contexts, _coverage = build_plan_store_overview(session, plans)
    return contexts


def build_plan_store_overview(
    session: Session, plans: list[ProcurementDemandPlan],
) -> tuple[dict[int, PlanStoreContext], list[StoreCoverage]]:
    """Per-plan history/recommendation plus the page-level coverage board, from a single history lookup."""
    product_ids = [plan.product_id for plan in plans if plan.product_id is not None]
    history_by_product = get_purchase_source_history(session, product_ids)
    contexts: dict[int, PlanStoreContext] = {}
    for plan in plans:
        entries = history_by_product.get(plan.product_id, []) if plan.product_id is not None else []
        recommended = recommended_store_entry(entries)
        contexts[plan.id] = PlanStoreContext(
            history=entries, recommended_store_id=recommended.store_id if recommended else None,
            recommended_store_name=recommended.store_name if recommended else None,
        )
    return contexts, build_store_coverage(history_by_product)


def set_plan_selected_store(session: Session, plan_id: int, store_id: int | None) -> ProcurementDemandPlan:
    plan = session.get(ProcurementDemandPlan, plan_id)
    if plan is None:
        raise LookupError("采购计划不存在")
    if store_id is not None:
        store = session.get(Store, store_id)
        if store is None:
            raise LookupError("门店不存在")
        if not store.is_active:
            raise ValueError("门店已停用，不能选择")
        plan.selected_store_id = store.id
    else:
        plan.selected_store_id = None
    session.commit()
    session.refresh(plan)
    return plan


def set_plans_selected_store_bulk(session: Session, plan_ids: list[int], store_id: int | None) -> list[ProcurementDemandPlan]:
    if not plan_ids:
        raise ValueError("请至少选择一个商品")
    store = None
    if store_id is not None:
        store = session.get(Store, store_id)
        if store is None:
            raise LookupError("门店不存在")
        if not store.is_active:
            raise ValueError("门店已停用，不能选择")
    unique_ids = list(dict.fromkeys(plan_ids))
    plans = list(session.scalars(select(ProcurementDemandPlan).where(ProcurementDemandPlan.id.in_(unique_ids))))
    if len(plans) != len(unique_ids):
        raise LookupError("部分采购计划不存在")
    for plan in plans:
        plan.selected_store_id = store.id if store else None
    session.commit()
    return plans


@dataclass(frozen=True, slots=True)
class PlanStoreGroup:
    store_id: int | None
    store_name: str
    plans: list[ProcurementDemandPlan]

    @property
    def item_kind_count(self) -> int:
        return len(self.plans)

    @property
    def total_quantity(self) -> int:
        return sum(plan.planned_quantity for plan in self.plans)


UNASSIGNED_STORE_GROUP_NAME = "未确定来源"


def group_plans_by_selected_store(plans: list[ProcurementDemandPlan]) -> list[PlanStoreGroup]:
    """采购来源 grouping for the 按店铺 view. Unassigned plans always form their own trailing group."""
    grouped: dict[int | None, list[ProcurementDemandPlan]] = {}
    names: dict[int | None, str] = {}
    for plan in plans:
        key = plan.selected_store_id
        grouped.setdefault(key, []).append(plan)
        if key is not None and plan.selected_store is not None:
            names[key] = plan.selected_store.display_name
    known_groups = [
        PlanStoreGroup(store_id=key, store_name=names.get(key, "未知门店"), plans=items)
        for key, items in grouped.items() if key is not None
    ]
    known_groups.sort(key=lambda group: (-group.item_kind_count, group.store_name))
    if None in grouped:
        known_groups.append(PlanStoreGroup(store_id=None, store_name=UNASSIGNED_STORE_GROUP_NAME, plans=grouped[None]))
    return known_groups


# ---------------- purchase execution (Phase 2D) ----------------
#
# An execution is "we actually bought N of this, on this trip" -- real-world
# evidence, but not yet a formal PurchaseBatch (no price, no receipt, no QinSi
# posting). planned_quantity on the plan is a decision fact and is never
# touched here; purchased/remaining are always summed fresh from non-cancelled
# executions so a plan can be topped up across several trips/stores over time.

EXECUTION_STATUSES = {"pending_receipt", "reconciled", "cancelled"}
EXECUTION_STATUS_LABELS: dict[str, str] = {
    "pending_receipt": "已买待小票", "reconciled": "已对账", "cancelled": "已撤销",
}


@dataclass(frozen=True, slots=True)
class PlanExecutionSummary:
    planned_quantity: int
    purchased_quantity: int
    remaining_quantity: int

    @property
    def fully_purchased(self) -> bool:
        return self.remaining_quantity <= 0 and self.purchased_quantity > 0


def purchased_quantity_for_plans(session: Session, plan_ids: list[int]) -> dict[int, int]:
    """Sum of non-cancelled execution quantities per plan, batched for the whole list at once."""
    unique_ids = list(dict.fromkeys(plan_ids))
    if not unique_ids:
        return {}
    rows = session.execute(
        select(ProcurementPurchaseExecution.plan_id, func.coalesce(func.sum(ProcurementPurchaseExecution.quantity), 0))
        .where(ProcurementPurchaseExecution.plan_id.in_(unique_ids), ProcurementPurchaseExecution.status != "cancelled")
        .group_by(ProcurementPurchaseExecution.plan_id)
    ).all()
    totals = {plan_id: 0 for plan_id in unique_ids}
    totals.update({plan_id: int(total) for plan_id, total in rows})
    return totals


def build_plan_execution_summaries(session: Session, plans: list[ProcurementDemandPlan]) -> dict[int, PlanExecutionSummary]:
    """One batched purchased-quantity lookup for every plan on the page -- callers must not query per-card."""
    purchased = purchased_quantity_for_plans(session, [plan.id for plan in plans])
    return {
        plan.id: PlanExecutionSummary(
            planned_quantity=plan.planned_quantity, purchased_quantity=purchased.get(plan.id, 0),
            remaining_quantity=max(plan.planned_quantity - purchased.get(plan.id, 0), 0),
        )
        for plan in plans
    }


def remaining_quantity_for_plans(session: Session, plans: list[ProcurementDemandPlan]) -> dict[int, int]:
    return {plan_id: summary.remaining_quantity for plan_id, summary in build_plan_execution_summaries(session, plans).items()}


@dataclass(frozen=True, slots=True)
class ExecutionInput:
    plan_id: int
    quantity: int
    store_id: int | None = None
    note: str | None = None


def record_purchase_execution(
    session: Session, plan_id: int, quantity: int, *, store_id: int | None = None, note: str | None = None,
    commit: bool = True,
) -> ProcurementPurchaseExecution:
    plan = session.get(ProcurementDemandPlan, plan_id)
    if plan is None:
        raise LookupError("采购计划不存在")
    if quantity <= 0:
        raise ValueError("实际采购数量必须大于0")
    # Snapshot the store now (plan.selected_store_id if the row didn't specify
    # its own) -- see ProcurementPurchaseExecution.store_id for why this is
    # never re-derived later.
    resolved_store_id = store_id if store_id is not None else plan.selected_store_id
    if resolved_store_id is not None and session.get(Store, resolved_store_id) is None:
        raise LookupError("门店不存在")
    execution = ProcurementPurchaseExecution(
        plan_id=plan.id, store_id=resolved_store_id, quantity=quantity, status="pending_receipt",
        note=(note or "").strip() or None,
    )
    session.add(execution)
    if commit:
        session.commit()
    else:
        session.flush()
    return execution


def record_purchase_executions_bulk(session: Session, executions: list[ExecutionInput]) -> list[ProcurementPurchaseExecution]:
    if not executions:
        raise ValueError("请至少勾选一个已买到的商品")
    for entry in executions:
        if entry.quantity <= 0:
            raise ValueError("实际采购数量必须大于0")
    created: list[ProcurementPurchaseExecution] = []
    try:
        for entry in executions:
            created.append(record_purchase_execution(
                session, entry.plan_id, entry.quantity, store_id=entry.store_id, note=entry.note, commit=False,
            ))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return created


def cancel_purchase_execution(session: Session, execution_id: int) -> ProcurementPurchaseExecution:
    execution = session.get(ProcurementPurchaseExecution, execution_id)
    if execution is None:
        raise LookupError("采购执行记录不存在")
    if execution.status != "pending_receipt":
        raise ValueError("该记录当前状态不支持撤销")
    if confirmed_match_quantity_for_executions(session, [execution.id]).get(execution.id, 0) > 0:
        raise ValueError("该记录已有小票对账，不能直接撤销，请先处理对账")
    execution.status = "cancelled"
    session.commit()
    return execution


def list_plans_for_store_purchase(session: Session, store_id: int | None) -> list[ProcurementDemandPlan]:
    """Plans to show on one store's purchase checklist: matching selected_store_id (or unassigned) with remaining>0."""
    query = select(ProcurementDemandPlan).where(ProcurementDemandPlan.status == "planned").options(
        selectinload(ProcurementDemandPlan.product), selectinload(ProcurementDemandPlan.selected_store),
    )
    query = query.where(
        ProcurementDemandPlan.selected_store_id.is_(None) if store_id is None
        else ProcurementDemandPlan.selected_store_id == store_id
    )
    plans = list(session.scalars(query.order_by(ProcurementDemandPlan.created_at)))
    summaries = build_plan_execution_summaries(session, plans)
    return [plan for plan in plans if summaries[plan.id].remaining_quantity > 0]


@dataclass(frozen=True, slots=True)
class StorePurchaseEntry:
    """The 【开始采购】 entry-point stat for one store group: how much of it is still actually outstanding."""

    store_id: int | None
    store_name: str
    remaining_kind_count: int
    remaining_quantity: int


def build_store_purchase_entries(
    groups: list[PlanStoreGroup], summaries: dict[int, PlanExecutionSummary],
) -> list[StorePurchaseEntry]:
    entries = []
    for group in groups:
        remaining_plans = [plan for plan in group.plans if summaries[plan.id].remaining_quantity > 0]
        entries.append(StorePurchaseEntry(
            store_id=group.store_id, store_name=group.store_name,
            remaining_kind_count=len(remaining_plans),
            remaining_quantity=sum(summaries[plan.id].remaining_quantity for plan in remaining_plans),
        ))
    return entries


def list_pending_executions_for_plan(session: Session, plan_id: int) -> list[ProcurementPurchaseExecution]:
    return list(session.scalars(
        select(ProcurementPurchaseExecution)
        .where(ProcurementPurchaseExecution.plan_id == plan_id, ProcurementPurchaseExecution.status != "cancelled")
        .options(selectinload(ProcurementPurchaseExecution.store))
        .order_by(ProcurementPurchaseExecution.created_at.desc())
    ))


def build_plan_executions(session: Session, plans: list[ProcurementDemandPlan]) -> dict[int, list[ProcurementPurchaseExecution]]:
    """One batched query for every plan's non-cancelled execution history -- callers must not query per-card."""
    plan_ids = [plan.id for plan in plans]
    if not plan_ids:
        return {plan_id: [] for plan_id in plan_ids}
    rows = list(session.scalars(
        select(ProcurementPurchaseExecution)
        .where(ProcurementPurchaseExecution.plan_id.in_(plan_ids), ProcurementPurchaseExecution.status != "cancelled")
        .options(selectinload(ProcurementPurchaseExecution.store))
        .order_by(ProcurementPurchaseExecution.created_at.desc())
    ))
    grouped: dict[int, list[ProcurementPurchaseExecution]] = {plan_id: [] for plan_id in plan_ids}
    for execution in rows:
        grouped[execution.plan_id].append(execution)
    return grouped


# ---------------- receipt reconciliation (Phase 2E) ----------------
#
# Turns a "已买待小票" execution into evidence backed by a real receipt line,
# feeding the SAME confirm_receipt() transaction that already turns a
# confirmed Receipt into PurchaseBatch/PurchaseBatchItem (see app/services.py
# and app/purchase_service.py). Nothing here creates a second purchase-fact
# table or a parallel confirm flow. A match is only ever written once a human
# has picked it during that same confirm -- there is no persisted "suggested"
# state, so suggestions are always computed fresh in
# find_execution_candidates_for_receipt_items.

RECONCILE_DATE_WINDOW = timedelta(days=7)
CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW = "high", "medium", "low"
CONFIDENCE_ORDER = {CONFIDENCE_HIGH: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_LOW: 2}
CONFIDENCE_LABELS: dict[str, str] = {CONFIDENCE_HIGH: "高可信", CONFIDENCE_MEDIUM: "中等可信", CONFIDENCE_LOW: "低可信"}


def confirmed_match_quantity_for_executions(session: Session, execution_ids: list[int]) -> dict[int, int]:
    unique_ids = list(dict.fromkeys(execution_ids))
    if not unique_ids:
        return {}
    rows = session.execute(
        select(ProcurementExecutionReceiptMatch.execution_id, func.coalesce(func.sum(ProcurementExecutionReceiptMatch.matched_quantity), 0))
        .where(ProcurementExecutionReceiptMatch.execution_id.in_(unique_ids))
        .group_by(ProcurementExecutionReceiptMatch.execution_id)
    ).all()
    totals = {execution_id: 0 for execution_id in unique_ids}
    totals.update({execution_id: int(total) for execution_id, total in rows})
    return totals


def confirmed_match_quantity_for_receipt_items(session: Session, item_ids: list[int]) -> dict[int, int]:
    unique_ids = list(dict.fromkeys(item_ids))
    if not unique_ids:
        return {}
    rows = session.execute(
        select(ProcurementExecutionReceiptMatch.receipt_item_id, func.coalesce(func.sum(ProcurementExecutionReceiptMatch.matched_quantity), 0))
        .where(ProcurementExecutionReceiptMatch.receipt_item_id.in_(unique_ids))
        .group_by(ProcurementExecutionReceiptMatch.receipt_item_id)
    ).all()
    totals = {item_id: 0 for item_id in unique_ids}
    totals.update({item_id: int(total) for item_id, total in rows})
    return totals


def remaining_reconcile_quantity(session: Session, execution: ProcurementPurchaseExecution) -> int:
    confirmed = confirmed_match_quantity_for_executions(session, [execution.id]).get(execution.id, 0)
    return max(execution.quantity - confirmed, 0)


@dataclass(frozen=True, slots=True)
class PlanReconciliationSummary:
    """已买 / 已对账 / 待小票 for one plan, aggregated across all its non-cancelled executions."""

    purchased_quantity: int
    reconciled_quantity: int
    pending_receipt_quantity: int

    @property
    def fully_reconciled(self) -> bool:
        return self.purchased_quantity > 0 and self.pending_receipt_quantity <= 0


def build_plan_reconciliation_summaries(
    session: Session, plans: list[ProcurementDemandPlan],
) -> dict[int, PlanReconciliationSummary]:
    """One batched lookup for every plan's reconciliation state -- callers must not query per-card."""
    executions_by_plan = build_plan_executions(session, plans)
    all_execution_ids = [execution.id for executions in executions_by_plan.values() for execution in executions]
    confirmed_totals = confirmed_match_quantity_for_executions(session, all_execution_ids)
    summaries: dict[int, PlanReconciliationSummary] = {}
    for plan in plans:
        executions = executions_by_plan.get(plan.id, [])
        purchased = sum(execution.quantity for execution in executions)
        # An execution's own reconciled share never exceeds its own quantity,
        # even though the raw confirmed-match sum theoretically could not
        # anyway (confirm_execution_receipt_matches enforces that already).
        reconciled = sum(
            min(confirmed_totals.get(execution.id, 0), execution.quantity) for execution in executions
        )
        summaries[plan.id] = PlanReconciliationSummary(
            purchased_quantity=purchased, reconciled_quantity=reconciled,
            pending_receipt_quantity=max(purchased - reconciled, 0),
        )
    return summaries


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _candidate_confidence(execution: ProcurementPurchaseExecution, receipt: Receipt) -> str:
    date_near = True
    if receipt.purchased_at is not None:
        date_near = abs(_aware(receipt.purchased_at) - _aware(execution.created_at)) <= RECONCILE_DATE_WINDOW
    if execution.store_id is not None and receipt.store_id is not None:
        if execution.store_id != receipt.store_id:
            return CONFIDENCE_LOW  # known mismatch -- still selectable, never auto-preferred
        return CONFIDENCE_HIGH if date_near else CONFIDENCE_MEDIUM
    return CONFIDENCE_MEDIUM  # store unknown on one side or both


@dataclass(frozen=True, slots=True)
class ExecutionCandidate:
    execution: ProcurementPurchaseExecution
    confidence: str
    remaining_to_reconcile: int
    suggested_quantity: int


def find_execution_candidates_for_receipt_items(
    session: Session, receipt: Receipt,
) -> dict[int, list[ExecutionCandidate]]:
    """Candidate pending_receipt executions per active receipt item, batched across the whole receipt.

    Never matches on name/JAN guessing: an item with no resolved product_id
    gets no candidates at all. A plain, non-plan-scoped receipt (no matching
    execution ever existed) simply gets an empty candidate list per item and
    must continue through the ordinary receipt-confirm path untouched.
    """
    active_items = [item for item in receipt.items if item.review_status != "ignored"]
    product_ids = {item.product_id for item in active_items if item.product_id is not None}
    if not product_ids:
        return {item.id: [] for item in active_items}
    executions = list(session.scalars(
        select(ProcurementPurchaseExecution)
        .join(ProcurementDemandPlan, ProcurementDemandPlan.id == ProcurementPurchaseExecution.plan_id)
        .where(
            ProcurementDemandPlan.product_id.in_(product_ids),
            ProcurementPurchaseExecution.status == "pending_receipt",
        )
        .options(selectinload(ProcurementPurchaseExecution.plan), selectinload(ProcurementPurchaseExecution.store))
    ))
    if not executions:
        return {item.id: [] for item in active_items}
    confirmed_totals = confirmed_match_quantity_for_executions(session, [execution.id for execution in executions])
    by_product: dict[int, list[ProcurementPurchaseExecution]] = {}
    for execution in executions:
        by_product.setdefault(execution.plan.product_id, []).append(execution)
    result: dict[int, list[ExecutionCandidate]] = {}
    for item in active_items:
        candidates = []
        for execution in by_product.get(item.product_id, []):
            remaining = max(execution.quantity - confirmed_totals.get(execution.id, 0), 0)
            if remaining <= 0:
                continue
            candidates.append(ExecutionCandidate(
                execution=execution, confidence=_candidate_confidence(execution, receipt),
                remaining_to_reconcile=remaining, suggested_quantity=min(item.quantity, remaining),
            ))
        candidates.sort(key=lambda candidate: CONFIDENCE_ORDER[candidate.confidence])
        result[item.id] = candidates
    return result


@dataclass(frozen=True, slots=True)
class ExecutionMatchInput:
    item_id: int
    execution_id: int
    matched_quantity: int


def confirm_execution_receipt_matches(
    session: Session, receipt: Receipt, matches: list[ExecutionMatchInput],
) -> list[ProcurementExecutionReceiptMatch]:
    """Writes human-confirmed matches and reconciles executions. Caller commits (see services.confirm_receipt).

    Deliberately not called for every receipt -- a receipt with no matches
    submitted (the common case: nothing was pre-planned, or nothing matched)
    just does nothing here, and confirm_receipt's PurchaseBatch creation
    proceeds exactly as before.
    """
    if not matches:
        return []
    item_by_id = {item.id: item for item in receipt.items}
    execution_ids = list(dict.fromkeys(match.execution_id for match in matches))
    executions = {
        execution.id: execution
        for execution in session.scalars(select(ProcurementPurchaseExecution).where(ProcurementPurchaseExecution.id.in_(execution_ids)))
    }
    exec_running = confirmed_match_quantity_for_executions(session, execution_ids)
    item_running = confirmed_match_quantity_for_receipt_items(session, [match.item_id for match in matches])
    seen_pairs: set[tuple[int, int]] = set()
    created: list[ProcurementExecutionReceiptMatch] = []
    for match in matches:
        if match.matched_quantity <= 0:
            raise ValueError("对账数量必须大于0")
        item = item_by_id.get(match.item_id)
        if item is None:
            raise LookupError("小票商品行不存在")
        execution = executions.get(match.execution_id)
        if execution is None:
            raise LookupError("采购执行记录不存在")
        if execution.status != "pending_receipt":
            raise ValueError("该采购执行记录当前状态不支持对账")
        pair = (execution.id, item.id)
        if pair in seen_pairs:
            raise ValueError("同一采购执行与小票商品行不能重复提交对账")
        seen_pairs.add(pair)
        if exec_running.get(execution.id, 0) + match.matched_quantity > execution.quantity:
            raise ValueError("对账数量超过该采购执行的实际采购数量")
        if item_running.get(item.id, 0) + match.matched_quantity > item.quantity:
            raise ValueError("对账数量超过该小票商品行数量")
        exec_running[execution.id] = exec_running.get(execution.id, 0) + match.matched_quantity
        item_running[item.id] = item_running.get(item.id, 0) + match.matched_quantity
        row = ProcurementExecutionReceiptMatch(
            execution_id=execution.id, receipt_item_id=item.id, matched_quantity=match.matched_quantity,
        )
        session.add(row)
        created.append(row)
    session.flush()
    for execution_id in execution_ids:
        execution = executions[execution_id]
        if exec_running.get(execution_id, 0) >= execution.quantity:
            execution.status = "reconciled"
    return created
