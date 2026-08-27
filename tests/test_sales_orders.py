from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Customer, Product, SalesOrder, Salesperson
from app.sales_order_service import (
    ALLOWED_TRANSITIONS, DEFAULT_SALESPERSON_NAME, SalesOrderItemInput, cancel_sales_order,
    create_customer, create_sales_order, ensure_default_salesperson, get_sales_order,
    list_sales_orders, status_counts, update_sales_order_status,
)


def product(db, suffix: str, *, sale_price: int | None = 900) -> Product:
    item = Product(
        internal_sku=f"SO-{suffix:0>4}", jan=f"0498000000{suffix:0>3}",
        name_cn=f"销售商品{suffix}", name_ja=f"販売商品{suffix}", sale_price=sale_price,
    )
    db.add(item)
    db.flush()
    return item


def customer(db, name: str = "测试客户", **kwargs):
    return create_customer(db, name=name, phone=kwargs.pop("phone", "13800000000"), wechat_name="wx_test", **kwargs)


def simple_order(db, *, customer_row=None, name_suffix: str = "0"):
    salesperson = ensure_default_salesperson(db)
    buyer = customer_row or customer(db, f"状态测试客户{name_suffix}")
    return create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name=f"状态测试商品{name_suffix}", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )


# ---------------- status transition tests ----------------


def test_submitted_to_ready_to_ship_succeeds(db_session):
    order = simple_order(db_session, name_suffix="1")
    updated = update_sales_order_status(db_session, order.id, "ready_to_ship")
    assert updated.status == "ready_to_ship"


def test_ready_to_ship_to_shipped_succeeds(db_session):
    order = simple_order(db_session, name_suffix="2")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    updated = update_sales_order_status(db_session, order.id, "shipped")
    assert updated.status == "shipped"


def test_shipped_to_completed_succeeds(db_session):
    order = simple_order(db_session, name_suffix="3")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    update_sales_order_status(db_session, order.id, "shipped")
    updated = update_sales_order_status(db_session, order.id, "completed")
    assert updated.status == "completed"


def test_submitted_to_cancelled_succeeds(db_session):
    order = simple_order(db_session, name_suffix="4")
    updated = update_sales_order_status(db_session, order.id, "cancelled")
    assert updated.status == "cancelled"


def test_ready_to_ship_to_cancelled_succeeds(db_session):
    order = simple_order(db_session, name_suffix="5")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    updated = update_sales_order_status(db_session, order.id, "cancelled")
    assert updated.status == "cancelled"


def test_submitted_to_shipped_rejected(db_session):
    order = simple_order(db_session, name_suffix="6")
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "shipped")


def test_ready_to_ship_to_completed_rejected(db_session):
    order = simple_order(db_session, name_suffix="7")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "completed")


def test_shipped_to_cancelled_rejected(db_session):
    order = simple_order(db_session, name_suffix="8")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    update_sales_order_status(db_session, order.id, "shipped")
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "cancelled")


def test_completed_cannot_transition_further(db_session):
    order = simple_order(db_session, name_suffix="9")
    update_sales_order_status(db_session, order.id, "ready_to_ship")
    update_sales_order_status(db_session, order.id, "shipped")
    update_sales_order_status(db_session, order.id, "completed")
    assert ALLOWED_TRANSITIONS["completed"] == set()
    for target in ("submitted", "ready_to_ship", "shipped", "cancelled"):
        with pytest.raises(ValueError):
            update_sales_order_status(db_session, order.id, target)


def test_cancelled_cannot_be_restored(db_session):
    order = simple_order(db_session, name_suffix="10")
    update_sales_order_status(db_session, order.id, "cancelled")
    assert ALLOWED_TRANSITIONS["cancelled"] == set()
    for target in ("submitted", "ready_to_ship", "shipped", "completed"):
        with pytest.raises(ValueError):
            update_sales_order_status(db_session, order.id, target)


def test_status_counts_reflect_current_orders(db_session):
    a = simple_order(db_session, name_suffix="11")
    b = simple_order(db_session, name_suffix="12")
    update_sales_order_status(db_session, b.id, "ready_to_ship")
    counts = status_counts(db_session)
    assert counts["submitted"] >= 1
    assert counts["ready_to_ship"] >= 1
    assert counts["completed"] == counts.get("completed", 0)


# ---------------- shipping address snapshot tests ----------------


def test_new_order_defaults_recipient_snapshot_from_customer(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "地址快照客户1", phone="13711112222")
    buyer.address = "东京都渋谷区1-1-1"
    db_session.flush()
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="地址测试商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    assert order.recipient_name_snapshot == "地址快照客户1"
    assert order.recipient_phone_snapshot == "13711112222"
    assert order.shipping_address_snapshot == "东京都渋谷区1-1-1"


