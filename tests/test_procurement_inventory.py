from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models import Location, Product, QinsiInventorySnapshot, QinsiInventorySnapshotLine
from app.procurement_service import (
    aggregate_open_demand_groups, build_group_inventory_contexts, build_plan_inventory_contexts,
    create_channel_shortage_demand, create_investigation_demand, create_plans, get_group, PlanSelectionInput,
    reference_inventory_for_products,
)


@contextmanager
def count_queries(session):
    counter = {"n": 0}
    def _on_execute(*args, **kwargs):
        counter["n"] += 1
    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"INV-{suffix:0>4}", jan=f"0490300000{suffix:0>3}", name_cn=f"库存测试商品{suffix}")
    db.add(item)
    db.flush()
    return item


def china_warehouse(db) -> Location:
    loc = db.query(Location).filter_by(internal_code="QW-2025-QIANYU").first()
    if loc is None:
        loc = Location(internal_code="QW-2025-QIANYU", display_name="2025千羽", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
        db.add(loc)
        db.flush()
    return loc


def japan_warehouse(db) -> Location:
    loc = db.query(Location).filter_by(internal_code="QW-NEW-JAPAN").first()
    if loc is None:
        loc = Location(internal_code="QW-NEW-JAPAN", display_name="新日本仓库", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
        db.add(loc)
        db.flush()
    return loc


def unclassified_warehouse(db) -> Location:
    loc = db.query(Location).filter_by(internal_code="QW-NO-BARCODE").first()
    if loc is None:
        loc = Location(internal_code="QW-NO-BARCODE", display_name="无条码商品", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
        db.add(loc)
        db.flush()
    return loc


def snapshot(db, *, data_at=None) -> QinsiInventorySnapshot:
    row = QinsiInventorySnapshot(
        batch_no=f"QS-TEST-{db.query(QinsiInventorySnapshot).count() + 1}", original_filename="test.xlsx",
        file_hash=f"hash-{db.query(QinsiInventorySnapshot).count() + 1}", file_content=b"x",
        data_at=data_at, status="completed",
    )
    db.add(row)
    db.flush()
    return row


def line(db, snap, *, product_id=None, warehouse_id=None, quantity=None, matching_status="matched", row_no=1) -> QinsiInventorySnapshotLine:
    row = QinsiInventorySnapshotLine(
        snapshot_id=snap.id, original_row_no=row_no, raw_summary_json=json.dumps({}),
        product_id=product_id, warehouse_id=warehouse_id, quantity=quantity,
        matching_status=matching_status, warehouse_status="matched" if warehouse_id else "unknown",
    )
    db.add(row)
    db.flush()
    return row


def utcnow():
    return datetime.now(timezone.utc)


# ---------------- reference inventory reads ----------------


def test_finds_latest_snapshot(db_session):
    prod = product(db_session, "1")
    china = china_warehouse(db_session)
    old_snap = snapshot(db_session, data_at=utcnow() - timedelta(days=10))
    line(db_session, old_snap, product_id=prod.id, warehouse_id=china.id, quantity=99)
    new_snap = snapshot(db_session, data_at=utcnow() - timedelta(hours=1))
    line(db_session, new_snap, product_id=prod.id, warehouse_id=china.id, quantity=2)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.snapshot_id == new_snap.id
    assert result.china_quantity == 2


def test_multiple_china_warehouses_sum_correctly(db_session):
    prod = product(db_session, "2")
    china_a = china_warehouse(db_session)
    china_b = Location(internal_code="QW-2025-ZHAOCAIMAO", display_name="2025招财猫", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
    db_session.add(china_b)
    db_session.flush()
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china_a.id, quantity=3, row_no=1)
    line(db_session, snap, product_id=prod.id, warehouse_id=china_b.id, quantity=4, row_no=2)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.china_quantity == 7
    assert result.china_known is True


def test_multiple_japan_warehouses_sum_correctly(db_session):
    prod = product(db_session, "3")
    japan_a = japan_warehouse(db_session)
    japan_b = Location(internal_code="LOC-JP-HOME", display_name="日本家里库存", location_type="local_physical", is_qinsi_warehouse=True)
    db_session.add(japan_b)
    db_session.flush()
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=japan_a.id, quantity=5, row_no=1)
    line(db_session, snap, product_id=prod.id, warehouse_id=japan_b.id, quantity=6, row_no=2)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.japan_quantity == 11


def test_china_and_japan_not_summed_together(db_session):
    prod = product(db_session, "4")
    china = china_warehouse(db_session)
    japan = japan_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=1, row_no=1)
    line(db_session, snap, product_id=prod.id, warehouse_id=japan.id, quantity=10, row_no=2)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.china_quantity == 1
    assert result.japan_quantity == 10


def test_known_zero_is_distinct_from_unknown(db_session):
    prod = product(db_session, "5")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=0)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.china_quantity == 0
    assert result.china_known is True


