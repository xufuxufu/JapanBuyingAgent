from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from app.models import Location, Product, ProcurementDemand, ProcurementDemandPlan, QinsiInventorySnapshot, QinsiInventorySnapshotLine
from app.sales_order_service import SalesOrderItemInput, create_customer, create_sales_order, ensure_default_salesperson


def product(db, suffix: str) -> Product:
    item = Product(internal_sku=f"PDR-{suffix:0>4}", jan=f"0490200000{suffix:0>3}", name_cn=f"路由测试商品{suffix}")
    db.add(item)
    db.flush()
    return item


def china_warehouse(db) -> Location:
    loc = Location(internal_code="QW-2025-QIANYU", display_name="2025千羽", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
    db.add(loc)
    db.flush()
    return loc


def add_snapshot_line(db, product_id: int, warehouse_id: int, quantity: int) -> None:
    snap = QinsiInventorySnapshot(
        batch_no="QS-ROUTE-TEST", original_filename="test.xlsx", file_hash="route-test-hash",
        file_content=b"x", data_at=datetime.now(timezone.utc), status="completed",
    )
    db.add(snap)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snap.id, original_row_no=1, raw_summary_json=json.dumps({}),
        product_id=product_id, warehouse_id=warehouse_id, quantity=quantity, matching_status="matched",
        warehouse_status="matched",
    ))
    db.commit()


def test_procurement_demands_page_accessible(client):
    test_client, db, _tmp = client
    response = test_client.get("/procurement-demands")
    assert response.status_code == 200
    assert "采购需求中心" in response.text


def test_report_shortage_page_accessible_and_submittable(client):
    test_client, db, _tmp = client
    prod = product(db, "1")
    db.commit()
    get_response = test_client.get("/procurement-demands/report-shortage")
    assert get_response.status_code == 200

    post_response = test_client.post(
        "/procurement-demands/report-shortage",
        data={"product_id": str(prod.id), "quantity": "3", "note": "小红书卖完了"},
        follow_redirects=False,
    )
    assert post_response.status_code == 303
    demand = db.query(ProcurementDemand).filter_by(product_id=prod.id).one()
    assert demand.source_person == "丈母娘"
    assert demand.demand_type == "channel_shortage"
    assert demand.requested_quantity == 3


def test_investigation_submit_does_not_create_sales_order(client):
    test_client, db, _tmp = client
    response = test_client.post(
        "/procurement-demands/investigations",
        data={"manual_name": "看看有没有粉色大号", "note": "拍照片"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "investigation_ok=1" in response.headers["location"]
    demand = db.query(ProcurementDemand).filter_by(demand_type="investigation").one()
    assert demand.source_person == "秀"
    assert demand.product_name_snapshot == "看看有没有粉色大号"
    from app.models import SalesOrder
    assert db.query(SalesOrder).count() == 0


def test_aggregated_list_shows_one_card_for_two_sources(client):
    test_client, db, _tmp = client
    prod = product(db, "2")
    db.commit()
    salesperson = ensure_default_salesperson(db)
    buyer = create_customer(db, name="聚合测试客户", phone="13911112222")
    create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=prod.id, manual_name=None, jan=None, quantity=2, unit_sale_price=Decimal("300"))],
    )
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "3"})

    response = test_client.get("/procurement-demands?view=open")
    assert response.status_code == 200
    assert response.text.count(f'data-key="{prod.id}"') == 1
    assert "明确需求：<strong>5</strong>" in response.text


def test_checkbox_batch_plan_then_appears_in_planned_view(client):
    test_client, db, _tmp = client
    prod_a = product(db, "3")
    prod_b = product(db, "4")
    db.commit()
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod_a.id), "quantity": "5"})
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod_b.id), "quantity": "3"})

    import json
    selections = json.dumps([
        {"kind": "product", "key": str(prod_a.id), "planned_quantity": 6},
        {"kind": "product", "key": str(prod_b.id), "planned_quantity": 3},
    ])
    response = test_client.post("/procurement-demands/plan", data={"selections_json": selections}, follow_redirects=False)
    assert response.status_code == 303
    assert "view=planned" in response.headers["location"]

    plans = db.query(ProcurementDemandPlan).all()
    assert len(plans) == 2
    quantities = {p.product_id: p.planned_quantity for p in plans}
    assert quantities[prod_a.id] == 6
    assert quantities[prod_b.id] == 3

    open_response = test_client.get("/procurement-demands?view=open")
    assert f'data-key="{prod_a.id}"' not in open_response.text
    assert f'data-key="{prod_b.id}"' not in open_response.text

    planned_response = test_client.get("/procurement-demands?view=planned")
    assert "已安排" in planned_response.text
    assert "路由测试商品3" in planned_response.text
    assert "路由测试商品4" in planned_response.text


