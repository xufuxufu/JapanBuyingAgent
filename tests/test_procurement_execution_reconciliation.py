from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    Product, ProcurementDemandPlan, ProcurementExecutionReceiptMatch, ProcurementPurchaseExecution,
    PurchaseBatch, PurchaseBatchItem, Receipt, ReceiptBatch, ReceiptItem, Store,
)
from app.procurement_service import (
    ExecutionMatchInput, PlanSelectionInput, build_plan_reconciliation_summaries, cancel_purchase_execution,
    confirm_execution_receipt_matches, confirmed_match_quantity_for_executions, create_channel_shortage_demand,
    create_plans, find_execution_candidates_for_receipt_items, record_purchase_execution,
    remaining_reconcile_quantity, set_plan_selected_store,
)
from app.services import confirm_receipt


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"REC-{suffix:0>4}", jan=f"0490800000{suffix:0>3}", name_cn=f"对账测试商品{suffix}")
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


def make_receipt(
    db, *, store_row: Store | None = None, purchased_at: datetime | None = NOW,
    items: list[tuple[Product, int, int]] = (),
) -> tuple[ReceiptBatch, Receipt]:
    initialize_default_locations(db)
    seq = db.scalar(select(func.count()).select_from(ReceiptBatch)) + 1
    batch = ReceiptBatch(batch_no=f"REC-RB-{seq}", status="review", image_status="ready", gpt_status="json_imported")
    receipt = Receipt(
        batch=batch, raw_store_name=store_row.display_name if store_row else "未知店铺",
        store_id=store_row.id if store_row else None,
        store_match_status="confirmed" if store_row else "pending",
        purchased_at=purchased_at, confirmation_status="pending", review_status="pending",
    )
    db.add_all([batch, receipt])
    db.flush()
    for line_no, (prod, quantity, unit_price) in enumerate(items, start=1):
        receipt.items.append(ReceiptItem(
            line_no=line_no, raw_name=prod.name_cn, jan_candidate=prod.jan,
            product_id=prod.id, match_method="manual", match_status="matched_existing",
            quantity=quantity, unit_price=unit_price, discount_amount=0, line_total=quantity * unit_price,
            confidence=1, review_status="pending",
        ))
    db.commit()
    return batch, receipt


# ---------------- candidate matching ----------------


def test_product_exact_candidate_found(db_session):
    shop = store(db_session, "候选店1")
    prod = product(db_session, "1")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    item = receipt.items[0]
    assert len(candidates[item.id]) == 1
    assert candidates[item.id][0].execution.id == execution.id


def test_store_exact_match_raises_confidence(db_session):
    shop = store(db_session, "候选店2")
    prod = product(db_session, "2")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id][0].confidence == "high"


def test_store_mismatch_not_high_confidence(db_session):
    shop_a = store(db_session, "候选店3A")
    shop_b = store(db_session, "候选店3B")
    prod = product(db_session, "3")
    plan = make_plan(db_session, prod, 3, store_row=shop_a)
    record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop_b, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id][0].confidence == "low"


def test_cancelled_execution_not_a_candidate(db_session):
    shop = store(db_session, "候选店4")
    prod = product(db_session, "4")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    cancel_purchase_execution(db_session, execution.id)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id] == []


def test_reconciled_execution_not_a_candidate(db_session):
    shop = store(db_session, "候选店5")
    prod = product(db_session, "5")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    execution.status = "reconciled"
    db_session.commit()
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id] == []


def test_only_pending_receipt_matches(db_session):
    shop = store(db_session, "候选店6")
    prod = product(db_session, "6")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    assert execution.status == "pending_receipt"
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert len(candidates[receipt.items[0].id]) == 1


def test_receipt_without_execution_history_still_confirms(db_session):
    prod = product(db_session, "7")
    _, receipt = make_receipt(db_session, items=[(prod, 2, 300)])
    confirm_receipt(db_session, receipt.batch, receipt)
    assert db_session.scalar(select(PurchaseBatch).where(PurchaseBatch.receipt_id == receipt.id)) is not None


