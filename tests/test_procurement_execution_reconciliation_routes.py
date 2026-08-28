from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    Product, ProcurementDemandPlan, ProcurementPurchaseExecution, PurchaseBatch, PurchaseBatchItem,
    Receipt, ReceiptBatch, ReceiptItem, Store,
)
from app.procurement_service import PlanSelectionInput, create_channel_shortage_demand, create_plans, record_purchase_execution, set_plan_selected_store


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"RTR-{suffix:0>4}", jan=f"0490900000{suffix:0>3}", name_cn=f"路由对账商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def make_plan_with_execution(db, prod: Product, shop: Store, quantity: int) -> ProcurementPurchaseExecution:
    create_channel_shortage_demand(db, product_id=prod.id, quantity=quantity)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(prod.id), planned_quantity=quantity)])
    set_plan_selected_store(db, plan.id, shop.id)
    return record_purchase_execution(db, plan.id, quantity)


def make_receipt(db, *, store_row: Store, items: list[tuple[Product, int, int]]) -> tuple[ReceiptBatch, Receipt]:
    initialize_default_locations(db)
    seq = db.scalar(select(func.count()).select_from(ReceiptBatch)) + 1
    batch = ReceiptBatch(batch_no=f"RTR-RB-{seq}", status="review", image_status="ready", gpt_status="json_imported")
    receipt = Receipt(
        batch=batch, raw_store_name=store_row.display_name, store_id=store_row.id,
        store_match_status="confirmed", purchased_at=NOW, confirmation_status="pending", review_status="pending",
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


def test_review_page_shows_execution_candidate(client):
    http, db, _tmp = client
    shop = store(db, "路由候选店")
    prod = product(db, "1")
    db.commit()
    execution = make_plan_with_execution(db, prod, shop, 3)
    batch, receipt = make_receipt(db, store_row=shop, items=[(prod, 3, 680)])

    response = http.get(f"/receipts/{batch.id}/review?receipt_id={receipt.id}")
    assert response.status_code == 200
    assert "采购执行匹配" in response.text
    assert "高可信" in response.text
    assert f'value="{execution.id}"' in response.text


def test_review_page_no_candidate_shows_hint(client):
    http, db, _tmp = client
    shop = store(db, "路由无候选店")
    prod = product(db, "2")
    db.commit()
    batch, receipt = make_receipt(db, store_row=shop, items=[(prod, 1, 200)])

    response = http.get(f"/receipts/{batch.id}/review?receipt_id={receipt.id}")
    assert response.status_code == 200
    assert "无待小票采购记录" in response.text


def test_confirm_with_selected_execution_creates_match_and_batch(client):
    http, db, _tmp = client
    shop = store(db, "路由确认店")
    prod = product(db, "3")
    db.commit()
    execution = make_plan_with_execution(db, prod, shop, 3)
    batch, receipt = make_receipt(db, store_row=shop, items=[(prod, 3, 680)])
    item = receipt.items[0]

    from app.location_service import get_default_physical_location
    initial_location = get_default_physical_location(db)
    response = http.post(f"/receipts/{batch.id}/review/confirm", data={
        "receipt_id": str(receipt.id),
        "initial_location_id": str(initial_location.id),
        f"execution_match_{item.id}": str(execution.id),
        f"execution_match_qty_{item.id}": "3",
    }, follow_redirects=False)
    assert response.status_code == 303

    db.refresh(execution)
    assert execution.status == "reconciled"
    purchase_batch = db.scalar(select(PurchaseBatch).where(PurchaseBatch.receipt_id == receipt.id))
    assert purchase_batch is not None
    assert purchase_batch.items[0].unit_price == 680


def test_confirm_without_execution_selection_still_confirms(client):
    http, db, _tmp = client
    shop = store(db, "路由不关联店")
    prod = product(db, "4")
    db.commit()
    execution = make_plan_with_execution(db, prod, shop, 3)
    batch, receipt = make_receipt(db, store_row=shop, items=[(prod, 3, 680)])

    from app.location_service import get_default_physical_location
    initial_location = get_default_physical_location(db)
    response = http.post(f"/receipts/{batch.id}/review/confirm", data={
        "receipt_id": str(receipt.id), "initial_location_id": str(initial_location.id),
    }, follow_redirects=False)
    assert response.status_code == 303

    assert db.scalar(select(PurchaseBatch).where(PurchaseBatch.receipt_id == receipt.id)) is not None
    db.refresh(execution)
    assert execution.status == "pending_receipt"  # untouched -- nothing was selected


def test_planned_view_shows_reconciliation_breakdown(client):
    http, db, _tmp = client
    shop = store(db, "路由已安排店")
    prod = product(db, "5")
    db.commit()
    execution = make_plan_with_execution(db, prod, shop, 5)
    batch, receipt = make_receipt(db, store_row=shop, items=[(prod, 3, 680)])
    item = receipt.items[0]
    from app.location_service import get_default_physical_location
    initial_location = get_default_physical_location(db)
    http.post(f"/receipts/{batch.id}/review/confirm", data={
        "receipt_id": str(receipt.id), "initial_location_id": str(initial_location.id),
        f"execution_match_{item.id}": str(execution.id), f"execution_match_qty_{item.id}": "3",
    })

    response = http.get("/procurement-demands?view=planned")
    assert "已对账" in response.text
    assert "待小票" in response.text
