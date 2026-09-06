from __future__ import annotations

import io
from decimal import Decimal

import pytest
from PIL import Image

import app.main as main_module
from app.models import SalesShipment
from app.sales_order_service import (
    SalesOrderItemInput, add_shipping_label, create_customer, create_sales_order, create_shipment,
    ensure_default_salesperson, mark_shipment_shipped, update_sales_order_status,
)


@pytest.fixture(autouse=True)
def _isolated_shipping_label_storage(tmp_path, monkeypatch):
    import app.sales_order_shipping as shipping_module
    monkeypatch.setattr(shipping_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        shipping_module, "SALES_ORDER_SHIPPING_LABEL_DIR", tmp_path / "data" / "sales-orders" / "shipping-labels",
    )
    monkeypatch.setattr(
        shipping_module, "SALES_ORDER_ITEM_IMAGE_DIR", tmp_path / "data" / "sales-orders" / "item-images",
    )


def _label_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 90)).save(buffer, "JPEG")
    return buffer.getvalue()


def shipped_shipment(db, *, tracking_no: str | None = "70000000000001") -> tuple[int, int]:
    salesperson = ensure_default_salesperson(db)
    buyer = create_customer(db, name="路由测试客户", phone="13800000001", wechat_name="wx_route")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="路由测试商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        shipping_address="测试收货地址", recipient_phone="13800000001",
    )
    update_sales_order_status(db, order.id, "paid")
    shipment = create_shipment(
        db, order.id, item_quantities=[(order.items[0].id, 1)], carrier="中通", tracking_no=tracking_no,
    )
    add_shipping_label(db, shipment.id, content=_label_bytes(), original_filename="label.jpg")
    mark_shipment_shipped(db, shipment.id)
    return order.id, shipment.id


def test_domestic_logistics_list_page_loads(client):
    http, db, _tmp = client
    shipped_shipment(db)
    response = http.get("/domestic-logistics")
    assert response.status_code == 200
    assert "国内物流" in response.text
    assert "路由测试客户" in response.text


def test_domestic_logistics_page_shows_scheduler_status_off_by_default(client):
    http, db, _tmp = client
    response = http.get("/domestic-logistics")
    assert response.status_code == 200
    assert "自动物流更新：未开启" in response.text
    assert "最近后台检查" not in response.text  # nothing to show before any cycle has run


def test_domestic_logistics_page_shows_scheduler_running_when_active(client, monkeypatch):
    import datetime as datetime_module
    http, db, _tmp = client
    monkeypatch.setattr(main_module, "shipment_tracking_scheduler_running", lambda: True)
    monkeypatch.setattr(
        main_module, "shipment_tracking_last_cycle_at",
        lambda: datetime_module.datetime(2026, 9, 6, 3, 0, tzinfo=datetime_module.timezone.utc),
    )

    response = http.get("/domestic-logistics")

    assert response.status_code == 200
    assert "自动物流更新：运行中" in response.text
    assert "最近后台检查" in response.text


def test_domestic_logistics_filter_pill_narrows_results(client):
    http, db, _tmp = client
    shipped_shipment(db)
    response = http.get("/domestic-logistics?status=delivered")
    assert response.status_code == 200
    assert "路由测试客户" not in response.text  # tracking_status is still None, not delivered


def test_domestic_logistics_detail_page_loads(client):
    http, db, _tmp = client
    _order_id, shipment_id = shipped_shipment(db)
    response = http.get(f"/domestic-logistics/{shipment_id}")
    assert response.status_code == 200
    assert "暂无轨迹" in response.text


def test_domestic_logistics_detail_404_for_missing_shipment(client):
    http, _db, _tmp = client
    response = http.get("/domestic-logistics/999999")
    assert response.status_code == 404


def test_track_now_success_redirects_with_message(client, monkeypatch):
    http, db, _tmp = client
    _order_id, shipment_id = shipped_shipment(db)

    def fake_query(session, shipment_id_arg, **kwargs):
        shipment = session.get(SalesShipment, shipment_id_arg)
        shipment.tracking_status = "delivered"
        shipment.tracking_terminal = True
        session.commit()
        return shipment

    monkeypatch.setattr(main_module, "query_shipment_tracking", fake_query)
    response = http.post(f"/domestic-logistics/{shipment_id}/track-now", follow_redirects=False)
    assert response.status_code == 303
    assert "message=" in response.headers["location"]


def test_track_now_missing_tracking_no_redirects_with_error_not_500(client):
    http, db, _tmp = client
    _order_id, shipment_id = shipped_shipment(db, tracking_no=None)
    response = http.post(f"/domestic-logistics/{shipment_id}/track-now", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_sales_order_track_now_route_redirects_back_to_order(client, monkeypatch):
    http, db, _tmp = client
    order_id, shipment_id = shipped_shipment(db)

    def fake_query(session, shipment_id_arg, **kwargs):
        return session.get(SalesShipment, shipment_id_arg)

    monkeypatch.setattr(main_module, "query_shipment_tracking", fake_query)
    response = http.post(f"/sales-orders/{order_id}/shipments/{shipment_id}/track-now", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/sales-orders/{order_id}"
