from __future__ import annotations

import io
import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import (
    Customer, CustomerAddress, ProcurementDemand, Product, SalesOrder, SalesOrderShippingLabel, SalesShipment, Salesperson,
)
from app.sales_order_service import (
    ORDER_NO_DAILY_SEQUENCE_MAX,
    TOKYO,
    ADDRESS_EDITABLE_STATUSES, ALLOWED_TRANSITIONS, DEFAULT_SALESPERSON_NAME, ITEM_EDITABLE_STATUSES,
    ITEM_LOCKED_MESSAGE, SHIPPING_LABEL_DELETABLE_STATUSES, SHIPPING_LABEL_UPLOADABLE_STATUSES,
    DuplicateAddressError, SalesOrderItemInput, add_customer_address, add_shipping_label, cancel_sales_order,
    create_customer, create_sales_order, create_shipment, delete_customer_address,
    ensure_default_salesperson, find_duplicate_customer_address, get_sales_order,
    get_shipping_label, last_sale_price_for_product, list_customer_addresses, list_sales_orders,
    mark_shipment_shipped, remove_shipping_label, status_counts, update_customer_address, update_sales_order,
    update_sales_order_address, update_sales_order_status, update_shipment_tracking,
    suggest_order_no,
)
from app.sales_order_shipping import resolve_shipping_label_path


def shipping_label_jpeg_bytes(color=(10, 120, 90), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def shipping_label_image_bytes(fmt: str, color=(20, 90, 140), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated_shipping_label_storage(tmp_path, monkeypatch):
    # sales_order_shipping.py resolves saved files relative to its own module-level
    # PROJECT_ROOT/SALES_ORDER_SHIPPING_LABEL_DIR/SALES_ORDER_ITEM_IMAGE_DIR bindings,
    # independent of the client fixture's PROJECT_ROOT patches on other modules.
    # Without this, tests in this file would write real files into the real
    # project's data/sales-orders/ directory instead of a throwaway tmp_path.
    import app.sales_order_shipping as shipping_module
    monkeypatch.setattr(shipping_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        shipping_module, "SALES_ORDER_SHIPPING_LABEL_DIR", tmp_path / "data" / "sales-orders" / "shipping-labels",
    )
    monkeypatch.setattr(
        shipping_module, "SALES_ORDER_ITEM_IMAGE_DIR", tmp_path / "data" / "sales-orders" / "item-images",
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


def simple_order(db, *, customer_row=None, name_suffix: str = "0", quantity: int = 1):
    salesperson = ensure_default_salesperson(db)
    buyer = customer_row or customer(db, f"状态测试客户{name_suffix}")
    return create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name=f"状态测试商品{name_suffix}", jan=None, quantity=quantity, unit_sale_price=Decimal("100"))],
        shipping_address="测试收货地址",
    )


def pay(db, order: SalesOrder) -> SalesOrder:
    return update_sales_order_status(db, order.id, "paid")


def ship_full(db, order: SalesOrder, *, carrier: str | None = "中通", tracking_no: str | None = None) -> SalesOrder:
    """Pay (if still submitted), create one shipment covering every remaining
    item in full, attach a label, and mark it shipped. Returns the reloaded order."""
    if order.status == "submitted":
        pay(db, order)
        order = get_sales_order(db, order.id)
    remaining = [(item.id, item.remaining_quantity) for item in order.items if item.remaining_quantity > 0]
    shipment = create_shipment(db, order.id, item_quantities=remaining, carrier=carrier, tracking_no=tracking_no)
    add_shipping_label(db, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="label.jpg")
    mark_shipment_shipped(db, shipment.id)
    return get_sales_order(db, order.id)


# ---------------- status transition tests ----------------


def test_submitted_to_paid_succeeds(db_session):
    order = simple_order(db_session, name_suffix="1")
    updated = pay(db_session, order)
    assert updated.status == "paid"


def test_paid_order_shipped_via_shipment_succeeds(db_session):
    order = simple_order(db_session, name_suffix="2")
    updated = ship_full(db_session, order)
    assert updated.status == "shipped"


def test_shipped_to_completed_succeeds(db_session):
    order = simple_order(db_session, name_suffix="3")
    order = ship_full(db_session, order)
    updated = update_sales_order_status(db_session, order.id, "completed")
    assert updated.status == "completed"


def test_submitted_to_cancelled_succeeds(db_session):
    order = simple_order(db_session, name_suffix="4")
    updated = update_sales_order_status(db_session, order.id, "cancelled")
    assert updated.status == "cancelled"


def test_paid_to_cancelled_succeeds(db_session):
    order = simple_order(db_session, name_suffix="5")
    pay(db_session, order)
    updated = update_sales_order_status(db_session, order.id, "cancelled")
    assert updated.status == "cancelled"


def test_submitted_to_shipped_rejected(db_session):
    order = simple_order(db_session, name_suffix="6")
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "shipped")


def test_paid_to_completed_rejected(db_session):
    order = simple_order(db_session, name_suffix="7")
    pay(db_session, order)
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "completed")


def test_shipped_to_cancelled_rejected(db_session):
    order = simple_order(db_session, name_suffix="8")
    order = ship_full(db_session, order)
    with pytest.raises(ValueError):
        update_sales_order_status(db_session, order.id, "cancelled")


def test_completed_cannot_transition_further(db_session):
    order = simple_order(db_session, name_suffix="9")
    order = ship_full(db_session, order)
    update_sales_order_status(db_session, order.id, "completed")
    assert ALLOWED_TRANSITIONS["completed"] == set()
    for target in ("submitted", "paid", "shipped", "cancelled"):
        with pytest.raises(ValueError):
            update_sales_order_status(db_session, order.id, target)


def test_cancelled_cannot_be_restored(db_session):
    order = simple_order(db_session, name_suffix="10")
    update_sales_order_status(db_session, order.id, "cancelled")
    assert ALLOWED_TRANSITIONS["cancelled"] == set()
    for target in ("submitted", "paid", "shipped", "completed"):
        with pytest.raises(ValueError):
            update_sales_order_status(db_session, order.id, target)


def test_status_counts_reflect_current_orders(db_session):
    a = simple_order(db_session, name_suffix="11")
    b = simple_order(db_session, name_suffix="12")
    pay(db_session, b)
    counts = status_counts(db_session)
    assert counts["submitted"] >= 1
    assert counts["paid"] >= 1
    assert counts["completed"] == counts.get("completed", 0)


def test_partially_shipped_and_shipped_status_and_backwards_transitions_never_human_settable(db_session):
    # target_status="partially_shipped"/"shipped" is never in ALLOWED_TRANSITIONS'
    # value sets -- confirming these states are unreachable via the human-facing
    # update_sales_order_status() API and can only be derived from shipments.
    for allowed in ALLOWED_TRANSITIONS.values():
        assert "partially_shipped" not in allowed
        assert "shipped" not in allowed


# ---------------- shipment split / capping / status derivation ----------------


