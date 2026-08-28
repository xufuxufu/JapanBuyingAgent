from __future__ import annotations

import pytest

from app.models import Product, ProcurementDemandPlan, ProcurementPurchaseExecution, Store
from app.procurement_service import (
    ExecutionInput, PlanSelectionInput, build_plan_execution_summaries, build_plan_executions,
    build_store_purchase_entries, cancel_purchase_execution, create_channel_shortage_demand, create_plans,
    group_plans_by_selected_store, list_plans_for_store_purchase, purchased_quantity_for_plans,
    record_purchase_execution, record_purchase_executions_bulk, remaining_quantity_for_plans,
    set_plan_selected_store,
)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"EXE-{suffix:0>4}", jan=f"0490600000{suffix:0>3}", name_cn=f"执行测试商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def make_plan(db, prod: Product, quantity: int, *, store_row: Store | None = None) -> ProcurementDemandPlan:
    create_channel_shortage_demand(db, product_id=prod.id, quantity=quantity)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=quantity)])
    if store_row is not None:
        set_plan_selected_store(db, plan.id, store_row.id)
    return plan


# ---------------- execution basics ----------------


def test_checked_item_creates_execution(db_session):
    prod = product(db_session, "1")
    plan = make_plan(db_session, prod, 3)
    execution = record_purchase_execution(db_session, plan.id, 3)
    assert execution.plan_id == plan.id
    assert execution.status == "pending_receipt"


def test_unchecked_item_creates_nothing(db_session):
    prod = product(db_session, "2")
    plan = make_plan(db_session, prod, 3)
    # A bulk submit that only includes checked rows -- this plan was never
    # checked, so no ExecutionInput for it is ever built by the caller.
    count = db_session.query(ProcurementPurchaseExecution).filter_by(plan_id=plan.id).count()
    assert count == 0


def test_quantity_must_be_positive(db_session):
    prod = product(db_session, "3")
    plan = make_plan(db_session, prod, 3)
    with pytest.raises(ValueError):
        record_purchase_execution(db_session, plan.id, 0)
    with pytest.raises(ValueError):
        record_purchase_execution(db_session, plan.id, -1)


def test_default_actual_quantity_equals_remaining(db_session):
    prod = product(db_session, "4")
    plan = make_plan(db_session, prod, 5)
    record_purchase_execution(db_session, plan.id, 2)
    remaining = remaining_quantity_for_plans(db_session, [plan])[plan.id]
    assert remaining == 3  # this is what a fresh purchase page should default the input to


def test_partial_purchase_leaves_remaining(db_session):
    prod = product(db_session, "5")
    plan = make_plan(db_session, prod, 5)
    record_purchase_execution(db_session, plan.id, 2)
    summary = build_plan_execution_summaries(db_session, [plan])[plan.id]
    assert summary.purchased_quantity == 2
    assert summary.remaining_quantity == 3
    assert summary.fully_purchased is False


def test_multiple_purchases_accumulate(db_session):
    prod = product(db_session, "6")
    plan = make_plan(db_session, prod, 5)
    record_purchase_execution(db_session, plan.id, 2)
    record_purchase_execution(db_session, plan.id, 3)
    summary = build_plan_execution_summaries(db_session, [plan])[plan.id]
    assert summary.purchased_quantity == 5
    assert summary.remaining_quantity == 0


def test_remaining_correct_across_plans(db_session):
    prod_a = product(db_session, "7")
    prod_b = product(db_session, "8")
    plan_a = make_plan(db_session, prod_a, 4)
    plan_b = make_plan(db_session, prod_b, 6)
    record_purchase_execution(db_session, plan_a.id, 1)
    remaining = remaining_quantity_for_plans(db_session, [plan_a, plan_b])
    assert remaining[plan_a.id] == 3
    assert remaining[plan_b.id] == 6


def test_fully_purchased_remaining_zero(db_session):
    prod = product(db_session, "9")
    plan = make_plan(db_session, prod, 3)
    record_purchase_execution(db_session, plan.id, 3)
    summary = build_plan_execution_summaries(db_session, [plan])[plan.id]
    assert summary.remaining_quantity == 0
    assert summary.fully_purchased is True


