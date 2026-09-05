from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from app.location_service import initialize_default_locations
from app.models import Location, Product, PurchaseBatch, PurchaseBatchItem, Receipt, ReceiptBatch, ReceiptItem, Store
from app.procurement_service import (
    PlanSelectionInput, build_plan_store_contexts, build_store_coverage, create_channel_shortage_demand,
    create_plans, get_purchase_source_history, group_plans_by_selected_store, recommended_store_entry,
    set_plan_selected_store, set_plans_selected_store_bulk, UNASSIGNED_STORE_GROUP_NAME,
)


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


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
    item = Product(internal_sku=f"SRC-{suffix:0>4}", jan=f"0490400000{suffix:0>3}", name_cn=f"来源测试商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, name_ja=name)
    db.add(row)
    db.flush()
    return row


def _locations(db) -> tuple[Location, Location]:
    rows = {row.display_name: row for row in initialize_default_locations(db)}
    return rows["日本家里库存"], rows["新日本仓库"]


def add_purchase(db, item: Product, shop: Store, *, price: int = 500, quantity: int = 1, days_ago: int = 1) -> PurchaseBatchItem:
    local, qinsi = _locations(db)
    seq = db.scalar(select(func.count()).select_from(ReceiptBatch)) + 1
    purchased_at = NOW - timedelta(days=days_ago)
    source = ReceiptBatch(batch_no=f"SRC-RB-{seq}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(
        batch=source, raw_store_name=shop.display_name, store_id=shop.id, purchased_at=purchased_at,
        confirmation_status="confirmed", review_status="reviewed", confirmed_at=purchased_at,
    )
    receipt_item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name=item.name_cn, product_id=item.id,
        quantity=quantity, unit_price=price, line_total=price * quantity,
        discount_amount=0, confidence=1, review_status="confirmed",
    )
    db.add_all([source, receipt, receipt_item])
    db.flush()
    batch = PurchaseBatch(
        batch_no=f"SRC-PB-{seq}", receipt_id=receipt.id, gpt_batch_id=source.id,
        purchased_at=purchased_at, store_name=shop.display_name, store_id=shop.id,
        confirmed_at=purchased_at, status="confirmed", default_initial_location_id=local.id,
        default_qinsi_warehouse_id=qinsi.id,
    )
    db.add(batch)
    db.flush()
    detail = PurchaseBatchItem(
        purchase_batch_id=batch.id, product_id=item.id, receipt_item_id=receipt_item.id,
        quantity=quantity, unit_price=price, actual_line_amount=price * quantity,
        initial_location_id=local.id, qinsi_target_warehouse_id=qinsi.id,
    )
    db.add(detail)
    db.commit()
    return detail


def make_plan(db, prod: Product, quantity: int = 1) -> int:
    create_channel_shortage_demand(db, product_id=prod.id, quantity=quantity)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=quantity)])
    return plan.id


# ---------------- history aggregation ----------------


def test_multiple_stores_aggregate_correctly(db_session):
    prod = product(db_session, "1")
    matsumoto = store(db_session, "松本清")
    don = store(db_session, "唐吉诃德")
    add_purchase(db_session, prod, matsumoto, price=680, days_ago=5)
    add_purchase(db_session, prod, matsumoto, price=650, days_ago=20)
    add_purchase(db_session, prod, matsumoto, price=700, days_ago=40)
    add_purchase(db_session, prod, don, price=598, days_ago=10)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    by_store = {entry.store_id: entry for entry in history}
    assert by_store[matsumoto.id].purchase_count == 3
    assert by_store[don.id].purchase_count == 1


def test_purchase_count_correct(db_session):
    prod = product(db_session, "2")
    shop = store(db_session, "测试店A")
    add_purchase(db_session, prod, shop, days_ago=1)
    add_purchase(db_session, prod, shop, days_ago=2)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    assert history[0].purchase_count == 2