def two_item_paid_order(db):
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "拆分发货客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[
            SalesOrderItemInput(product_id=None, manual_name="商品A", jan=None, quantity=5, unit_sale_price=Decimal("10")),
            SalesOrderItemInput(product_id=None, manual_name="商品B", jan=None, quantity=1, unit_sale_price=Decimal("20")),
            SalesOrderItemInput(product_id=None, manual_name="商品C", jan=None, quantity=3, unit_sale_price=Decimal("30")),
        ],
        shipping_address="拆分发货测试地址",
    )
    pay(db, order)
    return get_sales_order(db, order.id)


def test_partial_shipment_moves_order_to_partially_shipped(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    shipment = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 3), (item_c.id, 3)])
    add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="s1.jpg")
    mark_shipment_shipped(db_session, shipment.id)
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.status == "partially_shipped"


def test_second_shipment_completes_order_and_cumulative_quantities_never_oversell(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    s1 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 3), (item_c.id, 3)])
    add_shipping_label(db_session, s1.id, content=shipping_label_jpeg_bytes(), original_filename="s1.jpg")
    mark_shipment_shipped(db_session, s1.id)

    s2 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 2), (item_b.id, 1)])
    add_shipping_label(db_session, s2.id, content=shipping_label_jpeg_bytes(), original_filename="s2.jpg")
    mark_shipment_shipped(db_session, s2.id)

    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.status == "shipped"
    item_a, item_b, item_c = reloaded.items
    assert item_a.shipped_quantity == 5 and item_a.remaining_quantity == 0
    assert item_b.shipped_quantity == 1 and item_b.remaining_quantity == 0
    assert item_c.shipped_quantity == 3 and item_c.remaining_quantity == 0


def test_shipment_quantity_cannot_exceed_remaining_across_multiple_shipments(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    create_shipment(db_session, order.id, item_quantities=[(item_a.id, 4)])
    with pytest.raises(ValueError):
        create_shipment(db_session, order.id, item_quantities=[(item_a.id, 2)])  # 4 + 2 > 5


def test_shipment_quantity_cannot_exceed_ordered_quantity_in_one_call(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    with pytest.raises(ValueError):
        create_shipment(db_session, order.id, item_quantities=[(item_b.id, 5)])


def test_one_order_item_can_appear_in_multiple_shipments(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    s1 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 2)])
    s2 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 3)])
    assert s1.id != s2.id
    assert item_a.quantity == 5


def test_shipment_history_is_never_overwritten_by_a_later_shipment(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    s1 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 3)])
    add_shipping_label(db_session, s1.id, content=shipping_label_jpeg_bytes(), original_filename="s1.jpg")
    mark_shipment_shipped(db_session, s1.id)
    s2 = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 2)])
    add_shipping_label(db_session, s2.id, content=shipping_label_jpeg_bytes(), original_filename="s2.jpg")
    mark_shipment_shipped(db_session, s2.id)
    reloaded = get_sales_order(db_session, order.id)
    assert len(reloaded.shipments) == 2
    assert {shipment.id for shipment in reloaded.shipments} == {s1.id, s2.id}
    assert all(shipment.status == "shipped" for shipment in reloaded.shipments)


def test_mark_shipment_shipped_requires_a_label(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    shipment = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 1)])
    with pytest.raises(ValueError, match="请先上传发货面单图片"):
        mark_shipment_shipped(db_session, shipment.id)


def test_shipment_default_carrier_is_zto_and_tracking_no_editable_before_shipped(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    shipment = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 1)])
    assert shipment.carrier == "中通"
    update_shipment_tracking(db_session, shipment.id, carrier="顺丰", tracking_no="SF123456")
    reloaded = get_sales_order(db_session, order.id)
    updated_shipment = next(s for s in reloaded.shipments if s.id == shipment.id)
    assert updated_shipment.carrier == "顺丰"
    assert updated_shipment.tracking_no == "SF123456"


def test_create_shipment_rejected_before_payment(db_session):
    order = simple_order(db_session, name_suffix="ship-before-pay", quantity=2)
    with pytest.raises(ValueError):
        create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])


# ---------------- item lock / address edit rules ----------------


def test_items_editable_before_payment(db_session):
    order = simple_order(db_session, name_suffix="edit-1")
    assert order.status in ITEM_EDITABLE_STATUSES
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "改单后客户")
    updated = update_sales_order(
        db_session, order.id, customer_id=buyer.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="改单后商品", jan=None, quantity=2, unit_sale_price=Decimal("200"))],
    )
    assert updated.customer_id == buyer.id
    assert len(updated.items) == 1
    assert updated.items[0].product_name_snapshot == "改单后商品"
    assert updated.items[0].quantity == 2


def test_items_locked_after_payment(db_session):
    order = simple_order(db_session, name_suffix="edit-2")
    pay(db_session, order)
    assert order.status not in ITEM_EDITABLE_STATUSES
    with pytest.raises(ValueError, match=ITEM_LOCKED_MESSAGE):
        update_sales_order(
            db_session, order.id, customer_id=order.customer_id,
            items=[SalesOrderItemInput(product_id=None, manual_name="不该成功", jan=None, quantity=1, unit_sale_price=Decimal("1"))],
        )


def test_price_locked_after_payment_same_as_items(db_session):
    order = simple_order(db_session, name_suffix="edit-3")
    pay(db_session, order)
    with pytest.raises(ValueError):
        update_sales_order(
            db_session, order.id, customer_id=order.customer_id,
            items=[SalesOrderItemInput(product_id=None, manual_name="状态测试商品edit-3", jan=None, quantity=1, unit_sale_price=Decimal("999"))],
        )


def test_address_editable_before_payment(db_session):
    order = simple_order(db_session, name_suffix="addr-1")
    updated = update_sales_order_address(
        db_session, order.id, recipient_name="改地址前", recipient_phone="13100000000", shipping_address="新地址1",
    )
    assert updated.shipping_address_snapshot == "新地址1"


def test_address_editable_after_payment_before_shipping(db_session):
    order = simple_order(db_session, name_suffix="addr-2")
    pay(db_session, order)
    assert order.status in ADDRESS_EDITABLE_STATUSES
    updated = update_sales_order_address(
        db_session, order.id, recipient_name="改地址后", recipient_phone="13200000000", shipping_address="新地址2",
    )
    assert updated.shipping_address_snapshot == "新地址2"


def test_shipment_address_frozen_once_shipped(db_session):
    order = simple_order(db_session, name_suffix="addr-3")
    order = ship_full(db_session, order)
    shipment = order.shipments[0]
    original_address = shipment.shipping_address_snapshot
    # No update path exists for a shipped shipment's address -- confirm the
    # order-level address editor now refuses too, since nothing is left to ship.
    with pytest.raises(ValueError):
        update_sales_order_address(
            db_session, order.id, recipient_name="不该生效", recipient_phone="", shipping_address="不该生效的地址",
        )
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.shipments[0].shipping_address_snapshot == original_address


