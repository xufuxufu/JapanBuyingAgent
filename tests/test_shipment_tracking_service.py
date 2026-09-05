from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from app.kuaidi100_tracking_client import Kuaidi100ClientError
from app.models import SalesShipment, ShipmentTrackingEvent
from app.sales_order_service import (
    SalesOrderItemInput, add_shipping_label, create_customer, create_sales_order, create_shipment,
    ensure_default_salesperson, mark_shipment_shipped, update_sales_order_status,
)
from app.shipment_tracking_service import (
    query_shipment_tracking, run_due_shipment_tracking_cycle, select_due_shipment_ids,
    tracking_eligibility_error,
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


@pytest.fixture(autouse=True)
def _fake_kuaidi100_credentials(monkeypatch):
    # Real API credentials must never be used in pytest -- this is a fake
    # test-only pair so config.configured is True and the client module
    # builds/signs a request, which then goes to a FakeHTTPClient, never
    # a real network call.
    monkeypatch.setenv("KUAIDI100_KEY", "test-key")
    monkeypatch.setenv("KUAIDI100_CUSTOMER", "test-customer")


def _label_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 90)).save(buffer, "JPEG")
    return buffer.getvalue()


def shipped_shipment(
    db, *, tracking_no: str | None = "70000000000001", carrier: str = "中通",
    recipient_phone: str | None = "13800000001", customer_phone: str | None = "13800000000",
) -> SalesShipment:
    salesperson = ensure_default_salesperson(db)
    buyer = create_customer(db, name="物流测试客户", phone=customer_phone, wechat_name="wx_tk")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="物流测试商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        shipping_address="测试收货地址", recipient_phone=recipient_phone,
    )
    update_sales_order_status(db, order.id, "paid")
    shipment = create_shipment(
        db, order.id, item_quantities=[(order.items[0].id, 1)],
        carrier=carrier, tracking_no=tracking_no, recipient_phone=recipient_phone,
    )
    add_shipping_label(db, shipment.id, content=_label_bytes(), original_filename="label.jpg")
    mark_shipment_shipped(db, shipment.id)
    return db.get(SalesShipment, shipment.id)


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHTTPClient:
    def __init__(self, payload: dict | Exception):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if isinstance(self.payload, Exception):
            raise self.payload
        return FakeResponse(self.payload)


def delivered_payload(tracking_no: str) -> dict:
    return {
        "message": "ok", "nu": tracking_no, "ischeck": "1", "com": "zhongtong", "status": "200",
        "state": "3",
        "data": [
            {"time": "2026-08-28 20:53:03", "context": "已签收", "areaCode": "CN420100000000", "areaName": "湖北,武汉市", "status": "签收"},
            {"time": "2026-08-27 09:20:55", "context": "已揽收", "areaCode": "CN420100000000", "areaName": "湖北,武汉市", "status": "揽收"},
        ],
    }


def in_transit_payload(tracking_no: str) -> dict:
    return {
        "message": "ok", "nu": tracking_no, "ischeck": "0", "com": "zhongtong", "status": "200",
        "state": "0",
        "data": [
            {"time": "2026-08-27 09:20:55", "context": "运输中", "areaCode": "CN420100000000", "areaName": "湖北,武汉市", "status": "在途"},
        ],
    }


def error_payload() -> dict:
    return {"message": "单号不存在", "status": "500", "result": False}


# ---------------- eligibility / missing-input guards ----------------


def test_missing_tracking_no_blocks_query(db_session):
    shipment = shipped_shipment(db_session, tracking_no=None)
    assert tracking_eligibility_error(shipment) == "缺少运单号"
    with pytest.raises(ValueError):
        query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(delivered_payload("x")))


def test_missing_phone_blocks_query(db_session):
    shipment = shipped_shipment(db_session, recipient_phone=None, customer_phone=None)
    assert shipment.recipient_phone_snapshot is None
    assert tracking_eligibility_error(shipment) == "缺少收件人手机号"
    with pytest.raises(ValueError):
        query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(delivered_payload("x")))


def test_non_zhongtong_carrier_blocks_query(db_session):
    shipment = shipped_shipment(db_session, carrier="圆通")
    assert tracking_eligibility_error(shipment) == "本期仅支持中通"


def test_pending_shipment_blocks_query(db_session):
    salesperson = ensure_default_salesperson(db_session)
    buyer = create_customer(db_session, name="待发货客户", phone="13800000002", wechat_name="wx2")
    order = create_sales_order(
        db_session, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="待发货商品", jan=None, quantity=1, unit_sale_price=Decimal("50"))],
        shipping_address="地址", recipient_phone="13800000002",
    )
    update_sales_order_status(db_session, order.id, "paid")
    shipment = create_shipment(db_session, order.id, item_quantities=[(order.items[0].id, 1)], tracking_no="70000000000001")
    assert tracking_eligibility_error(shipment) == "发货单尚未发货"


# ---------------- successful query: parsing, terminal, dedupe ----------------


def test_delivered_response_sets_terminal_and_status(db_session):
    shipment = shipped_shipment(db_session)
    fake = FakeHTTPClient(delivered_payload(shipment.tracking_no))
    updated = query_shipment_tracking(db_session, shipment.id, client=fake)
    assert updated.tracking_status == "delivered"
    assert updated.tracking_terminal is True
    assert updated.tracking_error is None
    assert updated.tracking_next_check_at is None
    assert len(fake.calls) == 1


