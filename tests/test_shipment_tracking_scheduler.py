from __future__ import annotations

import asyncio
import io
from decimal import Decimal

import pytest
from PIL import Image

from app.models import SalesShipment
from app.sales_order_service import (
    SalesOrderItemInput, add_shipping_label, create_customer, create_sales_order, create_shipment,
    ensure_default_salesperson, mark_shipment_shipped, update_sales_order_status,
)
import app.shipment_tracking_scheduler as scheduler_module
from app.shipment_tracking_scheduler import (
    ShipmentTrackingSchedulerSettings,
    get_shipment_tracking_scheduler_settings,
    last_cycle_at,
    scheduler_running,
    start_shipment_tracking_scheduler,
    stop_shipment_tracking_scheduler,
)
from app.shipment_tracking_service import (
    _cycle_lock,
    run_due_shipment_tracking_cycle_standalone,
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
def _reset_scheduler_state():
    # These are module-level globals (matching monitor_scheduler.py's own
    # pattern) -- reset around every test so one test's task/timestamp can
    # never leak into the next.
    scheduler_module._scheduler_task = None
    scheduler_module._last_cycle_at = None
    yield
    scheduler_module._scheduler_task = None
    scheduler_module._last_cycle_at = None


def _label_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 90)).save(buffer, "JPEG")
    return buffer.getvalue()


def shipped_shipment(db, *, tracking_no: str | None = "70000000000001", phone: str = "13800000001") -> SalesShipment:
    salesperson = ensure_default_salesperson(db)
    buyer = create_customer(db, name="调度测试客户", phone="13800000000", wechat_name="wx_sched")
    order = create_sales_order(
        db, customer_id=buyer.id, salesperson_id=salesperson.id,
        items=[SalesOrderItemInput(product_id=None, manual_name="调度测试商品", jan=None, quantity=1, unit_sale_price=Decimal("100"))],
        shipping_address="测试收货地址", recipient_phone=phone,
    )
    update_sales_order_status(db, order.id, "paid")
    shipment = create_shipment(
        db, order.id, item_quantities=[(order.items[0].id, 1)],
        carrier="中通", tracking_no=tracking_no, recipient_phone=phone,
    )
    add_shipping_label(db, shipment.id, content=_label_bytes(), original_filename="label.jpg")
    mark_shipment_shipped(db, shipment.id)
    return db.get(SalesShipment, shipment.id)


# ---------------- settings parsing ----------------