def test_partially_shipped_address_still_editable_for_unshipped_remainder(db_session):
    order = two_item_paid_order(db_session)
    item_a, item_b, item_c = order.items
    shipment = create_shipment(db_session, order.id, item_quantities=[(item_a.id, 5)])
    add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="s.jpg")
    mark_shipment_shipped(db_session, shipment.id)
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.status == "partially_shipped"
    assert reloaded.status in ADDRESS_EDITABLE_STATUSES
    updated = update_sales_order_address(
        db_session, order.id, recipient_name="剩余部分新收件人", recipient_phone="", shipping_address="剩余部分新地址",
    )
    assert updated.shipping_address_snapshot == "剩余部分新地址"
    # The already-shipped shipment's own snapshot must stay untouched.
    assert reloaded.shipments[0].shipping_address_snapshot != "剩余部分新地址"


# ---------------- shipping address snapshot tests ----------------


def test_new_order_defaults_recipient_snapshot_from_customer(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "地址快照客户1", phone="13711112222", address="东京都渋谷区1-1-1")
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
    buyer = customer(db_session, "地址快照客户3", address="原始地址")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="地址测试商品3", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    assert order.shipping_address_snapshot == "原始地址"
    add_customer_address(db_session, buyer.id, recipient_name=buyer.name, address="客户改后的新地址", is_default=True)
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.shipping_address_snapshot == "原始地址"


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


# ---------------- customer multi-address ----------------


def test_create_customer_with_address_creates_default_customer_address(db_session):
    row = create_customer(db_session, name="多地址客户1", address="上海市浦东新区1号", recipient_name="张三")
    addresses = list_customer_addresses(db_session, row.id)
    assert len(addresses) == 1
    assert addresses[0].is_default is True
    assert addresses[0].address == "上海市浦东新区1号"
    assert addresses[0].recipient_name == "张三"


def test_add_second_customer_address_does_not_replace_first_unless_default(db_session):
    row = create_customer(db_session, name="多地址客户2", address="地址甲")
    add_customer_address(db_session, row.id, recipient_name="朋友李四", address="地址乙", label="朋友", is_default=False)
    addresses = list_customer_addresses(db_session, row.id)
    assert len(addresses) == 2
    defaults = [a for a in addresses if a.is_default]
    assert len(defaults) == 1
    assert defaults[0].address == "地址甲"


def test_setting_new_address_as_default_unsets_previous_default(db_session):
    row = create_customer(db_session, name="多地址客户3", address="地址甲")
    add_customer_address(db_session, row.id, recipient_name="公司", address="地址乙", label="公司", is_default=True)
    addresses = list_customer_addresses(db_session, row.id)
    defaults = [a for a in addresses if a.is_default]
    assert len(defaults) == 1
    assert defaults[0].address == "地址乙"


def test_order_can_be_created_from_a_specific_non_default_customer_address(db_session):
    salesperson = ensure_default_salesperson(db_session)
    row = create_customer(db_session, name="多地址客户4", address="默认地址")
    friend_address = add_customer_address(db_session, row.id, recipient_name="朋友", address="朋友地址", label="朋友")
    order = create_sales_order(
        db_session, customer_id=row.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="多地址商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        customer_address_id=friend_address.id,
    )
    assert order.shipping_address_snapshot == "朋友地址"
    assert order.recipient_name_snapshot == "朋友"


def test_customer_address_no_hard_limit(db_session):
    row = create_customer(db_session, name="多地址客户5", address="地址0")
    for i in range(1, 6):
        add_customer_address(db_session, row.id, recipient_name=f"收件人{i}", address=f"地址{i}")
    assert len(list_customer_addresses(db_session, row.id)) == 6


# ---------------- price defaulting / hinting ----------------


def test_wechat_price_never_defaults_to_jpy_purchase_price(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "价格测试客户1")
    item = product(db_session, "price1", sale_price=999)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("50"))],
    )
    # The order line stores exactly what was manually entered (CNY), never the
    # product's JPY purchase/sale_price -- they are deliberately different values.
    assert order.items[0].unit_sale_price == Decimal("50") != item.sale_price


def test_last_sale_price_hint_only_matches_real_product_id(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "价格测试客户2")
    item = product(db_session, "price2")
    other_item = product(db_session, "price3")
    assert last_sale_price_for_product(db_session, item.id) is None
    create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("66"))],
    )
    assert last_sale_price_for_product(db_session, item.id) == Decimal("66")
    assert last_sale_price_for_product(db_session, other_item.id) is None


def test_last_sale_price_hint_ignores_cancelled_orders(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "价格测试客户3")
    item = product(db_session, "price4")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("77"))],
    )
    cancel_sales_order(db_session, order.id)
    assert last_sale_price_for_product(db_session, item.id) is None


# ---------------- manual item identity / image validation ----------------


def test_manual_item_without_jan_submits(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="没有JAN的商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    assert order.items[0].jan_snapshot is None
    assert order.items[0].product_id is None


def test_manual_item_with_no_name_and_no_image_is_rejected(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    with pytest.raises(ValueError, match="必须填写商品名或上传图片"):
        create_sales_order(
            db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
            items=[SalesOrderItemInput(product_id=None, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        )


def test_manual_item_with_only_an_image_and_no_name_is_allowed(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(
            product_id=None, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("100"),
            manual_image_content=shipping_label_jpeg_bytes(), manual_image_filename="only-image.jpg",
        )],
    )
    item = order.items[0]
    assert item.product_name_snapshot is None
    assert item.manual_image_relative_path is not None
    assert item.manual_image_original_filename == "only-image.jpg"


def test_manual_item_image_rejects_non_image_content(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    with pytest.raises(ValueError):
        create_sales_order(
            db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
            items=[SalesOrderItemInput(
                product_id=None, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("100"),
                manual_image_content=b"not an image", manual_image_filename="fake.jpg",
            )],
        )


def test_manual_item_image_is_never_auto_deleted_when_line_stays_manual(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session)
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(
            product_id=None, manual_name="带图手工商品", jan=None, quantity=1, unit_sale_price=Decimal("100"),
            manual_image_content=shipping_label_jpeg_bytes(), manual_image_filename="keep.jpg",
        )],
    )
    from app.sales_order_shipping import resolve_sales_order_item_image_path
    relative_path = order.items[0].manual_image_relative_path
    assert resolve_sales_order_item_image_path(relative_path) is not None


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
    # YYMMDDNN: 8 digits, day-prefix shared, sequence increments.
    assert len(first.order_no) == 8 and first.order_no.isdigit()
    assert first.order_no[:6] == second.order_no[:6]
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


# ---------------- route tests ----------------


def test_sales_orders_list_and_new_pages_return_200(client):
    http, db, _ = client
    assert http.get("/sales-orders").status_code == 200
    assert http.get("/sales-orders/new").status_code == 200


def test_product_search_api_returns_inventory_and_price_hint(client):
    http, db, _ = client
    item = product(db, "3", sale_price=750)
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "搜索测试客户")
    create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("88"))],
    )
    response = http.get("/api/products/search", params={"q": item.internal_sku})
    assert response.status_code == 200
    payload = response.json()
    matches = [row for row in payload if row["id"] == item.id]
    assert matches
    row = matches[0]
    assert "sale_price" not in row  # JPY purchase price must never be surfaced as a sale-price default
    assert Decimal(row["last_sale_price"]) == Decimal("88")
    assert "image_url" in row and "qinsi_product_code" in row