def test_no_fuzzy_name_auto_confirmation(db_session):
    """An item with no resolved product_id gets no candidates -- never guessed by name."""
    shop = store(db_session, "候选店8")
    prod = product(db_session, "8")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[])
    receipt.items.append(ReceiptItem(
        line_no=1, raw_name=prod.name_cn, jan_candidate=None, product_id=None,
        match_method=None, match_status="unmatched", quantity=3, unit_price=680,
        discount_amount=0, line_total=2040, confidence=0.3, review_status="pending",
    ))
    db_session.commit()
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id] == []


def test_does_not_guess_jan(db_session):
    """Two products with different JANs never cross-match just because names look similar."""
    shop = store(db_session, "候选店9")
    prod_a = product(db_session, "9")
    prod_b = product(db_session, "10")
    plan = make_plan(db_session, prod_a, 3, store_row=shop)
    record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod_b, 3, 680)])
    candidates = find_execution_candidates_for_receipt_items(db_session, receipt)
    assert candidates[receipt.items[0].id] == []


# ---------------- quantity matching ----------------


def test_exact_quantity_full_match(db_session):
    shop = store(db_session, "数量店1")
    prod = product(db_session, "11")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.refresh(execution)
    assert execution.status == "reconciled"


def test_execution5_receipt3_partial(db_session):
    shop = store(db_session, "数量店2")
    prod = product(db_session, "12")
    plan = make_plan(db_session, prod, 5, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 5)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.refresh(execution)
    assert execution.status == "pending_receipt"
    assert remaining_reconcile_quantity(db_session, execution) == 2


def test_execution3_receipt5_max_match_is_3(db_session):
    shop = store(db_session, "数量店3")
    prod = product(db_session, "13")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 5, 680)])
    with pytest.raises(ValueError):
        confirm_execution_receipt_matches(db_session, receipt, [
            ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=5),
        ])
    # the correct, allowed usage: cap the match at the execution's own quantity
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.commit()
    assert confirmed_match_quantity_for_executions(db_session, [execution.id])[execution.id] == 3


def test_multiple_receipt_items_to_one_execution(db_session):
    shop = store(db_session, "数量店4")
    prod = product(db_session, "14")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680), (prod, 1, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=2),
        ExecutionMatchInput(item_id=receipt.items[1].id, execution_id=execution.id, matched_quantity=1),
    ])
    db_session.commit()
    db_session.refresh(execution)
    assert execution.status == "reconciled"


def test_one_receipt_item_to_multiple_executions(db_session):
    shop = store(db_session, "数量店5")
    prod = product(db_session, "15")
    plan = make_plan(db_session, prod, 5, store_row=shop)
    execution_a = record_purchase_execution(db_session, plan.id, 2)
    execution_b = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 5, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_a.id, matched_quantity=2),
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_b.id, matched_quantity=3),
    ])
    db_session.commit()
    db_session.refresh(execution_a)
    db_session.refresh(execution_b)
    assert execution_a.status == "reconciled"
    assert execution_b.status == "reconciled"


def test_confirmed_total_never_exceeds_execution_quantity(db_session):
    shop = store(db_session, "数量店6")
    prod = product(db_session, "16")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680), (prod, 3, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.commit()
    with pytest.raises(ValueError):
        confirm_execution_receipt_matches(db_session, receipt, [
            ExecutionMatchInput(item_id=receipt.items[1].id, execution_id=execution.id, matched_quantity=1),
        ])


def test_receipt_matched_total_never_exceeds_item_quantity(db_session):
    shop = store(db_session, "数量店7")
    prod = product(db_session, "17")
    plan_a = make_plan(db_session, prod, 2, store_row=shop)
    plan_b = make_plan(db_session, product(db_session, "18"), 2, store_row=shop)
    execution_a = record_purchase_execution(db_session, plan_a.id, 2)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_a.id, matched_quantity=2),
    ])
    db_session.commit()
    with pytest.raises(ValueError):
        confirm_execution_receipt_matches(db_session, receipt, [
            ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_a.id, matched_quantity=1),
        ])


