from __future__ import annotations

import json

from app.models import Product, ProcurementDemandPlan, ProcurementPurchaseExecution, Store


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"EXR-{suffix:0>4}", jan=f"0490700000{suffix:0>3}", name_cn=f"执行路由商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def make_plan(test_client, db, prod: Product, quantity: int, shop: Store | None = None) -> int:
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": str(quantity)})
    selections = json.dumps([{"kind": "product", "key": str(prod.id), "planned_quantity": quantity}])
    test_client.post("/procurement-demands/plan", data={"selections_json": selections})
    plan = db.query(ProcurementDemandPlan).filter_by(product_id=prod.id).one()
    if shop is not None:
        test_client.post(f"/procurement-demands/plans/{plan.id}/store", data={"store_id": str(shop.id)})
    return plan.id


def test_store_purchase_page_shows_remaining_only(client):
    test_client, db, _tmp = client
    shop = store(db, "路由清单店")
    db.commit()
    prod_a = product(db, "1")
    prod_b = product(db, "2")
    db.commit()
    plan_a = make_plan(test_client, db, prod_a, 3, shop)
    plan_b = make_plan(test_client, db, prod_b, 2, shop)

    selections = json.dumps([{"plan_id": plan_a, "quantity": 3}])
    test_client.post("/procurement-demands/purchase", data={"store_id": str(shop.id), "executions_json": selections})

    response = test_client.get(f"/procurement-demands/purchase?store_id={shop.id}")
    assert response.status_code == 200
    assert "执行路由商品1" not in response.text
    assert "执行路由商品2" in response.text


def test_default_actual_quantity_is_remaining(client):
    test_client, db, _tmp = client
    shop = store(db, "路由默认值店")
    db.commit()
    prod = product(db, "3")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 5, shop)
    test_client.post("/procurement-demands/purchase", data={
        "store_id": str(shop.id), "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 2}]),
    })

    response = test_client.get(f"/procurement-demands/purchase?store_id={shop.id}")
    assert 'value="3"' in response.text  # 5 planned - 2 already bought = 3 remaining


def test_submit_creates_executions_and_updates_status(client):
    test_client, db, _tmp = client
    shop = store(db, "路由提交店")
    db.commit()
    prod_a = product(db, "4")
    prod_b = product(db, "5")
    db.commit()
    plan_a = make_plan(test_client, db, prod_a, 3, shop)
    plan_b = make_plan(test_client, db, prod_b, 2, shop)

    response = test_client.post(
        "/procurement-demands/purchase",
        data={"store_id": str(shop.id), "executions_json": json.dumps([
            {"plan_id": plan_a, "quantity": 3}, {"plan_id": plan_b, "quantity": 1},
        ])},
        follow_redirects=False,
    )
    assert response.status_code == 303

    executions = db.query(ProcurementPurchaseExecution).all()
    assert len(executions) == 2
    assert all(e.status == "pending_receipt" for e in executions)


def test_page_updates_after_submission(client):
    test_client, db, _tmp = client
    shop = store(db, "路由刷新店")
    db.commit()
    prod = product(db, "6")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 3, shop)
    test_client.post("/procurement-demands/purchase", data={
        "store_id": str(shop.id), "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 3}]),
    })

    response = test_client.get(f"/procurement-demands/purchase?store_id={shop.id}")
    assert "执行路由商品6" not in response.text  # fully bought, drops off the checklist


def test_fully_purchased_moves_from_planned_to_purchased_view(client):
    # Phase 8 tab redesign: 待采购商品 (view=planned) only lists plans that
    # still need buying; a fully-bought plan moves to 已采购 (view=purchased)
    # instead of lingering in the "still need to buy" queue forever.
    test_client, db, _tmp = client
    shop = store(db, "路由已安排店")
    db.commit()
    prod = product(db, "7")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 2, shop)
    test_client.post("/procurement-demands/purchase", data={
        "store_id": str(shop.id), "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 2}]),
    })

    planned_response = test_client.get("/procurement-demands?view=planned")
    assert "执行路由商品7" not in planned_response.text

    purchased_response = test_client.get("/procurement-demands?view=purchased")
    assert "计划已买齐" in purchased_response.text
    assert "已买记录" in purchased_response.text
    assert "已买待小票" in purchased_response.text


def test_unassigned_purchase_requires_store_selection(client):
    test_client, db, _tmp = client
    shop = store(db, "路由未确定店")
    db.commit()
    prod = product(db, "8")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 3)  # no store

    response = test_client.post(
        "/procurement-demands/purchase",
        data={"store_id": "", "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 3, "store_id": shop.id}])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    execution = db.query(ProcurementPurchaseExecution).filter_by(plan_id=plan_id).one()
    assert execution.store_id == shop.id


def test_overbuy_allowed_via_route(client):
    test_client, db, _tmp = client
    shop = store(db, "路由超买店")
    db.commit()
    prod = product(db, "9")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 5, shop)

    response = test_client.post(
        "/procurement-demands/purchase",
        data={"store_id": str(shop.id), "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 7}])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    execution = db.query(ProcurementPurchaseExecution).filter_by(plan_id=plan_id).one()
    assert execution.quantity == 7


def test_cancel_execution_restores_remaining(client):
    test_client, db, _tmp = client
    shop = store(db, "路由撤销店")
    db.commit()
    prod = product(db, "10")
    db.commit()
    plan_id = make_plan(test_client, db, prod, 5, shop)
    test_client.post("/procurement-demands/purchase", data={
        "store_id": str(shop.id), "executions_json": json.dumps([{"plan_id": plan_id, "quantity": 3}]),
    })
    execution = db.query(ProcurementPurchaseExecution).filter_by(plan_id=plan_id).one()

    response = test_client.post(
        f"/procurement-demands/executions/{execution.id}/cancel", data={"group_by": "product"}, follow_redirects=False,
    )
    assert response.status_code == 303
    db.refresh(execution)
    assert execution.status == "cancelled"

    purchase_page = test_client.get(f"/procurement-demands/purchase?store_id={shop.id}")
    assert 'value="5"' in purchase_page.text  # remaining restored to full plan quantity


def test_store_group_view_shows_start_purchase_entry(client):
    test_client, db, _tmp = client
    shop = store(db, "路由入口店")
    db.commit()
    prod = product(db, "11")
    db.commit()
    make_plan(test_client, db, prod, 3, shop)

    response = test_client.get("/procurement-demands?view=planned&group_by=store")
    assert "开始采购" in response.text