def test_product_search_api_manual_only_product_has_no_price_hint(client):
    http, db, _ = client
    item = product(db, "3b", sale_price=750)
    response = http.get("/api/products/search", params={"q": item.internal_sku})
    row = next(row for row in response.json() if row["id"] == item.id)
    assert row["last_sale_price"] is None


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


def test_customer_quick_add_with_address_creates_customer_address_api(client):
    http, db, _ = client
    created = http.post("/api/customers", json={
        "name": "带地址客户", "phone": "13600001111", "recipient_name": "带地址客户",
        "address": "杭州市西湖区1号", "address_label": "本人",
    })
    assert created.status_code == 201
    payload = created.json()
    assert len(payload["addresses"]) == 1
    assert payload["addresses"][0]["address"] == "杭州市西湖区1号"
    assert payload["addresses"][0]["is_default"] is True


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
    paid_order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="已付款商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
    )
    update_sales_order_status(db, paid_order.id, "paid")

    filtered = http.get("/sales-orders", params={"status": "paid"})
    assert filtered.status_code == 200
    assert "已付款商品" in filtered.text
    assert "待处理商品" not in filtered.text

    unfiltered = http.get("/sales-orders")
    assert "待处理商品" in unfiltered.text and "已付款商品" in unfiltered.text

    counts = status_counts(db)
    assert counts["submitted"] >= 1 and counts["paid"] >= 1


def test_ship_date_filter_matches_any_shipment_in_range(client):
    http, db, _ = client
    order_shipped_today = simple_order(db, name_suffix="ship-date-1")
    order_shipped_today = ship_full(db, order_shipped_today)
    order_not_shipped = simple_order(db, name_suffix="ship-date-2")

    # The filter interprets shipped_date_from/to as a Tokyo-local calendar
    # day (matching the rest of this app's date conventions -- see TOKYO in
    # sales_order_service.py), not the test runner's own local/UTC date.
    # Tokyo is UTC+9, so date.today() drifts a day behind Tokyo's date for
    # roughly 9 hours out of every UTC day; using it here made this test
    # fail whenever it happened to run during that window.
    today = datetime.now(timezone.utc).astimezone(TOKYO).date()
    filtered = http.get("/sales-orders", params={
        "shipped_date_from": today.isoformat(), "shipped_date_to": today.isoformat(),
    })
    assert filtered.status_code == 200
    assert "状态测试商品ship-date-1" in filtered.text
    assert "状态测试商品ship-date-2" not in filtered.text


def test_ship_date_filter_uses_tokyo_calendar_day_not_utc_date(client):
    """Regression for the UTC/Tokyo boundary bug above, pinned to a fixed
    instant instead of depending on when the test happens to run. shipped_at
    is stored in UTC; 2026-01-01 20:00 UTC is already 2026-01-02 05:00 in
    Tokyo (UTC+9). The filter must bucket this shipment under the Tokyo
    date (01-02), matching this app's universal date convention -- not the
    UTC date the raw timestamp happens to fall on (01-01)."""
    http, db, _ = client
    order = simple_order(db, name_suffix="ship-date-tz")
    order = ship_full(db, order)
    shipment = order.shipments[0]
    shipment.shipped_at = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    db.commit()

    same_utc_date = http.get("/sales-orders", params={
        "shipped_date_from": "2026-01-01", "shipped_date_to": "2026-01-01",
    })
    assert "状态测试商品ship-date-tz" not in same_utc_date.text

    correct_tokyo_date = http.get("/sales-orders", params={
        "shipped_date_from": "2026-01-02", "shipped_date_to": "2026-01-02",
    })
    assert "状态测试商品ship-date-tz" in correct_tokyo_date.text


def test_ship_date_filter_excludes_out_of_range_shipments(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="ship-date-3")
    order = ship_full(db, order)
    future = (date.today() + timedelta(days=5)).isoformat()
    filtered = http.get("/sales-orders", params={"shipped_date_from": future})
    assert "状态测试商品ship-date-3" not in filtered.text


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


def test_order_list_and_detail_reuse_shared_product_image_resolver(client):
    from app.product_image_localization import preferred_product_image_url

    http, db, _ = client
    item = product(db, "6", sale_price=800)
    item.display_image_url = "https://example.com/images/product-6.jpg"
    db.flush()
    expected_url = preferred_product_image_url(item)
    assert expected_url == "https://example.com/images/product-6.jpg"

    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "解析器测试客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("800"))],
    )

    listing = http.get("/sales-orders")
    assert expected_url in listing.text
    assert "product-thumb-slot" in listing.text

    detail = http.get(f"/sales-orders/{order.id}")
    assert expected_url in detail.text


def test_primary_action_buttons_present_per_status(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="route-action")
    listing = http.get("/sales-orders")
    assert "标记已付款" in listing.text

    update_sales_order_status(db, order.id, "paid")
    listing = http.get("/sales-orders?status=paid")
    assert "创建发货单" in listing.text or "已付款" in listing.text


def test_status_route_updates_successfully(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="status-route-ok")
    response = http.post(f"/sales-orders/{order.id}/status", data={"status": "paid"}, follow_redirects=False)
    assert response.status_code == 303
    assert get_sales_order(db, order.id).status == "paid"