def test_new_order_accepts_explicit_recipient_override(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "地址快照客户2")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="地址测试商品2", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        recipient_name="张三", recipient_phone="13900009999", shipping_address="大阪府大阪市2-2-2",
    )
    assert order.recipient_name_snapshot == "张三"
    assert order.recipient_phone_snapshot == "13900009999"
    assert order.shipping_address_snapshot == "大阪府大阪市2-2-2"


def test_customer_address_change_does_not_rewrite_existing_order_snapshot(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "地址快照客户3")
    buyer.address = "原始地址"
    db_session.flush()
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="地址测试商品3", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    assert order.shipping_address_snapshot == "原始地址"
    buyer.address = "客户改后的新地址"
    db_session.commit()
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.shipping_address_snapshot == "原始地址"
    assert reloaded.customer.address == "客户改后的新地址"


def test_same_customer_can_have_orders_with_different_recipients(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "地址快照客户4")
    order_a = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="商品A", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        recipient_name="收件人甲", shipping_address="地址甲",
    )
    order_b = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="商品B", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        recipient_name="收件人乙", shipping_address="地址乙",
    )
    assert order_a.recipient_name_snapshot == "收件人甲" and order_a.shipping_address_snapshot == "地址甲"
    assert order_b.recipient_name_snapshot == "收件人乙" and order_b.shipping_address_snapshot == "地址乙"


# ---------------- service / DB tests ----------------


def test_create_customer_minimal(db_session):
    row = create_customer(db_session, name="张三")
    assert row.id is not None and row.name == "张三"
    assert row.phone is None and row.wechat_name is None


def test_default_salesperson_created_once(db_session):
    first = ensure_default_salesperson(db_session)
    second = ensure_default_salesperson(db_session)
    assert first.id == second.id
    count = db_session.scalar(select(Salesperson).where(Salesperson.name == DEFAULT_SALESPERSON_NAME))
    assert count is not None
    total = len(list(db_session.scalars(select(Salesperson).where(Salesperson.name == DEFAULT_SALESPERSON_NAME))))
    assert total == 1


def test_order_no_auto_generated_and_unique(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    item = product(db_session, "1")
    first = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("900"))],
    )
    second = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("900"))],
    )
    assert first.order_no != second.order_no
    assert first.order_no.startswith("SO-")
    assert first.status == "submitted"


def test_order_with_multiple_items_and_existing_product_snapshot(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    item = product(db_session, "2", sale_price=1200)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[
            SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=2, unit_sale_price=Decimal("1100")),
            SalesOrderItemInput(product_id=None, manual_name="手工商品A", jan=None, quantity=1, unit_sale_price=Decimal("500")),
        ],
    )
    assert len(order.items) == 2
    existing_line, manual_line = order.items
    assert existing_line.product_id == item.id
    assert existing_line.product_name_snapshot == item.display_name or existing_line.product_name_snapshot == item.name_cn
    assert existing_line.jan_snapshot == item.jan
    # order snapshot price differs from product.sale_price on purpose
    assert existing_line.unit_sale_price == Decimal("1100") != item.sale_price
    assert manual_line.product_id is None
    assert manual_line.product_name_snapshot == "手工商品A"
    assert manual_line.jan_snapshot is None
    assert order.total_amount == Decimal("2200.00") + Decimal("500.00") == Decimal("2700.00")
    assert order.total_quantity == 3