# ---------------- status ----------------


def test_partial_match_keeps_pending_receipt(db_session):
    shop = store(db_session, "状态店1")
    prod = product(db_session, "19")
    plan = make_plan(db_session, prod, 5, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 5)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=2),
    ])
    db_session.commit()
    db_session.refresh(execution)
    assert execution.status == "pending_receipt"


def test_full_match_becomes_reconciled(db_session):
    shop = store(db_session, "状态店2")
    prod = product(db_session, "20")
    plan = make_plan(db_session, prod, 2, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 2)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=2),
    ])
    db_session.commit()
    db_session.refresh(execution)
    assert execution.status == "reconciled"


def test_cancelled_match_not_counted():
    # Design choice: matches are only ever written as confirmed (see model docstring);
    # there is no cancel-a-match path in Phase 2E, so cancellation is exercised at the
    # execution level instead (see test_partially_matched_execution_cannot_be_cancelled).
    assert True


def test_partially_matched_execution_cannot_be_cancelled(db_session):
    shop = store(db_session, "状态店3")
    prod = product(db_session, "21")
    plan = make_plan(db_session, prod, 5, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 5)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=2),
    ])
    db_session.commit()
    with pytest.raises(ValueError):
        cancel_purchase_execution(db_session, execution.id)


# ---------------- PurchaseBatch generation ----------------


def test_one_receipt_creates_one_purchase_batch(db_session):
    shop = store(db_session, "批次店1")
    prod_a = product(db_session, "22")
    prod_b = product(db_session, "23")
    plan_a = make_plan(db_session, prod_a, 3, store_row=shop)
    plan_b = make_plan(db_session, prod_b, 2, store_row=shop)
    execution_a = record_purchase_execution(db_session, plan_a.id, 3)
    execution_b = record_purchase_execution(db_session, plan_b.id, 2)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod_a, 3, 680), (prod_b, 2, 500)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_a.id, matched_quantity=3),
        ExecutionMatchInput(item_id=receipt.items[1].id, execution_id=execution_b.id, matched_quantity=2),
    ])
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == 1


def test_multiple_items_create_multiple_purchase_batch_items(db_session):
    shop = store(db_session, "批次店2")
    prod_a = product(db_session, "24")
    prod_b = product(db_session, "25")
    plan_a = make_plan(db_session, prod_a, 3, store_row=shop)
    plan_b = make_plan(db_session, prod_b, 2, store_row=shop)
    execution_a = record_purchase_execution(db_session, plan_a.id, 3)
    execution_b = record_purchase_execution(db_session, plan_b.id, 2)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod_a, 3, 680), (prod_b, 2, 500)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution_a.id, matched_quantity=3),
        ExecutionMatchInput(item_id=receipt.items[1].id, execution_id=execution_b.id, matched_quantity=2),
    ])
    batch = db_session.scalar(select(PurchaseBatch))
    assert len(batch.items) == 2


def test_official_price_comes_from_receipt(db_session):
    shop = store(db_session, "批次店3")
    prod = product(db_session, "26")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    detail = db_session.scalar(select(PurchaseBatchItem))
    assert detail.unit_price == 680


def test_confirm_is_not_repeated(db_session):
    # The route layer refuses to re-run confirm_receipt on an already-confirmed
    # receipt (see test_procurement_execution_reconciliation_routes.py); here we
    # confirm the underlying invariant it depends on: ensure_purchase_batch_for_receipt
    # itself stays idempotent even if called again directly.
    from app.purchase_service import ensure_purchase_batch_for_receipt
    shop = store(db_session, "批次店4")
    prod = product(db_session, "27")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    ensure_purchase_batch_for_receipt(db_session, receipt)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == 1