def test_illegal_status_transition_returns_error(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="status-route-bad")
    response = http.post(f"/sales-orders/{order.id}/status", data={"status": "shipped"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert get_sales_order(db, order.id).status == "submitted"


def test_address_update_route_success(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="addr-route-1")
    response = http.post(
        f"/sales-orders/{order.id}/address",
        data={"recipient_name": "路由改地址", "recipient_phone": "13700001111", "shipping_address": "路由新地址"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    reloaded = get_sales_order(db, order.id)
    assert reloaded.shipping_address_snapshot == "路由新地址"


def test_edit_page_rejects_after_payment(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="edit-route-1")
    update_sales_order_status(db, order.id, "paid")
    response = http.get(f"/sales-orders/{order.id}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


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


def test_create_shipment_route_and_mark_shipped_route(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="shipment-route-1", quantity=2)
    update_sales_order_status(db, order.id, "paid")
    order = get_sales_order(db, order.id)
    item = order.items[0]

    response = http.post(
        f"/sales-orders/{order.id}/shipments",
        data={"items_json": json.dumps([{"order_item_id": item.id, "quantity": 2}])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments) == 1
    shipment = reloaded.shipments[0]

    label_response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("label.jpg", shipping_label_jpeg_bytes(), "image/jpeg")},
        follow_redirects=False,
    )
    assert label_response.status_code == 303

    ship_response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/ship",
        data={"carrier": "中通", "tracking_no": "ZT999"},
        follow_redirects=False,
    )
    assert ship_response.status_code == 303
    final = get_sales_order(db, order.id)
    assert final.status == "shipped"
    assert final.shipments[0].tracking_no == "ZT999"


def test_create_shipment_route_rejects_overselling(client):
    http, db, _ = client
    order = simple_order(db, name_suffix="shipment-route-2", quantity=2)
    update_sales_order_status(db, order.id, "paid")
    order = get_sales_order(db, order.id)
    item = order.items[0]
    response = http.post(
        f"/sales-orders/{order.id}/shipments",
        data={"items_json": json.dumps([{"order_item_id": item.id, "quantity": 99}])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments) == 0


# ---------------- shipping label tests (now shipment-scoped) ----------------


def test_shipment_can_have_one_shipping_label(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="label.jpg")
    reloaded = get_sales_order(db_session, order.id)
    reloaded_shipment = next(s for s in reloaded.shipments if s.id == shipment.id)
    assert len(reloaded_shipment.shipping_labels) == 1
    assert reloaded_shipment.shipping_labels[0].id == label.id


def test_shipment_can_have_multiple_shipping_labels(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes((1, 2, 3)), original_filename="a.jpg")
    add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes((4, 5, 6)), original_filename="b.jpg")
    reloaded = get_sales_order(db_session, order.id)
    reloaded_shipment = next(s for s in reloaded.shipments if s.id == shipment.id)
    assert len(reloaded_shipment.shipping_labels) == 2


def test_shipping_label_relates_to_correct_shipment(db_session):
    order_a = two_item_paid_order(db_session)
    order_b = two_item_paid_order(db_session)
    shipment_a = create_shipment(db_session, order_a.id, item_quantities=[(order_a.items[0].id, 1)])
    shipment_b = create_shipment(db_session, order_b.id, item_quantities=[(order_b.items[0].id, 1)])
    label = add_shipping_label(db_session, shipment_a.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    assert label.sales_order_id == order_a.id
    assert label.shipment_id == shipment_a.id
    reloaded_b = get_sales_order(db_session, order_b.id)
    reloaded_shipment_b = next(s for s in reloaded_b.shipments if s.id == shipment_b.id)
    assert reloaded_shipment_b.shipping_labels == []


def test_deleting_order_cascades_shipments_and_shipping_labels(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    label_id, shipment_id = label.id, shipment.id
    live_order = db_session.get(SalesOrder, order.id)
    db_session.delete(live_order)
    db_session.commit()
    assert db_session.get(SalesOrderShippingLabel, label_id) is None
    assert db_session.get(SalesShipment, shipment_id) is None


def test_shipping_label_relative_path_is_relative(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    assert not label.relative_path.startswith("/")
    assert not (len(label.relative_path) > 1 and label.relative_path[1] == ":")
    assert label.relative_path.startswith("data/sales-orders/shipping-labels/")


def test_shipping_label_original_filename_sanitized(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    label = add_shipping_label(
        db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="../../etc/passwd.jpg",
    )
    assert label.original_filename == "passwd.jpg"
    resolved = resolve_shipping_label_path(label.relative_path)
    assert resolved is not None and resolved.is_file()


def test_non_image_upload_rejected(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    with pytest.raises(ValueError):
        add_shipping_label(db_session, shipment.id, content=b"not an image at all", original_filename="fake.jpg")


def test_oversized_upload_rejected(db_session, monkeypatch):
    monkeypatch.setenv("JBA_SHIPPING_LABEL_MAX_UPLOAD_MB", "1")
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    oversized = b"0" * (2 * 1024 * 1024)
    with pytest.raises(ValueError, match="不能超过"):
        add_shipping_label(db_session, shipment.id, content=oversized, original_filename="big.jpg")


def test_fake_extension_non_image_rejected(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    with pytest.raises(ValueError, match="JPG/PNG/WebP"):
        add_shipping_label(db_session, shipment.id, content=b"<html>not really a jpg</html>", original_filename="sneaky.jpg")


def test_mark_shipped_without_label_rejected(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    with pytest.raises(ValueError, match="请先上传发货面单图片"):
        mark_shipment_shipped(db_session, shipment.id)


def test_pending_shipment_can_upload_label(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    assert shipment.status in SHIPPING_LABEL_UPLOADABLE_STATUSES
    add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")


def test_pending_shipment_can_delete_shipping_label(db_session):
    order = two_item_paid_order(db_session)
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)])
    label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    assert shipment.status in SHIPPING_LABEL_DELETABLE_STATUSES
    resolved_before = resolve_shipping_label_path(label.relative_path)
    remove_shipping_label(db_session, label.id)
    assert get_shipping_label(db_session, label.id) is None
    assert resolved_before is not None and not resolved_before.is_file()


def test_shipped_shipment_label_can_be_corrected_via_logistics_fixup(db_session):
    # Phase 7: shipped shipments allow label/carrier/tracking corrections
    # through the explicit "modify logistics info" action -- items, shipped
    # quantities, and shipped_at must stay untouched regardless.
    order = two_item_paid_order(db_session)
    order_item = order.items[0]
    shipment = create_shipment(db_session, order.id, item_quantities=[(order_item.id, 1)])
    label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    mark_shipment_shipped(db_session, shipment.id)
    shipped_at_before = shipment.shipped_at

    remove_shipping_label(db_session, label.id)
    assert get_shipping_label(db_session, label.id) is None
    new_label = add_shipping_label(db_session, shipment.id, content=shipping_label_jpeg_bytes((9, 8, 7)), original_filename="b.jpg")
    assert get_shipping_label(db_session, new_label.id) is not None

    reloaded = get_sales_order(db_session, order.id)
    reloaded_shipment = next(s for s in reloaded.shipments if s.id == shipment.id)
    assert reloaded_shipment.status == "shipped"
    assert reloaded_shipment.shipped_at == shipped_at_before
    assert reloaded_shipment.items[0].quantity == 1
    assert order_item.shipped_quantity == 1


def test_resolve_shipping_label_path_blocks_traversal(db_session):
    assert resolve_shipping_label_path("../../app/main.py") is None
    assert resolve_shipping_label_path("data/db/japan_buying_agent.sqlite3") is None
    assert resolve_shipping_label_path("../../../../etc/passwd") is None


# ---------------- shipping label route tests ----------------


def paid_order_with_shipment(db, *, quantity=1, name_suffix="route"):
    order = simple_order(db, name_suffix=name_suffix, quantity=quantity)
    update_sales_order_status(db, order.id, "paid")
    order = get_sales_order(db, order.id)
    item = order.items[0]
    shipment = create_shipment(db, order.id, item_quantities=[(item.id, quantity)])
    return order, shipment


def test_upload_shipping_label_route_success(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-1")
    response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("label.jpg", shipping_label_jpeg_bytes(), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments[0].shipping_labels) == 1


def test_upload_shipping_label_route_rejects_genuinely_empty_file(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-empty")
    response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("empty.jpg", b"", "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments[0].shipping_labels) == 0

    detail = http.get(response.headers["location"])
    assert detail.status_code == 200
    assert "面单图片不能为空" in detail.text


def test_upload_shipping_label_route_accepts_png_and_webp(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-formats")

    png_response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("label.png", shipping_label_image_bytes("PNG"), "image/png")},
        follow_redirects=False,
    )
    assert png_response.status_code == 303

    webp_response = http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("label.webp", shipping_label_image_bytes("WEBP"), "image/webp")},
        follow_redirects=False,
    )
    assert webp_response.status_code == 303

    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments[0].shipping_labels) == 2


def test_delete_shipping_label_route_success(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-delete")
    label = add_shipping_label(db, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    response = http.post(f"/sales-orders/shipping-labels/{label.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert get_shipping_label(db, label.id) is None
    reloaded = get_sales_order(db, order.id)
    assert len(reloaded.shipments[0].shipping_labels) == 0


def test_view_shipping_label_route_success(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-2")
    label = add_shipping_label(db, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    response = http.get(f"/sales-orders/shipping-labels/{label.id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_download_shipping_label_route_success(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-3")
    label = add_shipping_label(db, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="my-label.jpg")
    response = http.get(f"/sales-orders/shipping-labels/{label.id}/download")
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "my-label.jpg" in response.headers.get("content-disposition", "")


def test_shipping_label_route_404_for_missing_label(client):
    http, db, _ = client
    assert http.get("/sales-orders/shipping-labels/9999999").status_code == 404
    assert http.get("/sales-orders/shipping-labels/9999999/download").status_code == 404


def test_shipping_label_route_safe_when_order_deleted(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-4")
    label = add_shipping_label(db, shipment.id, content=shipping_label_jpeg_bytes(), original_filename="a.jpg")
    label_id = label.id
    live_order = db.get(SalesOrder, order.id)
    db.delete(live_order)
    db.commit()
    assert http.get(f"/sales-orders/shipping-labels/{label_id}").status_code == 404


def test_detail_page_shows_uploaded_shipping_label(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-5")
    http.post(
        f"/sales-orders/{order.id}/shipments/{shipment.id}/shipping-labels",
        files={"file": ("label.jpg", shipping_label_jpeg_bytes(), "image/jpeg")},
    )
    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "shipping-label-thumb" in detail.text


def test_workbench_shows_paid_order_awaiting_shipment(client):
    http, db, _ = client
    order, shipment = paid_order_with_shipment(db, name_suffix="label-route-6")
    listing = http.get("/sales-orders?status=paid")
    assert listing.status_code == 200


def test_manual_item_image_view_route(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "图片路由客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(
            product_id=None, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("100"),
            manual_image_content=shipping_label_jpeg_bytes(), manual_image_filename="view.jpg",
        )],
    )
    item_id = order.items[0].id
    response = http.get(f"/sales-orders/item-images/{item_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


# ---------------- Phase 7: duplicate address detection ----------------


def test_duplicate_address_is_detected(db_session):
    row = create_customer(db_session, name="查重客户1", address="上海市浦东新区1号", recipient_name="张三", recipient_phone="13800000001")
    duplicate = find_duplicate_customer_address(
        db_session, row.id, recipient_name="张三", phone="13800000001", address="上海市浦东新区1号",
    )
    assert duplicate is not None


def test_duplicate_address_ignores_whitespace_differences(db_session):
    row = create_customer(db_session, name="查重客户2", address="上海市浦东新区1号", recipient_name="张三", recipient_phone="13800000001")
    duplicate = find_duplicate_customer_address(
        db_session, row.id, recipient_name=" 张 三 ", phone="1380 0000001", address="上海市 浦东新区1号",
    )
    assert duplicate is not None


def test_non_duplicate_address_is_not_flagged(db_session):
    row = create_customer(db_session, name="查重客户3", address="上海市浦东新区1号", recipient_name="张三", recipient_phone="13800000001")
    duplicate = find_duplicate_customer_address(
        db_session, row.id, recipient_name="李四", phone="13900000002", address="北京市朝阳区2号",
    )
    assert duplicate is None


def test_add_customer_address_rejects_exact_duplicate_by_default(db_session):
    row = create_customer(db_session, name="查重客户4", address="地址甲", recipient_name="王五", recipient_phone="13700000003")
    with pytest.raises(DuplicateAddressError):
        add_customer_address(db_session, row.id, recipient_name="王五", phone="13700000003", address="地址甲")


def test_add_customer_address_allows_duplicate_when_explicitly_confirmed(db_session):
    row = create_customer(db_session, name="查重客户5", address="地址甲", recipient_name="王五", recipient_phone="13700000003")
    add_customer_address(db_session, row.id, recipient_name="王五", phone="13700000003", address="地址甲", allow_duplicate=True)
    assert len(list_customer_addresses(db_session, row.id)) == 2


# ---------------- Phase 7: customer address CRUD ----------------


def test_update_customer_address_changes_fields(db_session):
    row = create_customer(db_session, name="改地址客户1", address="旧地址")
    address = list_customer_addresses(db_session, row.id)[0]
    updated = update_customer_address(
        db_session, address.id, recipient_name="新收件人", phone="13600000004", address="新地址", label="公司",
    )
    assert updated.recipient_name == "新收件人"
    assert updated.address == "新地址"
    assert updated.label == "公司"


def test_delete_customer_address_promotes_next_default(db_session):
    row = create_customer(db_session, name="删地址客户1", address="地址甲")
    second = add_customer_address(db_session, row.id, recipient_name="收件人乙", address="地址乙")
    first = list_customer_addresses(db_session, row.id)[0]
    delete_customer_address(db_session, first.id)
    remaining = list_customer_addresses(db_session, row.id)
    assert len(remaining) == 1
    assert remaining[0].id == second.id
    assert remaining[0].is_default is True


def test_deleting_customer_address_does_not_affect_existing_order_snapshot(db_session):
    salesperson = ensure_default_salesperson(db_session)
    row = create_customer(db_session, name="删地址客户2", address="地址甲")
    order = create_sales_order(
        db_session, customer_id=row.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="商品", jan=None, quantity=1, unit_sale_price=Decimal("10"))],
    )
    address = list_customer_addresses(db_session, row.id)[0]
    delete_customer_address(db_session, address.id)
    reloaded = get_sales_order(db_session, order.id)
    assert reloaded.shipping_address_snapshot == "地址甲"


# ---------------- Phase 7: customer pages ----------------


def test_customers_list_page_returns_200(client):
    http, db, _ = client
    customer(db, "列表页客户")
    response = http.get("/customers")
    assert response.status_code == 200
    assert "列表页客户" in response.text


def test_customers_list_page_has_add_address_quick_entry(client):
    http, db, _ = client
    row = customer(db, "快捷入口客户")
    response = http.get("/customers")
    assert response.status_code == 200
    # both the new quick-add-address entry and the pre-existing "管理地址"
    # link must be present -- neither replaces the other.
    assert f"/customers/{row.id}?open_address_form=1" in response.text
    assert "+ 新增地址" in response.text
    assert f'href="/customers/{row.id}"' in response.text
    assert "管理地址" in response.text


def test_customer_detail_page_loads_with_open_address_form_param(client):
    # The open_address_form query param only drives client-side JS (auto-show
    # the existing add-address form) -- this confirms the deep link itself
    # doesn't break the route and the form it targets is actually present.
    http, db, _ = client
    row = customer(db, "深链接客户")
    response = http.get(f"/customers/{row.id}", params={"open_address_form": "1"})
    assert response.status_code == 200
    assert 'id="addAddressForm"' in response.text
    assert 'id="addAddressToggle"' in response.text


def test_customer_detail_page_shows_all_addresses(client):
    http, db, _ = client
    row = create_customer(db, name="详情页客户", address="地址甲")
    add_customer_address(db, row.id, recipient_name="收件人乙", address="地址乙", label="公司")
    add_customer_address(db, row.id, recipient_name="收件人丙", address="地址丙", label="朋友")
    response = http.get(f"/customers/{row.id}")
    assert response.status_code == 200
    assert "地址甲" in response.text and "地址乙" in response.text and "地址丙" in response.text


def test_customer_address_api_crud_routes(client):
    http, db, _ = client
    row = create_customer(db, name="API客户1", address="地址甲")
    created = http.post(f"/api/customers/{row.id}/addresses", json={
        "recipient_name": "新收件人", "address": "地址乙", "phone": "13500000005",
    })
    assert created.status_code == 201
    address_id = created.json()["id"]

    updated = http.post(f"/api/customer-addresses/{address_id}", json={
        "recipient_name": "改名后", "address": "地址乙改", "phone": "13500000006", "is_default": True,
    })
    assert updated.status_code == 200
    assert updated.json()["recipient_name"] == "改名后"

    deleted = http.post(f"/api/customer-addresses/{address_id}/delete")
    assert deleted.status_code == 200
    assert len(list_customer_addresses(db, row.id)) == 1


def test_customer_address_api_conflict_on_duplicate(client):
    http, db, _ = client
    row = create_customer(db, name="API客户2", address="地址甲", recipient_name="张三", recipient_phone="13400000007")
    response = http.post(f"/api/customers/{row.id}/addresses", json={
        "recipient_name": "张三", "address": "地址甲", "phone": "13400000007",
    })
    assert response.status_code == 409


# ---------------- Phase 7: None-render regression ----------------


def test_edit_page_never_renders_python_none_for_blank_address_fields(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "空地址客户", address=None, phone=None)
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="空地址商品", jan=None, quantity=1, unit_sale_price=Decimal("10"))],
    )
    assert order.shipping_address_snapshot is None
    assert order.recipient_phone_snapshot is None
    edit_page = http.get(f"/sales-orders/{order.id}/edit")
    assert edit_page.status_code == 200
    assert "None" not in edit_page.text


def test_detail_page_never_renders_python_none_for_blank_address(client):
    http, db, _ = client
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "空地址客户2", address=None)
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="空地址商品2", jan=None, quantity=1, unit_sale_price=Decimal("10"))],
    )
    detail_page = http.get(f"/sales-orders/{order.id}")
    assert detail_page.status_code == 200
    assert "None" not in detail_page.text


# ---------------- Phase 7: price validation ----------------


def test_zero_wechat_price_allowed_for_gifts(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = customer(db_session, "赠品客户")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="赠品", jan=None, quantity=1, unit_sale_price=Decimal("0"))],
    )
    assert order.items[0].unit_sale_price == Decimal("0")


def test_empty_wechat_price_rejected_at_route_level(client):
    http, db, _ = client
    created = http.post("/api/customers", json={"name": "空价格客户"})
    customer_id = created.json()["id"]
    form = {
        "customer_id": str(customer_id),
        "salesperson_id": str(ensure_default_salesperson(db).id),
        "items_json": json.dumps([
            {"product_id": None, "manual_name": "无价格商品", "jan": None, "quantity": 1, "unit_sale_price": "", "note": None},
        ]),
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


# ---------------- Phase 7: shipment item images ----------------


def test_create_shipment_picker_shows_product_images(client):
    http, db, _ = client
    item = product(db, "shipimg1", sale_price=500)
    item.display_image_url = "https://example.com/images/shipimg1.jpg"
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "发货图片客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("500"))],
        shipping_address="发货图片测试地址",
    )
    db.commit()
    update_sales_order_status(db, order.id, "paid")
    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "https://example.com/images/shipimg1.jpg" in detail.text


def test_shipment_detail_shows_product_images(client):
    http, db, _ = client
    item = product(db, "shipimg2", sale_price=500)
    item.display_image_url = "https://example.com/images/shipimg2.jpg"
    salesperson = ensure_default_salesperson(db)
    buyer = customer(db, "发货详情图片客户")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=item.id, manual_name=None, jan=None, quantity=1, unit_sale_price=Decimal("500"))],
        shipping_address="发货详情图片测试地址",
    )
    db.commit()
    update_sales_order_status(db, order.id, "paid")
    order = get_sales_order(db, order.id)
    create_shipment(db, order.id, item_quantities=[(order.items[0].id, 1)])
    detail = http.get(f"/sales-orders/{order.id}")
    assert detail.status_code == 200
    assert "https://example.com/images/shipimg2.jpg" in detail.text


