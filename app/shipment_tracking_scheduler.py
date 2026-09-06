"""Optional background loop for Phase 10A's domestic shipment tracking --
Phase 10B. Mirrors app/monitor_scheduler.py's shape exactly (asyncio task,
sleep-then-check loop, re-check the enable flag every iteration, exceptions
logged and swallowed so one bad cycle never kills the loop) rather than
introducing a new scheduling mechanism, process, or dependency.

The per-shipment "how often to re-check" cadence (~2 hours, non-terminal
only) already lives in shipment_tracking_service.DUE_POLL_INTERVAL and is
untouched here. JBA_SHIPMENT_TRACKING_INTERVAL_MINUTES below controls a
different, smaller number: how often this loop wakes up to ask "which
shipments are due right now" -- most ticks will find nothing to do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import env_bool, env_int
from app.shipment_tracking_service import run_due_shipment_tracking_cycle_standalone

logger = logging.getLogger(__name__)
_scheduler_task: asyncio.Task | None = None
_last_cycle_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ShipmentTrackingSchedulerSettings:
    enabled: bool
    interval_seconds: int
    max_items_per_cycle: int


def get_shipment_tracking_scheduler_settings() -> ShipmentTrackingSchedulerSettings:
    return ShipmentTrackingSchedulerSettings(
        enabled=env_bool("JBA_SHIPMENT_TRACKING_AUTO_ENABLED", False),
        interval_seconds=env_int("JBA_SHIPMENT_TRACKING_INTERVAL_MINUTES", 10, 1, 1440) * 60,
        # Conservative default: Kuaidi100 quota is limited, and this cycle
        # runs unattended -- a human doing "立即查询" one at a time is not
        # bound by this (see shipment_tracking_service.DEFAULT_MAX_ITEMS_PER_CYCLE).
        max_items_per_cycle=env_int("JBA_SHIPMENT_TRACKING_MAX_ITEMS", 5, 1, 50),
    )


async def _scheduler_loop() -> None:
    global _last_cycle_at
    while True:
        settings = get_shipment_tracking_scheduler_settings()
        await asyncio.sleep(settings.interval_seconds)
        if not settings.enabled:
            continue
        try:
            await asyncio.to_thread(
                run_due_shipment_tracking_cycle_standalone, settings.max_items_per_cycle,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("shipment tracking cycle failed: %s", type(exc).__name__)
        finally:
            _last_cycle_at = datetime.now(timezone.utc)


def start_shipment_tracking_scheduler() -> bool:
    global _scheduler_task
    if not get_shipment_tracking_scheduler_settings().enabled:
        return False
    if _scheduler_task is not None and not _scheduler_task.done():
        return False
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name="jba-shipment-tracking")
    return True


async def stop_shipment_tracking_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    try:
        await _scheduler_task
    except asyncio.CancelledError:
        pass
    _scheduler_task = None


def scheduler_running() -> bool:
    return _scheduler_task is not None and not _scheduler_task.done()


def last_cycle_at() -> datetime | None:
    return _last_cycle_at