def test_in_transit_response_is_not_terminal(db_session):
    shipment = shipped_shipment(db_session)
    updated = query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)))
    assert updated.tracking_status == "in_transit"
    assert updated.tracking_terminal is False
    assert updated.tracking_next_check_at is not None


def test_events_are_deduped_across_repeated_queries(db_session):
    shipment = shipped_shipment(db_session)
    payload = in_transit_payload(shipment.tracking_no)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(payload), now=now)
    # second query far enough past the throttle window, same events returned again
    later = now + timedelta(hours=3)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(payload), now=later)
    events = list(db_session.scalars(select(ShipmentTrackingEvent).where(ShipmentTrackingEvent.shipment_id == shipment.id)))
    assert len(events) == 1


# ---------------- API failure preserves existing state ----------------


def test_api_error_records_error_without_raising(db_session):
    shipment = shipped_shipment(db_session)
    updated = query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(error_payload()))
    assert updated.tracking_error is not None
    assert "单号不存在" in updated.tracking_error
    assert updated.tracking_status is None
    assert updated.tracking_terminal is False


def test_api_failure_does_not_erase_existing_events(db_session):
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now)
    before_count = db_session.scalar(select(ShipmentTrackingEvent).where(ShipmentTrackingEvent.shipment_id == shipment.id).limit(1))
    assert before_count is not None
    later = now + timedelta(hours=3)
    updated = query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(error_payload()), now=later)
    assert updated.tracking_error is not None
    events = list(db_session.scalars(select(ShipmentTrackingEvent).where(ShipmentTrackingEvent.shipment_id == shipment.id)))
    assert len(events) == 1  # untouched by the failed query


# ---------------- 60s throttle ----------------


def test_throttle_blocks_immediate_repeat_query(db_session):
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now)
    with pytest.raises(ValueError, match="过于频繁"):
        query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now + timedelta(seconds=10))


def test_throttle_check_handles_naive_datetime_after_fresh_read(db_session):
    """Regression for a real 500 found during Phase 10A production
    acceptance testing: SQLite round-trips DateTime(timezone=True) columns as
    tzinfo-naive once an object is re-read from the DB (every real HTTP
    request gets a fresh session, so this always happens in production, even
    though this test file's shared db_session fixture normally masks it via
    expire_on_commit=False). The throttle's `now - tracking_last_checked_at`
    subtraction must not raise TypeError('can't subtract offset-naive and
    offset-aware datetimes') for a naive value read back this way."""
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now)
    db_session.expire_all()  # force the next attribute access to re-read from SQLite, losing tzinfo
    with pytest.raises(ValueError, match="过于频繁"):
        query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now + timedelta(seconds=10))


def test_throttle_allows_query_after_60_seconds(db_session):
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now)
    updated = query_shipment_tracking(
        db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)),
        now=now + timedelta(seconds=61),
    )
    assert updated.tracking_last_checked_at == now + timedelta(seconds=61)


# ---------------- due-cycle selection ----------------


def test_terminal_shipment_excluded_from_due_query(db_session):
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(delivered_payload(shipment.tracking_no)), now=now)
    due = select_due_shipment_ids(db_session, now=now + timedelta(days=30))
    assert shipment.id not in due


def test_non_terminal_shipment_becomes_due_after_interval(db_session):
    shipment = shipped_shipment(db_session)
    now = datetime.now(timezone.utc)
    query_shipment_tracking(db_session, shipment.id, client=FakeHTTPClient(in_transit_payload(shipment.tracking_no)), now=now)
    assert shipment.id not in select_due_shipment_ids(db_session, now=now + timedelta(minutes=1))
    assert shipment.id in select_due_shipment_ids(db_session, now=now + timedelta(hours=3))


def test_shipment_never_checked_is_due_immediately(db_session):
    shipment = shipped_shipment(db_session)
    assert shipment.id in select_due_shipment_ids(db_session, now=datetime.now(timezone.utc))


def test_missing_tracking_no_never_becomes_due(db_session):
    shipment = shipped_shipment(db_session, tracking_no=None)
    assert shipment.id not in select_due_shipment_ids(db_session, now=datetime.now(timezone.utc) + timedelta(days=1))


# ---------------- batch cycle isolates per-shipment failures ----------------


def test_due_cycle_counts_success_and_continues_past_one_failure(db_session, monkeypatch):
    good = shipped_shipment(db_session, tracking_no="70000000000001")
    bad = shipped_shipment(db_session, tracking_no="70000000000002")

    def fake_query(session, shipment_id, *, client=None, now=None):
        if shipment_id == bad.id:
            raise RuntimeError("boom")
        shipment = session.get(SalesShipment, shipment_id)
        shipment.tracking_terminal = True
        session.commit()
        return shipment

    import app.shipment_tracking_service as mod
    monkeypatch.setattr(mod, "query_shipment_tracking", fake_query)
    success, failure = run_due_shipment_tracking_cycle(db_session)
    assert success == 1
    assert failure == 1
