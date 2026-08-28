from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.local_product import resolve_local_product_by_jan
from app.models import (
    Location, Product, ProcurementDemand, ProcurementDemandPlan, ProcurementDemandPlanSource,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, SalesOrder, SalesOrderItem,
)
from app.qinsi_inventory import INVENTORY_REGION_CHINA, INVENTORY_REGION_JAPAN, inventory_region_for_location


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
        selectinload(ProcurementDemandPlan.sources).selectinload(ProcurementDemandPlanSource.demand),
    ).order_by(ProcurementDemandPlan.created_at.desc())
    if status is not None:
        query = query.where(ProcurementDemandPlan.status == status)
    return list(session.scalars(query))