def test_no_snapshot_is_unknown(db_session):
    prod = product(db_session, "6")
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.china_known is False
    assert result.japan_known is False
    assert result.freshness == "no_snapshot"


def test_snapshot_without_this_product_line_is_unknown(db_session):
    prod_a = product(db_session, "7")
    prod_b = product(db_session, "8")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod_a.id, warehouse_id=china.id, quantity=5)
    result = reference_inventory_for_products(db_session, [prod_a.id, prod_b.id])
    assert result[prod_a.id].china_known is True
    assert result[prod_b.id].china_known is False


def test_unclassified_warehouse_does_not_count_as_known(db_session):
    prod = product(db_session, "9")
    other = unclassified_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=other.id, quantity=5)
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.china_known is False
    assert result.japan_known is False


def test_manual_product_without_product_id_is_unknown(db_session):
    demand = create_investigation_demand(db_session, manual_name="纯手工商品", quantity=1)
    group = get_group(db_session, "demand", str(demand.id))
    contexts = build_group_inventory_contexts(db_session, [group])
    ctx = contexts[group.group_ref]
    assert ctx.inventory.china_known is False
    assert ctx.inventory.japan_known is False


# ---------------- domestic shortage ----------------


def test_shortage_demand5_china2_equals_3(db_session):
    prod = product(db_session, "10")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.domestic_shortage == 3
    assert ctx.domestic_shortage_known is True


def test_shortage_demand5_china8_equals_0(db_session):
    prod = product(db_session, "11")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=8)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.domestic_shortage == 0


def test_shortage_unknown_when_china_unknown(db_session):
    prod = product(db_session, "12")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.domestic_shortage is None
    assert ctx.domestic_shortage_known is False


def test_investigation_excluded_from_confirmed_total_used_in_shortage(db_session):
    prod = product(db_session, "13")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=0)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=2)
    create_investigation_demand(db_session, product_id=prod.id, quantity=100)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.confirmed_demand_quantity == 2
    assert ctx.domestic_shortage == 2


def test_cancelled_sales_order_demand_excluded_from_shortage(db_session):
    from decimal import Decimal
    from app.sales_order_service import SalesOrderItemInput, create_customer, create_sales_order, ensure_default_salesperson, update_sales_order_status
    prod = product(db_session, "14")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=0)
    salesperson = ensure_default_salesperson(db_session)
    buyer = create_customer(db_session, name="库存缺口测试客户", phone="13977778888")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=prod.id, manual_name=None, jan=None, quantity=9, unit_sale_price=Decimal("100"))],
    )
    update_sales_order_status(db_session, order.id, "cancelled")
    from app.procurement_service import aggregate_open_demand_groups
    assert not any(g.kind == "product" and g.key == str(prod.id) for g in aggregate_open_demand_groups(db_session))


def test_japan_stock_does_not_reduce_domestic_shortage(db_session):
    prod = product(db_session, "15")
    china = china_warehouse(db_session)
    japan = japan_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=1, row_no=1)
    line(db_session, snap, product_id=prod.id, warehouse_id=japan.id, quantity=100, row_no=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.domestic_shortage == 4
    assert ctx.inventory.japan_quantity == 100


# ---------------- default planned quantity ----------------


def test_default_planned_quantity_uses_shortage_when_china_known(db_session):
    prod = product(db_session, "16")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.default_planned_quantity == 3
    assert ctx.default_planned_quantity_basis == "shortage"


def test_default_planned_quantity_falls_back_to_confirmed_demand_when_unknown(db_session):
    prod = product(db_session, "17")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.default_planned_quantity == 5
    assert ctx.default_planned_quantity_basis == "confirmed_demand"


def test_planned_quantity_not_auto_modified_by_new_snapshot(db_session):
    prod = product(db_session, "18")
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=3)])
    # A brand-new, much larger snapshot arrives after the decision was made.
    later_snap = snapshot(db_session, data_at=utcnow() + timedelta(hours=1))
    line(db_session, later_snap, product_id=prod.id, warehouse_id=china.id, quantity=999)
    inventories = build_plan_inventory_contexts(db_session, [plan])
    assert plan.planned_quantity == 3
    assert inventories[plan.id].china_quantity == 999


