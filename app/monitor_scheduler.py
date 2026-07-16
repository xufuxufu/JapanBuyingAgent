from __future__ import annotations

import asyncio
import logging

from app.monitor_service import get_monitor_settings, run_due_monitor_cycle


logger = logging.getLogger(__name__)
_scheduler_task: asyncio.Task | None = None


async def _scheduler_loop() -> None:
    while True:
        settings = get_monitor_settings()
        await asyncio.sleep(settings.scan_interval_seconds)
        if not settings.enabled:
            continue
        try:
            await asyncio.to_thread(run_due_monitor_cycle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("price monitor cycle failed: %s", type(exc).__name__)


def start_monitor_scheduler() -> bool:
    global _scheduler_task
    if not get_monitor_settings().enabled:
        return False
    if _scheduler_task is not None and not _scheduler_task.done():
        return False
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name="jba-price-monitor")
    return True


async def stop_monitor_scheduler() -> None:
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