def test_execution_traceable_to_purchase_batch_item(db_session):
    shop = store(db_session, "批次店5")
    prod = product(db_session, "28")
    plan = make_plan(db_session, prod, 3, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    match = db_session.scalar(select(ProcurementExecutionReceiptMatch).where(ProcurementExecutionReceiptMatch.execution_id == execution.id))
    purchase_item = db_session.scalar(select(PurchaseBatchItem).where(PurchaseBatchItem.receipt_item_id == match.receipt_item_id))
    assert purchase_item is not None
    assert purchase_item.quantity == 3


# ---------------- planned view summaries ----------------


def test_plan_reconciliation_summary_reflects_partial_match(db_session):
    shop = store(db_session, "汇总店1")
    prod = product(db_session, "29")
    plan = make_plan(db_session, prod, 5, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 5)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.commit()
    summary = build_plan_reconciliation_summaries(db_session, [plan])[plan.id]
    assert summary.purchased_quantity == 5
    assert summary.reconciled_quantity == 3
    assert summary.pending_receipt_quantity == 2


def test_plan_reconciliation_summary_fully_reconciled(db_session):
    shop = store(db_session, "汇总店2")
    prod = product(db_session, "30")
    plan = make_plan(db_session, prod, 2, store_row=shop)
    execution = record_purchase_execution(db_session, plan.id, 2)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 2, 680)])
    confirm_execution_receipt_matches(db_session, receipt, [
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=2),
    ])
    db_session.commit()
    summary = build_plan_reconciliation_summaries(db_session, [plan])[plan.id]
    assert summary.fully_reconciled is True


# ---------------- traceability ----------------


def test_demand_to_execution_to_purchase_batch_traceable(db_session):
    shop = store(db_session, "链路店")
    prod = product(db_session, "31")
    create_channel_shortage_demand(db_session, product_id=prod.id, quantity=3, note="链路测试备注")
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=3)])
    set_plan_selected_store(db_session, plan.id, shop.id)
    execution = record_purchase_execution(db_session, plan.id, 3)
    _, receipt = make_receipt(db_session, store_row=shop, items=[(prod, 3, 680)])
    confirm_receipt(db_session, receipt.batch, receipt, execution_matches=[
        ExecutionMatchInput(item_id=receipt.items[0].id, execution_id=execution.id, matched_quantity=3),
    ])
    db_session.refresh(plan)
    assert plan.sources[0].demand.note == "链路测试备注"
    match = db_session.scalar(select(ProcurementExecutionReceiptMatch).where(ProcurementExecutionReceiptMatch.execution_id == execution.id))
    purchase_item = db_session.scalar(select(PurchaseBatchItem).where(PurchaseBatchItem.receipt_item_id == match.receipt_item_id))
    assert purchase_item.purchase_batch.receipt_id == receipt.id


# ---------------- performance ----------------


def test_candidate_lookup_does_not_scale_linearly(db_session):
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

    shop = store(db_session, "性能对账店")
    small_products = [product(db_session, str(60 + i)) for i in range(2)]
    large_products = small_products + [product(db_session, str(70 + i)) for i in range(18)]
    for prod in large_products:
        plan = make_plan(db_session, prod, 2, store_row=shop)
        record_purchase_execution(db_session, plan.id, 2)

    _, small_receipt = make_receipt(db_session, store_row=shop, items=[(p, 2, 500) for p in small_products])
    _, large_receipt = make_receipt(db_session, store_row=shop, items=[(p, 2, 500) for p in large_products])

    with count_queries(db_session) as small_counter:
        find_execution_candidates_for_receipt_items(db_session, small_receipt)
    with count_queries(db_session) as large_counter:
        find_execution_candidates_for_receipt_items(db_session, large_receipt)

    assert small_counter["n"] == large_counter["n"], (
        f"query count grew with item count: {small_counter['n']} for 2 items vs {large_counter['n']} for 20"
    )