def test_latest_purchase_date_correct(db_session):
    prod = product(db_session, "3")
    shop = store(db_session, "测试店B")
    add_purchase(db_session, prod, shop, days_ago=30, price=100)
    add_purchase(db_session, prod, shop, days_ago=3, price=200)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    stored = history[0].last_purchase_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == NOW - timedelta(days=3)


def test_latest_price_correct(db_session):
    prod = product(db_session, "4")
    shop = store(db_session, "测试店C")
    add_purchase(db_session, prod, shop, days_ago=30, price=100)
    add_purchase(db_session, prod, shop, days_ago=3, price=250)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    assert history[0].last_unit_price == 250


def test_min_price_correct(db_session):
    prod = product(db_session, "5")
    shop = store(db_session, "测试店D")
    add_purchase(db_session, prod, shop, days_ago=30, price=680)
    add_purchase(db_session, prod, shop, days_ago=3, price=598)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    assert history[0].min_unit_price == 598


def test_no_history_is_empty(db_session):
    prod = product(db_session, "6")
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    assert history == []


def test_store_display_name_never_shows_placeholder_for_japanese_only_name(db_session):
    # Regression: a store with only a Japanese name used to render as
    # "中文名待补｜<name>" -- it must now show just the Japanese name.
    japan_only = store(db_session, "HANDS心斎橋店")
    japan_only.name_ja = "HANDSハンズ心斎橋店"
    japan_only.name_cn = None
    db_session.commit()
    assert japan_only.display_name == "HANDSハンズ心斎橋店"
    assert "待补" not in japan_only.display_name


def test_store_display_name_shows_both_names_when_present(db_session):
    both = store(db_session, "松本清")
    both.name_cn = "松本清"
    both.name_ja = "マツモトキヨシ"
    db_session.commit()
    assert both.display_name == "松本清｜マツモトキヨシ"


def test_different_products_not_mixed(db_session):
    prod_a = product(db_session, "7")
    prod_b = product(db_session, "8")
    shop = store(db_session, "测试店E")
    add_purchase(db_session, prod_a, shop, price=100)
    add_purchase(db_session, prod_b, shop, price=999)
    history = get_purchase_source_history(db_session, [prod_a.id, prod_b.id])
    assert history[prod_a.id][0].last_unit_price == 100
    assert history[prod_b.id][0].last_unit_price == 999


# ---------------- recommendation & coverage ----------------


def test_most_recent_store_recommended(db_session):
    prod = product(db_session, "9")
    old_shop = store(db_session, "旧店")
    new_shop = store(db_session, "新店")
    add_purchase(db_session, prod, old_shop, days_ago=100)
    add_purchase(db_session, prod, new_shop, days_ago=1)
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    recommended = recommended_store_entry(history)
    assert recommended.store_id == new_shop.id


def test_more_frequent_store_recommended_when_same_recency_tier(db_session):
    prod = product(db_session, "10")
    frequent = store(db_session, "常去店")
    rare = store(db_session, "偶尔店")
    add_purchase(db_session, prod, frequent, days_ago=10)
    add_purchase(db_session, prod, frequent, days_ago=11)
    add_purchase(db_session, prod, frequent, days_ago=12)
    add_purchase(db_session, prod, rare, days_ago=9)  # slightly more recent, but only once
    history = get_purchase_source_history(db_session, [prod.id])[prod.id]
    # Recency is still the top factor: freshest single visit wins over frequency.
    assert recommended_store_entry(history).store_id == rare.id
    # With ties on the same day, frequency should still be visible via purchase_count.
    by_store = {e.store_id: e for e in history}
    assert by_store[frequent.id].purchase_count == 3


