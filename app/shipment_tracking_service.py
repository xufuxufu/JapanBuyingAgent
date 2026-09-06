"""Domestic (中通-only) shipment tracking -- Phase 10A/10B.

Owns the "立即查询" flow and the due-shipment batch cycle. Mirrors the
existing price-monitor pattern (app/monitor_service.py: next_check_at column
+ due query + non-blocking cycle lock) rather than introducing a new
scheduling mechanism. Phase 10B (app/shipment_tracking_scheduler.py) wires
run_due_shipment_tracking_cycle_standalone() into an optional background
loop, gated behind JBA_SHIPMENT_TRACKING_AUTO_ENABLED -- nothing in this
module changed to support that; it only needed a `limit` kwarg.

Business dispatch status (SalesShipment.status: pending/shipped) and carrier
tracking status (tracking_status/tracking_terminal) are intentionally kept
separate -- a delivered/签收 tracking result never auto-completes the
SalesOrder.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal
from app.kuaidi100_tracking_client import (
    Kuaidi100ClientError,
    event_hash,
    query_zhongtong_tracking,
)
from app.models import SalesOrder, SalesShipment, ShipmentTrackingEvent

TRACKING_THROTTLE_SECONDS = 60
DUE_POLL_INTERVAL = timedelta(hours=2)
DEFAULT_MAX_ITEMS_PER_CYCLE = 20
TRACKED_CARRIER = "中通"

_cycle_lock = threading.Lock()


def tracking_eligibility_error(shipment: SalesShipment) -> str | None:
    """Returns a Chinese reason string if this shipment cannot be queried
    right now, else None. Never guesses a phone number or tracking number."""
    if shipment.status != "shipped":
        return "发货单尚未发货"
    if (shipment.carrier or "").strip() != TRACKED_CARRIER:
        return "本期仅支持中通"
    if not (shipment.tracking_no or "").strip():
        return "缺少运单号"
    if not _resolve_phone(shipment):
        return "缺少收件人手机号"
    return None


def _resolve_phone(shipment: SalesShipment) -> str | None:
    # Prefer the shipment's own frozen snapshot (the exact phone number this
    # parcel shipped under); fall back to the order's current snapshot only
    # if the shipment never captured one of its own. Never guessed.
    phone = (shipment.recipient_phone_snapshot or "").strip()
    if phone:
        return phone
    order_phone = (shipment.sales_order.recipient_phone_snapshot or "").strip() if shipment.sales_order else ""
    return order_phone or None


def query_shipment_tracking(
    session: Session, shipment_id: int, *, client=None, now: datetime | None = None,
) -> SalesShipment:
    """Query Kuaidi100 for one shipment's latest ZTO tracking and persist it.

    Failure (API error, network, missing config) records tracking_error and
    updates tracking_last_checked_at, but never touches previously-saved
    tracking_status/tracking_terminal/events -- a transient failure must not
    erase history that's already known to be true.
    """
    now = now or datetime.now(timezone.utc)
    shipment = session.get(SalesShipment, shipment_id)
    if shipment is None:
        raise LookupError("发货单不存在")

    reason = tracking_eligibility_error(shipment)
    if reason:
        raise ValueError(reason)

    if shipment.tracking_last_checked_at is not None:
        # SQLite round-trips DateTime(timezone=True) as naive -- a value
        # read back in a later request has lost its tzinfo even though it
        # was written as UTC-aware.
        last_checked_at = shipment.tracking_last_checked_at
        if last_checked_at.tzinfo is None:
            last_checked_at = last_checked_at.replace(tzinfo=timezone.utc)
        elapsed = (now - last_checked_at).total_seconds()
        if elapsed < TRACKING_THROTTLE_SECONDS:
            wait = int(TRACKING_THROTTLE_SECONDS - elapsed) + 1
            raise ValueError(f"查询过于频繁，请{wait}秒后再试")

    phone = _resolve_phone(shipment)

    try:
        result = query_zhongtong_tracking(shipment.tracking_no, phone, client=client)
    except Kuaidi100ClientError as exc:
        shipment.tracking_error = exc.message[:500]
        shipment.tracking_last_checked_at = now
        shipment.tracking_next_check_at = now + DUE_POLL_INTERVAL
        session.commit()
        return shipment

    shipment.tracking_status = result.tracking_status
    shipment.tracking_terminal = result.terminal
    shipment.tracking_last_checked_at = now
    shipment.tracking_error = None

    if result.events:
        existing_hashes = set(session.scalars(
            select(ShipmentTrackingEvent.event_hash).where(ShipmentTrackingEvent.shipment_id == shipment.id)
        ))
        for ev in result.events:
            h = event_hash(ev.event_time, ev.status, ev.description)
            if h in existing_hashes:
                continue
            session.add(ShipmentTrackingEvent(
                shipment_id=shipment.id, event_time=ev.event_time, description=ev.description,
                area_code=ev.area_code, area_name=ev.area_name, status=ev.status, event_hash=h,
            ))
            existing_hashes.add(h)
        shipment.tracking_last_event_at = max(ev.event_time for ev in result.events)

    shipment.tracking_next_check_at = None if result.terminal else now + DUE_POLL_INTERVAL
    session.commit()
    return shipment


def select_due_shipment_ids(
    session: Session, *, now: datetime | None = None, limit: int | None = None,
) -> list[int]:
    now = now or datetime.now(timezone.utc)
    return list(session.scalars(
        select(SalesShipment.id)
        .where(
            SalesShipment.status == "shipped",
            SalesShipment.carrier == TRACKED_CARRIER,
            SalesShipment.tracking_no.is_not(None),
            SalesShipment.tracking_no != "",
            SalesShipment.tracking_terminal.is_(False),
            or_(SalesShipment.tracking_next_check_at.is_(None), SalesShipment.tracking_next_check_at <= now),
        )
        .order_by(SalesShipment.tracking_next_check_at.is_(None).desc(), SalesShipment.tracking_next_check_at)
        .limit(limit or DEFAULT_MAX_ITEMS_PER_CYCLE)
    ))


def run_due_shipment_tracking_cycle(
    session: Session, *, now: datetime | None = None, limit: int | None = None,
) -> tuple[int, int]:
    """One batch pass over due shipments. Per-shipment failures are isolated
    (rolled back individually) so one bad shipment never aborts the rest."""
    now = now or datetime.now(timezone.utc)
    due_ids = select_due_shipment_ids(session, now=now, limit=limit)
    success = failure = 0
    for shipment_id in due_ids:
        try:
            query_shipment_tracking(session, shipment_id, now=now)
            success += 1
        except Exception:
            session.rollback()
            failure += 1
    return success, failure


def run_due_shipment_tracking_cycle_standalone(limit: int | None = None) -> tuple[int, int]:
    """Manually-triggerable entry point using its own session. Also the one
    entry point app/shipment_tracking_scheduler.py calls from its background
    loop (Phase 10B) -- the non-blocking _cycle_lock below is what makes a
    scheduler tick and a human's manual trigger safe to overlap without
    double-querying the same shipment."""
    if not _cycle_lock.acquire(blocking=False):
        return 0, 0
    try:
        with SessionLocal() as session:
            return run_due_shipment_tracking_cycle(session, limit=limit)
    finally:
        _cycle_lock.release()


def list_domestic_shipments(session: Session, *, status_filter: str | None = None) -> list[SalesShipment]:
    """The 国内物流 list page's data source: every shipped 中通 shipment,
    regardless of whether it has a tracking number yet (so a shipment still
    needing one is visible too), optionally filtered to one tracking_status."""
    conditions = [SalesShipment.status == "shipped", SalesShipment.carrier == TRACKED_CARRIER]
    if status_filter and status_filter != "all":
        conditions.append(SalesShipment.tracking_status == status_filter)
    return list(session.scalars(
        select(SalesShipment)
        .where(*conditions)
        .options(
            selectinload(SalesShipment.sales_order).selectinload(SalesOrder.customer),
            selectinload(SalesShipment.tracking_events),
        )
        .order_by(SalesShipment.shipped_at.desc())
    ))


def tracking_status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(SalesShipment.tracking_status, func.count())
        .where(SalesShipment.status == "shipped", SalesShipment.carrier == TRACKED_CARRIER)
        .group_by(SalesShipment.tracking_status)
    ).all()
    return {(status or "no_info"): count for status, count in rows}