def test_overbuy_allowed_and_counted(db_session):
    prod = product(db_session, "10")
    plan = make_plan(db_session, prod, 5)
    record_purchase_execution(db_session, plan.id, 7)
    summary = build_plan_execution_summaries(db_session, [plan])[plan.id]
    assert summary.purchased_quantity == 7
    assert summary.remaining_quantity == 0  # never negative


def test_planned_quantity_never_modified(db_session):
    prod = product(db_session, "11")
    plan = make_plan(db_session, prod, 5)
    record_purchase_execution(db_session, plan.id, 2)
    record_purchase_execution(db_session, plan.id, 10)
    db_session.refresh(plan)
    assert plan.planned_quantity == 5


# ---------------- store snapshotting ----------------


def test_execution_snapshots_store_from_plan(db_session):
    prod = product(db_session, "12")
    shop = store(db_session, "快照店")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    assert execution.store_id == shop.id


def test_changing_selected_store_does_not_affect_past_execution(db_session):
    prod = product(db_session, "13")
    shop_a = store(db_session, "旧快照店")
    shop_b = store(db_session, "新快照店")
    plan = make_plan(db_session, prod, 3, store_row=shop_a)
    execution = record_purchase_execution(db_session, plan.id, 3)
    set_plan_selected_store(db_session, plan.id, shop_b.id)
    db_session.refresh(execution)
    assert execution.store_id == shop_a.id


def test_unassigned_plan_can_specify_store_at_execution_time(db_session):
    prod = product(db_session, "14")
    shop = store(db_session, "临时选店")
    plan = make_plan(db_session, prod, 3)  # no selected_store
    execution = record_purchase_execution(db_session, plan.id, 3, store_id=shop.id)
    assert execution.store_id == shop.id


# ---------------- bulk ----------------


def test_bulk_creates_multiple_executions(db_session):
    prod_a = product(db_session, "15")
    prod_b = product(db_session, "16")
    plan_a = make_plan(db_session, prod_a, 3)
    plan_b = make_plan(db_session, prod_b, 2)
    created = record_purchase_executions_bulk(db_session, [
        ExecutionInput(plan_id=plan_a.id, quantity=3),
        ExecutionInput(plan_id=plan_b.id, quantity=1),
    ])
    assert len(created) == 2


def test_bulk_is_transactional(db_session):
    prod = product(db_session, "17")
    plan = make_plan(db_session, prod, 3)
    with pytest.raises(LookupError):
        record_purchase_executions_bulk(db_session, [
            ExecutionInput(plan_id=plan.id, quantity=1),
            ExecutionInput(plan_id=999999, quantity=1),
        ])
    count = db_session.query(ProcurementPurchaseExecution).filter_by(plan_id=plan.id).count()
    assert count == 0


def test_unselected_plan_not_affected_by_bulk(db_session):
    prod_a = product(db_session, "18")
    prod_b = product(db_session, "19")
    plan_a = make_plan(db_session, prod_a, 3)
    plan_b = make_plan(db_session, prod_b, 2)
    record_purchase_executions_bulk(db_session, [ExecutionInput(plan_id=plan_a.id, quantity=3)])
    assert purchased_quantity_for_plans(db_session, [plan_b.id])[plan_b.id] == 0


# ---------------- cancellation ----------------


def test_pending_execution_can_be_cancelled(db_session):
    prod = product(db_session, "20")
    plan = make_plan(db_session, prod, 3)
    execution = record_purchase_execution(db_session, plan.id, 3)
    cancelled = cancel_purchase_execution(db_session, execution.id)
    assert cancelled.status == "cancelled"


def test_remaining_restored_after_cancel(db_session):
    prod = product(db_session, "21")
    plan = make_plan(db_session, prod, 5)
    execution = record_purchase_execution(db_session, plan.id, 3)
    assert remaining_quantity_for_plans(db_session, [plan])[plan.id] == 2
    cancel_purchase_execution(db_session, execution.id)
    assert remaining_quantity_for_plans(db_session, [plan])[plan.id] == 5


def test_cancelled_execution_excluded_from_purchased_sum(db_session):
    prod = product(db_session, "22")
    plan = make_plan(db_session, prod, 5)
    execution = record_purchase_execution(db_session, plan.id, 3)
    cancel_purchase_execution(db_session, execution.id)
    assert purchased_quantity_for_plans(db_session, [plan.id])[plan.id] == 0