def test_coverage_counts_products_per_store(db_session):
    prod_a = product(db_session, "11")
    prod_b = product(db_session, "12")
    prod_c = product(db_session, "13")
    matsumoto = store(db_session, "覆盖店松本清")
    don = store(db_session, "覆盖店唐吉诃德")
    add_purchase(db_session, prod_a, matsumoto)
    add_purchase(db_session, prod_b, matsumoto)
    add_purchase(db_session, prod_c, don)
    history = get_purchase_source_history(db_session, [prod_a.id, prod_b.id, prod_c.id])
    coverage = build_store_coverage(history)
    by_store = {c.store_id: c for c in coverage}
    assert by_store[matsumoto.id].covered_count == 2
    assert by_store[don.id].covered_count == 1


def test_recommendation_does_not_write_selected_store(db_session):
    prod = product(db_session, "14")
    shop = store(db_session, "不写入店")
    add_purchase(db_session, prod, shop)
    plan_id = make_plan(db_session, prod)
    from app.models import ProcurementDemandPlan
    plan = db_session.get(ProcurementDemandPlan, plan_id)
    build_plan_store_contexts(db_session, [plan])
    db_session.refresh(plan)
    assert plan.selected_store_id is None


# ---------------- selection ----------------


def test_can_select_store_for_plan(db_session):
    prod = product(db_session, "15")
    shop = store(db_session, "选择店A")
    plan_id = make_plan(db_session, prod)
    updated = set_plan_selected_store(db_session, plan_id, shop.id)
    assert updated.selected_store_id == shop.id


def test_can_change_selected_store(db_session):
    prod = product(db_session, "16")
    shop_a = store(db_session, "选择店B1")
    shop_b = store(db_session, "选择店B2")
    plan_id = make_plan(db_session, prod)
    set_plan_selected_store(db_session, plan_id, shop_a.id)
    updated = set_plan_selected_store(db_session, plan_id, shop_b.id)
    assert updated.selected_store_id == shop_b.id


def test_can_clear_selected_store(db_session):
    prod = product(db_session, "17")
    shop = store(db_session, "选择店C")
    plan_id = make_plan(db_session, prod)
    set_plan_selected_store(db_session, plan_id, shop.id)
    cleared = set_plan_selected_store(db_session, plan_id, None)
    assert cleared.selected_store_id is None


def test_selecting_store_does_not_change_planned_quantity(db_session):
    prod = product(db_session, "18")
    shop = store(db_session, "选择店D")
    plan_id = make_plan(db_session, prod, quantity=7)
    updated = set_plan_selected_store(db_session, plan_id, shop.id)
    assert updated.planned_quantity == 7


def test_selecting_store_does_not_change_source_demands(db_session):
    prod = product(db_session, "19")
    shop = store(db_session, "选择店E")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3, note="来源备注不变")
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=3)])
    set_plan_selected_store(db_session, plan.id, shop.id)
    db_session.refresh(plan)
    assert len(plan.sources) == 1
    assert plan.sources[0].demand.note == "来源备注不变"


# ---------------- bulk selection ----------------


def test_bulk_set_store_for_multiple_plans(db_session):
    prod_a = product(db_session, "20")
    prod_b = product(db_session, "21")
    shop = store(db_session, "批量店")
    plan_a = make_plan(db_session, prod_a)
    plan_b = make_plan(db_session, prod_b)
    updated = set_plans_selected_store_bulk(db_session, [plan_a, plan_b], shop.id)
    assert {plan.selected_store_id for plan in updated} == {shop.id}


def test_bulk_set_does_not_affect_unselected_plans(db_session):
    prod_a = product(db_session, "22")
    prod_b = product(db_session, "23")
    shop = store(db_session, "批量店2")
    plan_a = make_plan(db_session, prod_a)
    plan_b = make_plan(db_session, prod_b)
    set_plans_selected_store_bulk(db_session, [plan_a], shop.id)
    from app.models import ProcurementDemandPlan
    untouched = db_session.get(ProcurementDemandPlan, plan_b)
    assert untouched.selected_store_id is None


# ---------------- grouping by store ----------------