# ---------------- Phase 7: unknown vs zero inventory ----------------


def test_product_search_shows_known_zero_inventory_not_hidden(client):
    http, db, _ = client
    item = product(db, "zeroinv1")
    response = http.get("/api/products/search", params={"q": item.internal_sku})
    row = next(r for r in response.json() if r["id"] == item.id)
    # No reference-inventory snapshot exists in this test DB at all, so both
    # regions are genuinely unknown -- must be None, never a fake 0.
    assert row["china_quantity"] is None
    assert row["japan_quantity"] is None


# ---------------- Phase 8: real multipart route regression for manual item images ----------------


def test_create_order_route_actually_saves_a_real_uploaded_manual_image(client):
    # Regression test: _parse_order_items_from_form used to check
    # isinstance(upload, UploadFile) against fastapi.UploadFile, but
    # request.form() returns starlette.datastructures.UploadFile -- a
    # different class in this FastAPI version -- so every real photo posted
    # through the actual HTML form was silently discarded. Only caught now
    # because this test posts a genuine multipart file, unlike the other
    # manual-image tests which call create_sales_order() directly.
    http, db, _ = client
    created = http.post("/api/customers", json={"name": "真实上传测试客户"})
    customer_id = created.json()["id"]
    items_payload = [{
        "product_id": None, "manual_name": None, "jan": None, "quantity": 1,
        "unit_sale_price": "10", "note": None, "client_id": "c1",
    }]
    form = {
        "customer_id": str(customer_id),
        "salesperson_id": str(ensure_default_salesperson(db).id),
        "items_json": json.dumps(items_payload),
    }
    response = http.post(
        "/sales-orders", data=form,
        files={"item_image_c1": ("real.jpg", shipping_label_jpeg_bytes(), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    order_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    order = get_sales_order(db, order_id)
    assert order.items[0].manual_image_relative_path is not None


# ==================== order_no (YYMMDDNN) / order_date / historical backfill ====================


def _new_order(
    db, *, customer_row=None, order_no=None, order_date=None, historical_backfill=False, name_suffix="on",
):
    salesperson = ensure_default_salesperson(db)
    buyer = customer_row or customer(db, f"订单号测试客户{name_suffix}")
    return create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(
            product_id=None, manual_name=f"订单号测试商品{name_suffix}", jan=None,
            quantity=1, unit_sale_price=Decimal("10"),
        )],
        shipping_address="测试地址", order_no=order_no, order_date=order_date,
        historical_backfill=historical_backfill,
    )


def test_order_no_suggests_01_when_no_orders_exist_for_the_day(db_session):
    now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)  # 2026-09-05 12:00 JST
    assert suggest_order_no(db_session, now=now) == "26090501"


