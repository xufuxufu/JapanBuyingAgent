from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import Engine

from app.config import env_bool, env_int, is_testing
from app.field_purchase import process_pending_jobs
from app.product_image_localization import JOB_TYPE


@dataclass(frozen=True, slots=True)
class WorkerRun:
    processed: int
    batches: int


_stop_event = threading.Event()
_wake_event = threading.Event()
_thread: threading.Thread | None = None


def run_image_localization_worker(
    engine: Engine,
    *,
    once: bool = False,
    stop_event: threading.Event | None = None,
) -> WorkerRun:
    if is_testing():
        return WorkerRun(0, 0)
    stop = stop_event or _stop_event
    batch_size = env_int("JBA_QINSI_IMAGE_BATCH_SIZE", 20, 1, 100)
    concurrency = env_int("JBA_QINSI_IMAGE_CONCURRENCY", 2, 1, 4)
    poll_seconds = env_int("JBA_QINSI_IMAGE_POLL_SECONDS", 5, 1, 60)
    processed = 0
    batches = 0
    while not stop.is_set():
        count = process_pending_jobs(
            engine,
            batch_size,
            job_type=JOB_TYPE,
            concurrency=concurrency,
        )
        if count:
            processed += count
            batches += 1
            continue
        if once:
            break
        _wake_event.wait(poll_seconds)
        _wake_event.clear()
    return WorkerRun(processed, batches)


def start_image_localization_worker(engine: Engine) -> bool:
    global _thread
    if is_testing() or not env_bool("JBA_QINSI_IMAGE_WORKER_ENABLED", True):
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    _wake_event.clear()
    _thread = threading.Thread(
        target=run_image_localization_worker,
        args=(engine,),
        name="jba-image-localization",
        daemon=True,
    )
    _thread.start()
    return True


def wake_image_localization_worker() -> None:
    _wake_event.set()


def stop_image_localization_worker() -> None:
    global _thread
    _stop_event.set()
    _wake_event.set()
    if _thread is not None:
        _thread.join(timeout=3)
    _thread = None