def test_settings_default_to_disabled(monkeypatch):
    monkeypatch.delenv("JBA_SHIPMENT_TRACKING_AUTO_ENABLED", raising=False)
    monkeypatch.delenv("JBA_SHIPMENT_TRACKING_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("JBA_SHIPMENT_TRACKING_MAX_ITEMS", raising=False)

    settings = get_shipment_tracking_scheduler_settings()

    assert settings.enabled is False
    assert settings.interval_seconds == 10 * 60
    assert settings.max_items_per_cycle == 5


def test_settings_read_custom_env_values(monkeypatch):
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_AUTO_ENABLED", "true")
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_INTERVAL_MINUTES", "15")
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_MAX_ITEMS", "3")

    settings = get_shipment_tracking_scheduler_settings()

    assert settings.enabled is True
    assert settings.interval_seconds == 15 * 60
    assert settings.max_items_per_cycle == 3


# ---------------- start/stop lifecycle ----------------


def test_start_returns_false_when_disabled(monkeypatch):
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_AUTO_ENABLED", "false")

    started = start_shipment_tracking_scheduler()

    assert started is False
    assert scheduler_running() is False


def test_start_stop_lifecycle_when_enabled(monkeypatch):
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_AUTO_ENABLED", "true")
    monkeypatch.setenv("JBA_SHIPMENT_TRACKING_INTERVAL_MINUTES", "1440")  # never actually ticks in this test

    async def scenario():
        started = start_shipment_tracking_scheduler()
        assert started is True
        assert scheduler_running() is True
        # calling start() again while already running is a no-op, not a second task
        assert start_shipment_tracking_scheduler() is False
        await stop_shipment_tracking_scheduler()
        assert scheduler_running() is False

    asyncio.run(scenario())


# ---------------- loop behavior (fast, no real sleep durations) ----------------


def test_loop_invokes_cycle_with_configured_limit_and_records_last_cycle_at(monkeypatch):
    calls: list[int | None] = []

    def fake_cycle(limit=None):
        calls.append(limit)
        return (0, 0)

    monkeypatch.setattr(scheduler_module, "run_due_shipment_tracking_cycle_standalone", fake_cycle)
    monkeypatch.setattr(
        scheduler_module, "get_shipment_tracking_scheduler_settings",
        lambda: ShipmentTrackingSchedulerSettings(enabled=True, interval_seconds=0, max_items_per_cycle=5),
    )

    async def scenario():
        task = asyncio.create_task(scheduler_module._scheduler_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert calls, "the loop must have invoked the cycle at least once"
    assert calls[0] == 5
    assert last_cycle_at() is not None


def test_loop_skips_cycle_entirely_when_disabled(monkeypatch):
    calls: list[int | None] = []
    monkeypatch.setattr(scheduler_module, "run_due_shipment_tracking_cycle_standalone", lambda limit=None: calls.append(limit) or (0, 0))
    monkeypatch.setattr(
        scheduler_module, "get_shipment_tracking_scheduler_settings",
        lambda: ShipmentTrackingSchedulerSettings(enabled=False, interval_seconds=0, max_items_per_cycle=5),
    )

    async def scenario():
        task = asyncio.create_task(scheduler_module._scheduler_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert calls == []
    assert last_cycle_at() is None


def test_loop_survives_a_failing_cycle_and_keeps_ticking(monkeypatch):
    calls: list[int] = []

    def flaky_cycle(limit=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return (0, 0)

    monkeypatch.setattr(scheduler_module, "run_due_shipment_tracking_cycle_standalone", flaky_cycle)
    monkeypatch.setattr(
        scheduler_module, "get_shipment_tracking_scheduler_settings",
        lambda: ShipmentTrackingSchedulerSettings(enabled=True, interval_seconds=0, max_items_per_cycle=5),
    )

    async def scenario():
        task = asyncio.create_task(scheduler_module._scheduler_loop())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(calls) >= 2, "one failing tick must not stop the loop from ticking again"


# ---------------- the standalone entry point the scheduler calls ----------------


def test_batch_cycle_respects_the_limit_argument_the_scheduler_passes_through(db_session):
    # run_due_shipment_tracking_cycle_standalone() always opens its own
    # session (SessionLocal), so it can't be pointed at db_session directly
    # in a test -- this exercises the same run_due_shipment_tracking_cycle()
    # it calls internally, proving a `limit` argument narrower than the
    # number of due shipments is honored (the "单cycle最大5个" quota guard
    # the scheduler relies on, via ShipmentTrackingSchedulerSettings.max_items_per_cycle).
    from app.shipment_tracking_service import run_due_shipment_tracking_cycle
    for i in range(3):
        shipped_shipment(db_session, tracking_no=f"7000000000000{i}")

    success, failure = run_due_shipment_tracking_cycle(db_session, limit=2)

    assert success + failure == 2


def test_standalone_entry_point_is_reentrancy_locked():
    acquired = _cycle_lock.acquire(blocking=False)
    assert acquired, "test setup expects the lock to be free"
    try:
        result = run_due_shipment_tracking_cycle_standalone()
        assert result == (0, 0), "a concurrent call must back off instead of racing the in-progress cycle"
    finally:
        _cycle_lock.release()


def test_cycle_never_touches_sales_order_or_shipment_business_status(db_session, monkeypatch):
    shipment = shipped_shipment(db_session)
    order_id = shipment.sales_order_id
    original_shipment_status = shipment.status
    original_order_status = shipment.sales_order.status

    def fake_query(session, shipment_id, *, client=None, now=None):
        row = session.get(SalesShipment, shipment_id)
        row.tracking_status = "delivered"
        row.tracking_terminal = True
        row.tracking_next_check_at = None
        session.commit()
        return row

    import app.shipment_tracking_service as service_module
    monkeypatch.setattr(service_module, "query_shipment_tracking", fake_query)

    success, failure = service_module.run_due_shipment_tracking_cycle(db_session)

    assert success == 1 and failure == 0
    db_session.refresh(shipment)
    assert shipment.status == original_shipment_status == "shipped"
    assert shipment.sales_order.status == original_order_status
    assert shipment.tracking_terminal is True
