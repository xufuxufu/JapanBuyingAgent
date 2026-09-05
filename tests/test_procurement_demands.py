from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Product, ProcurementDemand
from app.procurement_service import (
    PlanSelectionInput, aggregate_open_demand_groups, close_investigation_demand,
    create_channel_shortage_demand, create_investigation_demand, create_manual_restock_demand,
    create_plans, default_planned_quantity_for_group, get_group, list_investigation_demands,
    list_open_demands, list_plans, sync_demand_for_sales_order_item, sync_demands_for_sales_order,
)
from app.sales_order_service import (
    SalesOrderItemInput, create_customer, create_sales_order, ensure_default_salesperson,
    update_sales_order_status,
)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"PD-{suffix:0>4}", jan=f"0490100000{suffix:0>3}", name_cn=f"采购需求商品{suffix}")
    db.add(item)
    db.flush()
    return item


def customer(db, name: str = "需求客户"):
    return create_customer(db, name=name, phone="13900000000", wechat_name="wx_demand")


def sales_order_with_product(db, prod: Product, *, quantity: int = 2):
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db)
    return create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=prod.id, manual_name=None, jan=None, quantity=quantity, unit_sale_price=Decimal("500"))],
    )


# ---------------- raw source generation ----------------


def test_sales_order_generates_confirmed_demand(db_session):
    prod = product(db_session, "1")
    order = sales_order_with_product(db_session, prod, quantity=2)
    demands = db_session.query(ProcurementDemand).filter_by(sales_order_item_id=order.items[0].id).all()
    assert len(demands) == 1
    demand = demands[0]
    assert demand.demand_type == "sales_confirmed"
    assert demand.source_person == "秀"
    assert demand.source_type == "sales_order"
    assert demand.source_channel == "微信"
    assert demand.requested_quantity == 2
    assert demand.product_id == prod.id


def test_sales_order_item_demand_generation_is_idempotent(db_session):
    prod = product(db_session, "2")
    order = sales_order_with_product(db_session, prod, quantity=3)
    item = order.items[0]
    first = sync_demand_for_sales_order_item(db_session, item)
    second = sync_demand_for_sales_order_item(db_session, item)
    assert first.id == second.id
    count = db_session.query(ProcurementDemand).filter_by(sales_order_item_id=item.id).count()
    assert count == 1


def test_cancelled_sales_order_excluded_from_open_demands(db_session):
    prod = product(db_session, "3")
    order = sales_order_with_product(db_session, prod, quantity=1)
    update_sales_order_status(db_session, order.id, "cancelled")
    open_demands = list_open_demands(db_session)
    assert all(d.sales_order_item_id != order.items[0].id for d in open_demands)


def test_channel_shortage_demand_created_by_mother_in_law(db_session):
    prod = product(db_session, "4")
    demand = create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3, note="淘宝卖完了")
    assert demand.source_person == "丈母娘"
    assert demand.demand_type == "channel_shortage"
    assert demand.source_channel == "国内销售渠道"
    assert demand.note == "淘宝卖完了"


def test_investigation_demand_quantity_can_be_empty(db_session):
    demand = create_investigation_demand(db_session, manual_name="粉色大号系列", quantity=None, note="看看有没有粉色、大号，拍照片")
    assert demand.requested_quantity is None
    assert demand.demand_type == "investigation"
    assert demand.source_person == "秀"
    assert demand.source_channel == "微信"
    assert demand.product_id is None


def test_investigation_demand_not_counted_as_confirmed(db_session):
    prod = product(db_session, "5")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3)
    create_investigation_demand(db_session, product_id=prod.id, quantity=1)
    groups = {g.key: g for g in aggregate_open_demand_groups(db_session)}
    group = groups[str(prod.id)]
    assert group.confirmed_quantity == 3
    assert len(group.investigation_demands) == 1


def test_source_traceable_back_to_sales_order_item(db_session):
    prod = product(db_session, "6")
    order = sales_order_with_product(db_session, prod, quantity=2)
    demand = db_session.query(ProcurementDemand).filter_by(sales_order_item_id=order.items[0].id).one()
    assert demand.sales_order_item.sales_order_id == order.id


# ---------------- aggregation ----------------


def test_multiple_sources_aggregate_into_one_product_group(db_session):
    prod = product(db_session, "7")
    sales_order_with_product(db_session, prod, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3)
    groups = aggregate_open_demand_groups(db_session)
    group = next(g for g in groups if g.kind == "product" and g.key == str(prod.id))
    assert group.confirmed_quantity == 5
    assert len(group.confirmed_demands) == 2


def test_open_groups_sorted_newest_demand_first(db_session):
    # Procurement demand center reorg: new demands sort created_at DESC, id
    # DESC -- the most recently reported need should surface first.
    prod_a = product(db_session, "21")
    prod_b = product(db_session, "22")
    create_channel_shortage_demand(db_session, product_id=prod_a.id, quantity=1)
    create_channel_shortage_demand(db_session, product_id=prod_b.id, quantity=1)
    groups = aggregate_open_demand_groups(db_session)
    keys = [(g.kind, g.key) for g in groups]
    assert keys.index(("product", str(prod_b.id))) < keys.index(("product", str(prod_a.id)))