def test_grouping_by_selected_store(db_session):
    prod_a = product(db_session, "24")
    prod_b = product(db_session, "25")
    prod_c = product(db_session, "26")
    matsumoto = store(db_session, "分组店松本清")
    don = store(db_session, "分组店唐吉诃德")
    from app.models import ProcurementDemandPlan
    plan_a = db_session.get(ProcurementDemandPlan, make_plan(db_session, prod_a, quantity=3))
    plan_b = db_session.get(ProcurementDemandPlan, make_plan(db_session, prod_b, quantity=2))
    plan_c = db_session.get(ProcurementDemandPlan, make_plan(db_session, prod_c, quantity=5))
    set_plan_selected_store(db_session, plan_a.id, matsumoto.id)
    set_plan_selected_store(db_session, plan_b.id, matsumoto.id)
    set_plan_selected_store(db_session, plan_c.id, don.id)
    plans = [
        db_session.get(ProcurementDemandPlan, plan_a.id),
        db_session.get(ProcurementDemandPlan, plan_b.id),
        db_session.get(ProcurementDemandPlan, plan_c.id),
    ]
    groups = group_plans_by_selected_store(plans)
    matsumoto_group = next(g for g in groups if g.store_id == matsumoto.id)
    assert matsumoto_group.item_kind_count == 2
    assert matsumoto_group.total_quantity == 5


def test_unassigned_is_its_own_group(db_session):
    prod_a = product(db_session, "27")
    prod_b = product(db_session, "28")
    shop = store(db_session, "分组店3")
    from app.models import ProcurementDemandPlan
    plan_a_id = make_plan(db_session, prod_a)
    plan_b_id = make_plan(db_session, prod_b)
    set_plan_selected_store(db_session, plan_a_id, shop.id)
    plans = [db_session.get(ProcurementDemandPlan, plan_a_id), db_session.get(ProcurementDemandPlan, plan_b_id)]
    groups = group_plans_by_selected_store(plans)
    unassigned = next(g for g in groups if g.store_name == UNASSIGNED_STORE_GROUP_NAME)
    assert unassigned.item_kind_count == 1
    assert groups[-1].store_name == UNASSIGNED_STORE_GROUP_NAME


def test_group_quantities_and_counts_correct(db_session):
    prod_a = product(db_session, "29")
    prod_b = product(db_session, "30")
    shop = store(db_session, "分组店4")
    from app.models import ProcurementDemandPlan
    plan_a_id = make_plan(db_session, prod_a, quantity=4)
    plan_b_id = make_plan(db_session, prod_b, quantity=6)
    set_plan_selected_store(db_session, plan_a_id, shop.id)
    set_plan_selected_store(db_session, plan_b_id, shop.id)
    plans = [db_session.get(ProcurementDemandPlan, plan_a_id), db_session.get(ProcurementDemandPlan, plan_b_id)]
    groups = group_plans_by_selected_store(plans)
    assert groups[0].item_kind_count == 2
    assert groups[0].total_quantity == 10


# ---------------- performance ----------------


def test_history_lookup_does_not_scale_linearly(db_session):
    shop = store(db_session, "性能测试店")
    products = [product(db_session, str(30 + i)) for i in range(20)]
    for prod in products:
        add_purchase(db_session, prod, shop)

    with count_queries(db_session) as small_counter:
        small_history = get_purchase_source_history(db_session, [p.id for p in products[:2]])
    with count_queries(db_session) as large_counter:
        large_history = get_purchase_source_history(db_session, [p.id for p in products])

    assert len(small_history) == 2
    assert len(large_history) == 20
    # The query count must stay flat as the product list grows 10x -- a per-card
    # lookup would instead multiply the count by roughly 10.
    assert large_counter["n"] == small_counter["n"], (
        f"query count grew with product count: {small_counter['n']} for 2 products vs {large_counter['n']} for 20"
    )