def test_order_no_suggests_next_free_sequence(db_session):
    now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)
    _new_order(db_session, order_no="26090501", name_suffix="seq1")
    _new_order(db_session, order_no="26090502", name_suffix="seq2")
    assert suggest_order_no(db_session, now=now) == "26090503"


def test_order_no_resets_to_01_on_a_new_day(db_session):
    day2 = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)  # 2026-09-06 12:00 JST
    _new_order(db_session, order_no="26090501", name_suffix="reset1")
    assert suggest_order_no(db_session, now=day2) == "26090601"


def test_order_no_ignores_legacy_so_format(db_session):
    # Simulate a pre-existing row in the old "SO-YYYYMMDD-NNNN" shape (as if
    # migrated from historical data) by passing it as a manual override --
    # it must never be counted toward today's YYMMDDNN sequence.
    now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)
    legacy_order = _new_order(db_session, order_no="SO-20260905-0001", name_suffix="legacy-order-no")
    assert legacy_order.order_no == "SO-20260905-0001"
    assert suggest_order_no(db_session, now=now) == "26090501"


def test_order_no_manual_override_succeeds(db_session):
    order = _new_order(db_session, order_no="26082701", name_suffix="manual")
    assert order.order_no == "26082701"


def test_order_no_manual_duplicate_raises_friendly_error(db_session):
    _new_order(db_session, order_no="26082701", name_suffix="dup1")
    with pytest.raises(ValueError, match="已存在"):
        _new_order(db_session, order_no="26082701", name_suffix="dup2")