def test_manual_item_without_jan_submits(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="没有JAN的商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    assert order.items[0].jan_snapshot is None
    assert order.items[0].product_id is None


def test_quantity_must_be_positive(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    with pytest.raises(ValueError):
        create_sales_order(
            db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
            items=[SalesOrderItemInput(product_id=None, manual_name="坏商品", jan=None, quantity=0, unit_sale_price=Decimal("100"))],
        )


def test_price_must_not_be_negative(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    with pytest.raises(ValueError):
        create_sales_order(
            db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
            items=[SalesOrderItemInput(product_id=None, manual_name="坏商品", jan=None, quantity=1, unit_sale_price=Decimal("-1"))],
        )


def test_cancelled_order_still_exists(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="待取消商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    cancel_sales_order(db_session, order.id)
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded is not None
    assert reloaded.status == "cancelled"
    assert db_session.get(SalesOrder, order.id) is not None
    listed = list_sales_orders(db_session, status="cancelled")
    assert any(row.id == order.id for row in listed)


# ---------------- route tests ----------------


def test_sales_orders_list_and_new_pages_return_200(client):
    http, db, _ = client
    assert http.get("/sales-orders").status_code == 200
    assert http.get("/sales-orders/new").status_code == 200


def test_product_search_api_returns_sale_price(client):
    http, db, _ = client
    item = product(db, "3", sale_price=750)
    response = http.get("/api/products/search", params={"q": item.internal_sku})
    assert response.status_code == 200
    payload = response.json()
    matches = [row for row in payload if row["id"] == item.id]
    assert matches and Decimal(matches[0]["sale_price"]) == Decimal("750")


def test_quick_add_customer_then_submit_order(client):
    http, db, _ = client
    item = product(db, "4", sale_price=600)
    created = http.post("/api/customers", json={"name": "微信客户小李", "phone": "13900001111"})
    assert created.status_code == 201
    new_customer_id = created.json()["id"]

    items_payload = [
        {"product_id": item.id, "manual_name": None, "jan": item.jan, "quantity": 2, "unit_sale_price": "600", "note": None},
        {"product_id": None, "manual_name": "手工商品B", "jan": None, "quantity": 1, "unit_sale_price": "300", "note": "客户备注"},
    ]
    form = {
        "customer_id": str(new_customer_id),
        "salesperson_id": str(ensure_default_salesperson(db).id),
        "note": "微信订单测试",
        "items_json": json.dumps(items_payload),
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    detail_url = response.headers["location"]

    detail = http.get(detail_url)
    assert detail.status_code == 200
    assert "手工商品B" in detail.text
    assert "微信客户小李" in detail.text

    order_id = int(detail_url.rstrip("/").rsplit("/", 1)[-1])
    order = get_sales_order(db, order_id)
    assert order.total_quantity == 3
    assert order.total_amount == Decimal("1500.00")


def test_cancel_order_route(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "取消测试客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="待取消商品路由", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    response = http.post(f"/sales-orders/{order.id}/status", data={"status": "cancelled"}, follow_redirects=False)
    assert response.status_code == 303
    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "已取消" in detail.text
    assert get_sales_order(db, order.id).status == "cancelled"


def test_status_filter_and_tab_counts_are_correct(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "工作台筛选客户")
    submitted_order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="待处理商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    ready_order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="待发货商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    update_sales_order_status(db, ready_order.id, "ready_to_ship")

    filtered = http.get("/sales-orders", params={"status": "ready_to_ship"})
    assert filtered.status_code == 200
    assert "待发货商品" in filtered.text
    assert "待处理商品" not in filtered.text

    unfiltered = http.get("/sales-orders")
    assert "待处理商品" in unfiltered.text and "待发货商品" in unfiltered.text

    counts = status_counts(db)
    assert counts["submitted"] >= 1 and counts["ready_to_ship"] >= 1


def test_new_order_shows_product_image_or_placeholder(client):
    http, db, _ = client
    item = product(db, "5", sale_price=500)
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "图片测试客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[
            SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("500")),
            SalesOrderItemInput(product_id=None, manual_name="无图手工商品", jan=None, quantity=1, unit_sale_price=Decimal("100")),
        ],
    )
    listing = http.get("/sales-orders")
    assert listing.status_code == 200
    assert "product-image-empty" in listing.text  # placeholder for the manual item without a product image

    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "product-image-empty" in detail.text


def test_primary_action_buttons_present_per_status(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="route-action")
    listing = http.get("/sales-orders")
    assert "设为待发货" in listing.text

    update_sales_order_status(db, order.id, "ready_to_ship")
    listing = http.get("/sales-orders?status=ready_to_ship")
    assert "标记已发货" in listing.text


def test_status_route_updates_successfully(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="status-route-ok")
    response = http.post(f"/sales-orders/{order.id}/status", data={"status": "ready_to_ship"}, follow_redirects=False)
    assert response.status_code == 303
    assert get_sales_order(db, order.id).status == "ready_to_ship"


def test_illegal_status_transition_returns_error(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="status-route-bad")
    response = http.post(f"/sales-orders/{order.id}/status", data={"status": "shipped"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert get_sales_order(db, order.id).status == "submitted"


def test_detail_page_shows_shipping_snapshot(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "详情页地址客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="详情页商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        recipient_name="收件人详情页", recipient_phone="13611112222", shipping_address="详情页收货地址",
    )
    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "收件人详情页" in detail.text
    assert "13611112222" in detail.text
    assert "详情页收货地址" in detail.text


def test_new_order_page_has_recipient_fields_and_accepts_submission(client):
    http, db, _ = client
    new_page = http.get("/sales-orders/new")
    assert new_page.status_code == 200
    assert 'name="recipient_name"' in new_page.text
    assert 'name="shipping_address"' in new_page.text

    created = http.post("/api/customers", json={"name": "新建页客户", "phone": "13500001111", "address": "客户默认地址"})
    new_customer_id = created.json()["id"]
    form = {
        "customer_id": str(new_customer_id),
        "salesperson_id": str(ensure_default_salesperson(db).id),
        "note": "",
        "recipient_name": "现场收件人",
        "recipient_phone": "13500002222",
        "shipping_address": "现场填写地址",
        "items_json": json.dumps([
            {"product_id": None, "manual_name": "新建页测试商品", "jan": None, "quantity": 1, "unit_sale_price": "100", "note": None},
        ]),
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    order_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    order = get_sales_order(db, order_id)
    assert order.recipient_name_snapshot == "现场收件人"
    assert order.shipping_address_snapshot == "现场填写地址"