def test_reconciled_execution_cannot_be_cancelled(db_session):
    prod = product(db_session, "23")
    plan = make_plan(db_session, prod, 3)
    execution = record_purchase_execution(db_session, plan.id, 3)
    execution.status = "reconciled"
    db_session.commit()
    with pytest.raises(ValueError):
        cancel_purchase_execution(db_session, execution.id)


# ---------------- store purchase listing ----------------


def test_store_purchase_list_excludes_fully_purchased(db_session):
    shop = store(db_session, "清单店1")
    prod_a = product(db_session, "24")
    prod_b = product(db_session, "25")
    plan_a = make_plan(db_session, prod_a, 3, store_row=shop)
    plan_b = make_plan(db_session, prod_b, 2, store_row=shop)
    record_purchase_execution(db_session, plan_a.id, 3)
    listing = list_plans_for_store_purchase(db_session, shop.id)
    ids = {plan.id for plan in listing}
    assert plan_a.id not in ids
    assert plan_b.id in ids


def test_store_purchase_list_only_that_store(db_session):
    shop_a = store(db_session, "清单店A")
    shop_b = store(db_session, "清单店B")
    prod_a = product(db_session, "26")
    prod_b = product(db_session, "27")
    plan_a = make_plan(db_session, prod_a, 3, store_row=shop_a)
    plan_b = make_plan(db_session, prod_b, 2, store_row=shop_b)
    listing = list_plans_for_store_purchase(db_session, shop_a.id)
    ids = {plan.id for plan in listing}
    assert plan_a.id in ids
    assert plan_b.id not in ids


def test_unassigned_store_purchase_list(db_session):
    prod = product(db_session, "28")
    plan = make_plan(db_session, prod, 3)
    listing = list_plans_for_store_purchase(db_session, None)
    assert plan.id in {p.id for p in listing}


# ---------------- coverage-style entry stats ----------------


def test_store_purchase_entries_use_remaining_not_planned(db_session):
    shop = store(db_session, "入口统计店")
    prod_a = product(db_session, "29")
    prod_b = product(db_session, "30")
    plan_a = make_plan(db_session, prod_a, 5, store_row=shop)
    plan_b = make_plan(db_session, prod_b, 4, store_row=shop)
    record_purchase_execution(db_session, plan_a.id, 5)  # fully bought, should drop out
    plans = [plan_a, plan_b]
    groups = group_plans_by_selected_store(plans)
    summaries = build_plan_execution_summaries(db_session, plans)
    entries = build_store_purchase_entries(groups, summaries)
    entry = next(e for e in entries if e.store_id == shop.id)
    assert entry.remaining_kind_count == 1
    assert entry.remaining_quantity == 4


# ---------------- traceability ----------------


def test_execution_traceable_to_demand_source(db_session):
    prod = product(db_session, "31")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3, note="来源链路测试")
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=3)])
    execution = record_purchase_execution(db_session, plan.id, 3)
    db_session.refresh(plan)
    assert execution.plan_id == plan.id
    assert plan.sources[0].demand.note == "来源链路测试"


def test_build_plan_executions_batched(db_session):
    prod_a = product(db_session, "32")
    prod_b = product(db_session, "33")
    plan_a = make_plan(db_session, prod_a, 3)
    plan_b = make_plan(db_session, prod_b, 2)
    record_purchase_execution(db_session, plan_a.id, 1)
    grouped = build_plan_executions(db_session, [plan_a, plan_b])
    assert len(grouped[plan_a.id]) == 1
    assert grouped[plan_b.id] == []


# ---------------- performance ----------------


def test_execution_summary_lookup_does_not_scale_linearly(db_session):
    from contextlib import contextmanager
    from sqlalchemy import event

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

    plans_small = [make_plan(db_session, product(db_session, str(40 + i)), 3) for i in range(2)]
    for plan in plans_small:
        record_purchase_execution(db_session, plan.id, 1)
    plans_large = plans_small + [make_plan(db_session, product(db_session, str(50 + i)), 3) for i in range(18)]
    for plan in plans_large[2:]:
        record_purchase_execution(db_session, plan.id, 1)

    with count_queries(db_session) as small_counter:
        build_plan_execution_summaries(db_session, plans_small)
    with count_queries(db_session) as large_counter:
        build_plan_execution_summaries(db_session, plans_large)

    assert small_counter["n"] == large_counter["n"], (
        f"query count grew with plan count: {small_counter['n']} for 2 plans vs {large_counter['n']} for 20"
    )