def test_demand_summary_text_shows_reliable_source_breakdown(db_session):
    # source_person is a CHECK-constrained enum set programmatically at
    # creation (see ProcurementDemand) -- sales_confirmed is always "秀",
    # channel_shortage defaults to "丈母娘" -- so this breakdown never guesses
    # from any name text.
    prod = product(db_session, "23")
    sales_order_with_product(db_session, prod, quantity=1)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=2)
    group = get_group(db_session, "product", str(prod.id))
    assert group.demand_summary_text == "需采购 ×3（秀销售 ×1 · 丈母娘销售 ×2）"


def test_demand_summary_text_omits_breakdown_for_single_source(db_session):
    prod = product(db_session, "24")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    group = get_group(db_session, "product", str(prod.id))
    assert group.demand_summary_text == "需采购 ×5"


def test_different_products_are_not_merged(db_session):
    prod_a = product(db_session, "8")
    prod_b = product(db_session, "9")
    create_channel_shortage_demand(db_session, product_id=prod_a.id, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod_b.id, quantity=3)
    groups = aggregate_open_demand_groups(db_session)
    keys = {(g.kind, g.key) for g in groups}
    assert ("product", str(prod_a.id)) in keys
    assert ("product", str(prod_b.id)) in keys


def test_investigation_without_product_stands_alone(db_session):
    create_investigation_demand(db_session, manual_name="某系列 A", quantity=None)
    create_investigation_demand(db_session, manual_name="某系列 B", quantity=None)
    groups = aggregate_open_demand_groups(db_session)
    standalone = [g for g in groups if g.kind == "demand"]
    assert len(standalone) == 2
    for group in standalone:
        assert group.confirmed_quantity == 0
        assert len(group.all_demands) == 1


def test_group_lookup_by_kind_and_key(db_session):
    prod = product(db_session, "10")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=4)
    group = get_group(db_session, "product", str(prod.id))
    assert group is not None
    assert group.confirmed_quantity == 4


# ---------------- purchase decision (checkbox -> plan) ----------------


def test_default_planned_quantity_equals_confirmed_sum(db_session):
    prod = product(db_session, "11")
    sales_order_with_product(db_session, prod, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3)
    group = get_group(db_session, "product", str(prod.id))
    assert default_planned_quantity_for_group(group) == 5


def test_batch_checkbox_creates_plans_for_multiple_products(db_session):
    prod_a = product(db_session, "12")
    prod_b = product(db_session, "13")
    create_channel_shortage_demand(db_session, product_id=prod_a.id, quantity=5)
    create_channel_shortage_demand(db_session, product_id=prod_b.id, quantity=3)
    plans = create_plans(db_session, [
        PlanSelectionInput(kind="product", key=str(prod_a.id), planned_quantity=6),
        PlanSelectionInput(kind="product", key=str(prod_b.id), planned_quantity=3),
    ])
    assert len(plans) == 2
    quantities = {plan.product_id: plan.planned_quantity for plan in plans}
    assert quantities[prod_a.id] == 6
    assert quantities[prod_b.id] == 3


def test_user_can_edit_planned_quantity_above_default(db_session):
    prod = product(db_session, "14")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=6)])
    assert plan.planned_quantity == 6
    assert plan.confirmed_demand_quantity_snapshot == 5


def test_plan_keeps_original_demands_undeleted(db_session):
    prod = product(db_session, "15")
    demand = create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=5)])
    db_session.refresh(demand)
    assert demand.status == "planned"
    still_exists = db_session.get(ProcurementDemand, demand.id)
    assert still_exists is not None


def test_plan_source_breakdown_still_visible(db_session):
    prod = product(db_session, "16")
    sales_order_with_product(db_session, prod, quantity=2)
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=5)])
    source_types = sorted(source.demand.demand_type for source in plan.sources)
    assert source_types == ["channel_shortage", "sales_confirmed"]


def test_planned_demand_excluded_from_default_open_view(db_session):
    prod = product(db_session, "17")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=5)
    create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=5)])
    groups = aggregate_open_demand_groups(db_session)
    assert not any(g.kind == "product" and g.key == str(prod.id) for g in groups)


def test_plan_without_confirmed_demand_rejected(db_session):
    prod = product(db_session, "18")
    create_investigation_demand(db_session, product_id=prod.id, quantity=1)
    with pytest.raises(ValueError):
        create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=1)])


def test_list_plans_returns_planned_entries(db_session):
    prod = product(db_session, "19")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=2)
    create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=2)])
    plans = list_plans(db_session, status="planned")
    assert len(plans) == 1
    assert plans[0].product_id == prod.id


# ---------------- investigation lifecycle ----------------


def test_investigation_demand_can_be_closed_with_note(db_session):
    demand = create_investigation_demand(db_session, manual_name="看看有没有粉色", quantity=None)
    closed = close_investigation_demand(db_session, demand.id, note="现场没有粉色，只有蓝色")
    assert closed.status == "closed"
    assert "现场没有粉色" in closed.note


def test_manual_restock_demand_counts_as_confirmed(db_session):
    prod = product(db_session, "20")
    create_manual_restock_demand(db_session, product_id=prod.id, quantity=2, source_person="老婆")
    group = get_group(db_session, "product", str(prod.id))
    assert group.confirmed_quantity == 2