# ---------------- freshness ----------------


def test_freshness_within_24h_is_fresh(db_session):
    prod = product(db_session, "19")
    snapshot(db_session, data_at=utcnow() - timedelta(hours=1))
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.freshness == "fresh"


def test_freshness_between_24_and_72h_is_stale(db_session):
    prod = product(db_session, "20")
    snapshot(db_session, data_at=utcnow() - timedelta(hours=48))
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.freshness == "stale"


def test_freshness_over_72h_is_expired(db_session):
    prod = product(db_session, "21")
    snapshot(db_session, data_at=utcnow() - timedelta(hours=100))
    result = reference_inventory_for_products(db_session, [prod.id])[prod.id]
    assert result.freshness == "expired"


# ---------------- JAN-only resolution ----------------


def test_jan_only_group_resolves_unique_product(db_session):
    prod = product(db_session, "22")
    prod.jan = "0490300000228"  # valid JAN-13 checksum, required by resolve_local_product_by_jan
    db_session.flush()
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=4)
    demand = create_channel_shortage_demand(db_session, manual_name="手工输入的JAN", product_id=None, quantity=1)
    # Simulate a demand that only carries a JAN (no product_id) by editing directly,
    # the way a manually-typed sales order JAN would arrive.
    demand.jan_snapshot = prod.jan
    db_session.commit()
    group = get_group(db_session, "jan", prod.jan)
    assert group is not None
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.inventory.china_known is True
    assert ctx.inventory.china_quantity == 4


def test_jan_ambiguous_does_not_resolve_inventory(db_session):
    from app.models import ProductBarcode
    prod_a = Product(internal_sku="INV-DUPA", jan=None, name_cn="共享条码甲")
    prod_b = Product(internal_sku="INV-DUPB", jan=None, name_cn="共享条码乙")
    db_session.add_all([prod_a, prod_b])
    db_session.flush()
    shared_barcode = "0490999999995"  # valid JAN-13 checksum, required by resolve_local_product_by_jan
    db_session.add_all([
        ProductBarcode(product_id=prod_a.id, barcode=shared_barcode, source_system="manual"),
        ProductBarcode(product_id=prod_b.id, barcode=shared_barcode, source_system="manual"),
    ])
    db_session.flush()
    demand = create_channel_shortage_demand(db_session, product_id=None, manual_name="共享条码商品", quantity=1)
    demand.jan_snapshot = shared_barcode
    db_session.commit()
    group = get_group(db_session, "jan", shared_barcode)
    ctx = build_group_inventory_contexts(db_session, [group])[group.group_ref]
    assert ctx.inventory.china_known is False


# ---------------- performance (no per-card N+1) ----------------


def test_inventory_lookup_does_not_scale_linearly_with_product_count(db_session):
    china = china_warehouse(db_session)
    snap = snapshot(db_session, data_at=utcnow())
    products = []
    for suffix in range(30, 40):
        prod = product(db_session, str(suffix))
        line(db_session, snap, product_id=prod.id, warehouse_id=china.id, quantity=1, row_no=suffix)
        create_channel_shortage_demand(db_session, product_id=prod.id, quantity=2)
        products.append(prod)
    db_session.commit()
    groups = [g for g in aggregate_open_demand_groups(db_session) if g.product and g.product.id in {p.id for p in products}]
    assert len(groups) == 10
    with count_queries(db_session) as counter:
        contexts = build_group_inventory_contexts(db_session, groups)
    assert len(contexts) == 10
    # One batch lookup for every card, not one query per card: stays small and
    # flat regardless of how many products are on the page.
    assert counter["n"] <= 5, f"expected a handful of batched queries, got {counter['n']}"