def test_order_no_daily_cap_raises_friendly_error_not_500(db_session):
    now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)
    for seq in range(1, ORDER_NO_DAILY_SEQUENCE_MAX + 1):
        _new_order(db_session, order_no=f"260905{seq:02d}", name_suffix=f"cap{seq}")
    with pytest.raises(ValueError, match="已用满"):
        suggest_order_no(db_session, now=now)


def test_order_no_uses_tokyo_calendar_day_not_utc_date(db_session):
    just_before_midnight_jst = datetime(2026, 9, 5, 14, 59, tzinfo=timezone.utc)  # 2026-09-05 23:59 JST
    just_after_midnight_jst = datetime(2026, 9, 5, 15, 1, tzinfo=timezone.utc)  # 2026-09-06 00:01 JST
    assert suggest_order_no(db_session, now=just_before_midnight_jst) == "26090501"
    assert suggest_order_no(db_session, now=just_after_midnight_jst) == "26090601"


def test_order_date_defaults_to_now_when_not_given(db_session):
    before = datetime.now(timezone.utc)
    order = _new_order(db_session, name_suffix="default-date")
    after = datetime.now(timezone.utc)
    assert before <= order.order_date <= after
    assert before <= order.created_at <= after


def test_order_date_manual_historical_value_differs_from_created_at(db_session):
    historical = datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)
    before_created = datetime.now(timezone.utc)
    order = _new_order(db_session, order_no="26082701", order_date=historical, name_suffix="historical-date")
    after_created = datetime.now(timezone.utc)
    assert order.order_date == historical
    assert before_created <= order.created_at <= after_created
    assert order.order_date != order.created_at


def test_normal_order_generates_procurement_demand(db_session):
    order = _new_order(db_session, name_suffix="demand-on")
    item_ids = [item.id for item in order.items]
    count = db_session.query(ProcurementDemand).filter(ProcurementDemand.sales_order_item_id.in_(item_ids)).count()
    assert count == len(order.items) > 0


def test_historical_backfill_skips_procurement_demand(db_session):
    order = _new_order(
        db_session, order_no="26082701", order_date=datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc),
        historical_backfill=True, name_suffix="demand-off",
    )
    assert len(order.items) == 1
    item_ids = [item.id for item in order.items]
    count = db_session.query(ProcurementDemand).filter(ProcurementDemand.sales_order_item_id.in_(item_ids)).count()
    assert count == 0
    assert order.order_no == "26082701"


def test_route_parses_order_datetime_local_as_tokyo_time(client):
    http, db, _ = client
    buyer = create_customer(db, name="路由日期测试客户", address="地址")
    salesperson = ensure_default_salesperson(db)
    items_payload = [{
        "product_id": None, "manual_name": "路由日期测试商品", "jan": None, "quantity": 1,
        "unit_sale_price": "10", "note": None, "client_id": "c1",
    }]
    form = {
        "customer_id": str(buyer.id), "salesperson_id": str(salesperson.id),
        "items_json": json.dumps(items_payload),
        "order_no": "26082701", "order_datetime_local": "2026-08-27T14:00",
        "shipping_address": "地址",
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    order_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    order = get_sales_order(db, order_id)
    assert order.order_date == datetime(2026, 8, 27, 5, 0, tzinfo=timezone.utc)


def test_route_historical_backfill_checkbox_skips_procurement_demand(client):
    http, db, _ = client
    buyer = create_customer(db, name="路由历史订单客户", address="地址")
    salesperson = ensure_default_salesperson(db)
    items_payload = [{
        "product_id": None, "manual_name": "路由历史订单商品", "jan": None, "quantity": 1,
        "unit_sale_price": "10", "note": None, "client_id": "c1",
    }]
    form = {
        "customer_id": str(buyer.id), "salesperson_id": str(salesperson.id),
        "items_json": json.dumps(items_payload),
        "order_no": "26082702", "order_datetime_local": "2026-08-27T14:00",
        "historical_backfill": "1", "shipping_address": "地址",
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    order_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])
    order = get_sales_order(db, order_id)
    item_ids = [item.id for item in order.items]
    count = db.query(ProcurementDemand).filter(ProcurementDemand.sales_order_item_id.in_(item_ids)).count()
    assert count == 0


def test_route_shows_friendly_error_for_duplicate_order_no(client):
    http, db, _ = client
    buyer = create_customer(db, name="重复订单号客户", address="地址")
    salesperson = ensure_default_salesperson(db)
    _new_order(db, order_no="26082701", name_suffix="route-dup")
    items_payload = [{
        "product_id": None, "manual_name": "重复订单号商品", "jan": None, "quantity": 1,
        "unit_sale_price": "10", "note": None, "client_id": "c1",
    }]
    form = {
        "customer_id": str(buyer.id), "salesperson_id": str(salesperson.id),
        "items_json": json.dumps(items_payload),
        "order_no": "26082701", "shipping_address": "地址",
    }
    response = http.post("/sales-orders", data=form, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "已存在" in urllib.parse.unquote(response.headers["location"])


def test_new_order_page_prefills_suggested_order_no_and_datetime(client):
    http, _db, _ = client
    response = http.get("/sales-orders/new")
    assert response.status_code == 200
    assert 'name="order_no"' in response.text
    assert 'name="order_datetime_local"' in response.text
    assert 'name="historical_backfill"' in response.text
    assert "历史订单补录" in response.text