def test_detail_page_shows_source_breakdown(client):
    test_client, db, _tmp = client
    prod = product(db, "5")
    db.commit()
    salesperson = ensure_default_salesperson(db)
    buyer = create_customer(db, name="详情测试客户", phone="13922223333")
    create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=prod.id, manual_name=None, jan=None, quantity=2, unit_sale_price=Decimal("100"))],
    )
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "3", "note": "缺货备注"})

    response = test_client.get(f"/procurement-demands/groups/product/{prod.id}")
    assert response.status_code == 200
    assert "秀" in response.text
    assert "丈母娘" in response.text
    assert "缺货备注" in response.text


def test_investigation_view_lists_open_and_close_action(client):
    test_client, db, _tmp = client
    test_client.post("/procurement-demands/investigations", data={"manual_name": "调查任务A"})
    demand = db.query(ProcurementDemand).filter_by(demand_type="investigation").one()

    view_response = test_client.get("/procurement-demands?view=investigation")
    assert "调查任务A" in view_response.text

    close_response = test_client.post(
        f"/procurement-demands/{demand.id}/close", data={"note": "已找到"}, follow_redirects=False,
    )
    assert close_response.status_code == 303
    db.refresh(demand)
    assert demand.status == "closed"

    view_after = test_client.get("/procurement-demands?view=investigation")
    assert "调查任务A" not in view_after.text


def test_open_card_shows_china_japan_and_shortage(client):
    test_client, db, _tmp = client
    prod = product(db, "10")
    china = china_warehouse(db)
    db.commit()
    add_snapshot_line(db, prod.id, china.id, 2)
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "5"})

    response = test_client.get("/procurement-demands?view=open")
    assert response.status_code == 200
    assert "中国参考" in response.text
    assert "日本参考" in response.text
    assert "国内明确缺口" in response.text
    assert "<strong>3</strong>" in response.text  # 5 - 2 = 3
    assert 'value="3"' in response.text  # default planned quantity follows the shortage


def test_open_card_shows_unknown_when_no_snapshot(client):
    test_client, db, _tmp = client
    prod = product(db, "11")
    db.commit()
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "5"})

    response = test_client.get("/procurement-demands?view=open")
    assert "库存未知" in response.text
    assert 'value="5"' in response.text  # falls back to confirmed demand total


def test_planned_view_shows_inventory_without_changing_planned_quantity(client):
    test_client, db, _tmp = client
    prod = product(db, "12")
    china = china_warehouse(db)
    db.commit()
    add_snapshot_line(db, prod.id, china.id, 1)
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "5"})
    selections = json.dumps([{"kind": "product", "key": str(prod.id), "planned_quantity": 4}])
    test_client.post("/procurement-demands/plan", data={"selections_json": selections})

    response = test_client.get("/procurement-demands?view=planned")
    assert "计划采购数量：<strong>4</strong>" in response.text
    assert "中国参考" in response.text
    assert "不会因新快照自动变化" in response.text

    plan = db.query(ProcurementDemandPlan).filter_by(product_id=prod.id).one()
    assert plan.planned_quantity == 4


def test_detail_page_shows_inventory_panel(client):
    test_client, db, _tmp = client
    prod = product(db, "13")
    china = china_warehouse(db)
    db.commit()
    add_snapshot_line(db, prod.id, china.id, 0)
    test_client.post("/procurement-demands/report-shortage", data={"product_id": str(prod.id), "quantity": "2"})

    response = test_client.get(f"/procurement-demands/groups/product/{prod.id}")
    assert response.status_code == 200
    assert "参考库存" in response.text
    assert "中国参考：<strong>0</strong>" in response.text
    assert "国内明确缺口" in response.text
