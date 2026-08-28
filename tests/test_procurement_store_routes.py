from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import Location, Product, ProcurementDemandPlan, PurchaseBatch, PurchaseBatchItem, Receipt, ReceiptBatch, ReceiptItem, Store


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"SRR-{suffix:0>4}", jan=f"0490500000{suffix:0>3}", name_cn=f"来源路由商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, name_ja=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def add_purchase(db, item: Product, shop: Store, *, price: int = 500, quantity: int = 1, days_ago: int = 1) -> PurchaseBatchItem:
    rows = {row.display_name: row for row in initialize_default_locations(db)}
    local, qinsi = rows["日本家里库存"], rows["新日本仓库"]
    seq = db.scalar(select(func.count()).select_from(ReceiptBatch)) + 1
    purchased_at = NOW - timedelta(days=days_ago)
    source = ReceiptBatch(batch_no=f"SRR-RB-{seq}", status="confirmed", image_status="ready", gpt_status="reviewed")
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
        batch_no=f"SRR-PB-{seq}", receipt_id=receipt.id, gpt_batch_id=source.id,
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


def make_plan(test_client, db, prod: Product, quantity: int = 1) -> int:
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": str(quantity)})
    import json
    selections = json.dumps([{"kind": "product", "key": str(prod.id), "planned_quantity": quantity}])
    test_client.post("/procurement-demands/plan", data={"selections_json": selections})
    plan = db.query(ProcurementDemandPlan).filter_by(product_id=prod.id).one()
    return plan.id


def test_planned_view_shows_history_and_recommendation(client):
    test_client, db, _tmp = client
    prod = product(db, "1")
    matsumoto = store(db, "松本清")
    don = store(db, "唐吉诃德")
    db.commit()
    add_purchase(db, prod, matsumoto, price=680, days_ago=5)
    add_purchase(db, prod, matsumoto, price=650, days_ago=20)
    add_purchase(db, prod, matsumoto, price=700, days_ago=40)
    add_purchase(db, prod, don, price=598, days_ago=10)
    make_plan(test_client, db, prod, quantity=3)

    response = test_client.get("/procurement-demands?view=planned")
    assert response.status_code == 200
    assert "历史采购来源" in response.text
    assert "推荐：" in response.text
    assert "历史价格" in response.text or "历史最低" in response.text
    assert "当前价格" not in response.text


def test_no_history_product_shows_no_history_message(client):
    test_client, db, _tmp = client
    prod = product(db, "2")
    db.commit()
    make_plan(test_client, db, prod, quantity=1)

    response = test_client.get("/procurement-demands?view=planned")
    assert "无历史采购来源" in response.text


def test_select_store_for_plan_via_route(client):
    test_client, db, _tmp = client
    prod = product(db, "3")
    shop = store(db, "路由选择店")
    db.commit()
    plan_id = make_plan(test_client, db, prod)

    response = test_client.post(
        f"/procurement-demands/plans/{plan_id}/store", data={"store_id": str(shop.id), "group_by": "product"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    plan = db.get(ProcurementDemandPlan, plan_id)
    assert plan.selected_store_id == shop.id


def test_clear_store_selection_via_route(client):
    test_client, db, _tmp = client
    prod = product(db, "4")
    shop = store(db, "路由清空店")
    db.commit()
    plan_id = make_plan(test_client, db, prod)
    test_client.post(f"/procurement-demands/plans/{plan_id}/store", data={"store_id": str(shop.id)})
    test_client.post(f"/procurement-demands/plans/{plan_id}/store", data={"store_id": ""})
    plan = db.get(ProcurementDemandPlan, plan_id)
    assert plan.selected_store_id is None


def test_bulk_store_route_applies_to_selected_plans_only(client):
    test_client, db, _tmp = client
    prod_a = product(db, "5")
    prod_b = product(db, "6")
    shop = store(db, "路由批量店")
    db.commit()
    plan_a = make_plan(test_client, db, prod_a)
    plan_b = make_plan(test_client, db, prod_b)

    response = test_client.post(
        "/procurement-demands/plans/store-bulk",
        data={"store_id": str(shop.id), "group_by": "product", "plan_ids": [str(plan_a)]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get(ProcurementDemandPlan, plan_a).selected_store_id == shop.id
    assert db.get(ProcurementDemandPlan, plan_b).selected_store_id is None


def test_switch_between_product_and_store_grouping(client):
    test_client, db, _tmp = client
    prod_a = product(db, "7")
    prod_b = product(db, "8")
    shop = store(db, "路由分组店")
    db.commit()
    plan_a = make_plan(test_client, db, prod_a, quantity=3)
    plan_b = make_plan(test_client, db, prod_b, quantity=2)
    test_client.post(f"/procurement-demands/plans/{plan_a}/store", data={"store_id": str(shop.id)})

    product_view = test_client.get("/procurement-demands?view=planned&group_by=product")
    assert product_view.status_code == 200
    assert "按商品" in product_view.text and "按店铺" in product_view.text

    store_view = test_client.get("/procurement-demands?view=planned&group_by=store")
    assert store_view.status_code == 200
    assert "来源路由商品7" in store_view.text
    assert "未确定来源" in store_view.text


def test_unassigned_group_appears_for_plan_without_store(client):
    test_client, db, _tmp = client
    prod = product(db, "9")
    db.commit()
    make_plan(test_client, db, prod, quantity=1)

    response = test_client.get("/procurement-demands?view=planned&group_by=store")
    assert "未确定来源" in response.text
    assert "来源路由商品9" in response.text


def test_coverage_board_shown_when_history_exists(client):
    test_client, db, _tmp = client
    prod = product(db, "10")
    shop = store(db, "覆盖路由店")
    db.commit()
    add_purchase(db, prod, shop)
    make_plan(test_client, db, prod, quantity=1)

    response = test_client.get("/procurement-demands?view=planned")
    assert "历史可覆盖商品" in response.text


def test_changing_store_does_not_change_planned_quantity_or_sources(client):
    test_client, db, _tmp = client
    prod = product(db, "11")
    shop_a = store(db, "先选店")
    shop_b = store(db, "后选店")
    db.commit()
    plan_id = make_plan(test_client, db, prod, quantity=6)
    test_client.post(f"/procurement-demands/plans/{plan_id}/store", data={"store_id": str(shop_a.id)})
    test_client.post(f"/procurement-demands/plans/{plan_id}/store", data={"store_id": str(shop_b.id)})

    plan = db.get(ProcurementDemandPlan, plan_id)
    assert plan.planned_quantity == 6
    assert plan.selected_store_id == shop_b.id
    assert len(plan.sources) == 1
