from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal
from app.models import (
    MonitorSchedulerState,
    Product,
    ProductWatchConfig,
    ProductWatchNotification,
    ProductWatchSnapshot,
)
from app.price_providers import PriceProvider
from app.price_service import PriceLookupView, query_prices
from app.schemas import PriceLookupInput
from app.watch_service import FREQUENCY_HOURS


NOTIFICATION_TYPE_LABELS = {
    "target_reached": "达到目标价",
    "new_historical_low": "线上历史新低",
    "restocked": "重新上架",
    "monitor_failed": "监控连续失败",
}
SUCCESS_ATTEMPT_STATUSES = {"success", "empty"}
_cycle_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    enabled: bool
    scan_interval_seconds: int
    max_items_per_cycle: int
    max_consecutive_failures: int
    request_timeout_seconds: float
    notification_dedupe_seconds: int


@dataclass(frozen=True, slots=True)
class MonitorCheckResult:
    watch_config_id: int
    product_id: int
    status: str
    snapshot_id: int | None
    notification_count: int = 0
    error_summary: str | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def get_monitor_settings() -> MonitorSettings:
    return MonitorSettings(
        enabled=_env_bool("JBA_PRICE_MONITOR_ENABLED", False),
        scan_interval_seconds=_env_int("JBA_MONITOR_SCAN_INTERVAL_SECONDS", 3600, 3600, 86400),
        max_items_per_cycle=_env_int("JBA_MONITOR_MAX_ITEMS", 10, 1, 100),
        max_consecutive_failures=_env_int("JBA_MONITOR_MAX_FAILURES", 3, 1, 20),
        request_timeout_seconds=_env_float("JBA_MONITOR_REQUEST_TIMEOUT_SECONDS", 4.0, 1.0, 30.0),
        notification_dedupe_seconds=_env_int("JBA_MONITOR_NOTIFICATION_DEDUPE_SECONDS", 86400, 60, 604800),
    )


def calculate_next_check_at(
    frequency_tier: str,
    checked_at: datetime,
    *,
    consecutive_failures: int = 0,
) -> datetime:
    if frequency_tier not in FREQUENCY_HOURS:
        raise ValueError("监控频率档位无效")
    multiplier = 1 if consecutive_failures <= 0 else 2 ** min(consecutive_failures, 3)
    hours = min(168, FREQUENCY_HOURS[frequency_tier] * multiplier)
    return checked_at + timedelta(hours=hours)


def _eligible(config: ProductWatchConfig) -> bool:
    return bool(
        config.enabled
        and config.effective_target_price is not None
        and config.effective_target_price > 0
        and config.product is not None
        and (config.product.jan or "").strip()
    )


def select_due_watch_configs(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[ProductWatchConfig]:
    now = now or datetime.now(timezone.utc)
    settings = get_monitor_settings()
    return list(session.scalars(
        select(ProductWatchConfig)
        .join(Product, Product.id == ProductWatchConfig.product_id)
        .where(
            ProductWatchConfig.enabled.is_(True),
            ProductWatchConfig.effective_target_price.is_not(None),
            ProductWatchConfig.effective_target_price > 0,
            Product.jan.is_not(None),
            Product.jan != "",
            or_(ProductWatchConfig.next_check_at.is_(None), ProductWatchConfig.next_check_at <= now),
        )
        .options(selectinload(ProductWatchConfig.product))
        .order_by(ProductWatchConfig.next_check_at, ProductWatchConfig.id)
        .limit(limit or settings.max_items_per_cycle)
    ))


def _provider_codes(view: PriceLookupView) -> str | None:
    codes = sorted({attempt.provider_code for attempt in view.attempts})
    return ",".join(codes)[:255] or None


def _attempt_error(view: PriceLookupView) -> str:
    failures = [
        f"{attempt.provider_code}:{attempt.status}:{attempt.message or '无详情'}"
        for attempt in view.attempts
        if attempt.status not in SUCCESS_ATTEMPT_STATUSES
    ]
    return "；".join(failures)[:1000] or "没有可用 Provider"


def _stock_status(view: PriceLookupView) -> bool | None:
    if view.trusted_offers:
        return True
    offers = list(view.run.offers)
    if not offers:
        return None
    return any(offer.stock_status != "out_of_stock" for offer in offers)


def _create_notification(
    session: Session,
    config: ProductWatchConfig,
    snapshot: ProductWatchSnapshot,
    event_type: str,
    dedupe_key: str,
    *,
    now: datetime,
    settings: MonitorSettings,
) -> ProductWatchNotification | None:
    existing = session.scalar(
        select(ProductWatchNotification.id).where(ProductWatchNotification.dedupe_key == dedupe_key)
    )
    if existing is not None:
        return None
    if event_type == "monitor_failed":
        cooldown_start = now - timedelta(seconds=settings.notification_dedupe_seconds)
        recent = session.scalar(
            select(ProductWatchNotification.id)
            .where(
                ProductWatchNotification.product_id == config.product_id,
                ProductWatchNotification.event_type == "monitor_failed",
                ProductWatchNotification.triggered_at >= cooldown_start,
            )
            .limit(1)
        )
        if recent is not None:
            return None
    notification = ProductWatchNotification(
        watch_config_id=config.id,
        product_id=config.product_id,
        snapshot_id=snapshot.id,
        event_type=event_type,
        target_price=config.effective_target_price,
        current_price=snapshot.total_price,
        marketplace=snapshot.marketplace,
        seller=snapshot.seller,
        url=snapshot.url,
        dedupe_key=dedupe_key,
        triggered_at=now,
        data_updated_at=snapshot.checked_at,
    )
    session.add(notification)
    return notification


def _save_failure(
    session: Session,
    config: ProductWatchConfig,
    *,
    now: datetime,
    error_summary: str,
    provider_codes: str | None,
    lookup_history_id: int | None,
    settings: MonitorSettings,
) -> MonitorCheckResult:
    config.consecutive_failures += 1
    config.last_check_at = now
    config.next_check_at = calculate_next_check_at(
        config.frequency_tier, now, consecutive_failures=config.consecutive_failures,
    )
    config.last_check_status = "failed"
    config.last_provider_codes = provider_codes
    config.last_error_summary = error_summary[:1000]
    snapshot = ProductWatchSnapshot(
        watch_config_id=config.id,
        product_id=config.product_id,
        price_lookup_history_id=lookup_history_id,
        checked_at=now,
        status="failed",
        result_count=0,
        provider_codes=provider_codes,
        error_summary=error_summary[:1000],
    )
    session.add(snapshot)
    session.flush()
    notification_count = 0
    if config.consecutive_failures >= settings.max_consecutive_failures and not config.failure_notification_sent:
        notification = _create_notification(
            session, config, snapshot, "monitor_failed",
            f"monitor_failed:{config.id}:{snapshot.id}", now=now, settings=settings,
        )
        config.failure_notification_sent = True
        notification_count = int(notification is not None)
    session.commit()
    return MonitorCheckResult(
        config.id, config.product_id, "failed", snapshot.id, notification_count, error_summary[:1000],
    )


def run_watch_check(
    session: Session,
    watch_config_id: int,
    *,
    providers: list[PriceProvider] | None = None,
    now: datetime | None = None,
    force: bool = False,
    settings: MonitorSettings | None = None,
) -> MonitorCheckResult:
    now = now or datetime.now(timezone.utc)
    settings = settings or get_monitor_settings()
    config = session.scalar(
        select(ProductWatchConfig)
        .where(ProductWatchConfig.id == watch_config_id)
        .options(selectinload(ProductWatchConfig.product))
    )
    if config is None:
        raise LookupError("关注配置不存在")
    if not _eligible(config):
        return MonitorCheckResult(config.id, config.product_id, "skipped", None)
    if not force and config.next_check_at is not None and config.next_check_at > now:
        return MonitorCheckResult(config.id, config.product_id, "skipped", None)

    previous_in_stock = config.last_in_stock
    previous_historical_low = config.historical_online_lowest_price
    try:
        view = query_prices(
            session,
            PriceLookupInput(jan=config.product.jan, force_refresh=True),
            providers,
            now=now,
            lookup_source="monitor",
            provider_timeout_seconds=settings.request_timeout_seconds,
        )
    except Exception as exc:
        session.rollback()
        config = session.scalar(
            select(ProductWatchConfig)
            .where(ProductWatchConfig.id == watch_config_id)
            .options(selectinload(ProductWatchConfig.product))
        )
        if config is None:
            raise
        return _save_failure(
            session, config, now=now, error_summary=f"监控查询失败：{type(exc).__name__}",
            provider_codes=None, lookup_history_id=None, settings=settings,
        )

    provider_codes = _provider_codes(view)
    if not any(attempt.status in SUCCESS_ATTEMPT_STATUSES for attempt in view.attempts):
        session.refresh(config)
        return _save_failure(
            session, config, now=now, error_summary=_attempt_error(view),
            provider_codes=provider_codes, lookup_history_id=view.history.id, settings=settings,
        )

    winner = view.trusted_offers[0] if view.trusted_offers else None
    in_stock = _stock_status(view)
    snapshot = ProductWatchSnapshot(
        watch_config_id=config.id,
        product_id=config.product_id,
        price_lookup_history_id=view.history.id,
        checked_at=now,
        lowest_item_price=winner.item_price if winner else None,
        shipping_price=winner.shipping_price if winner else None,
        total_price=winner.total_price if winner else None,
        marketplace=winner.marketplace.name if winner else None,
        seller=winner.seller if winner else None,
        url=winner.url if winner else None,
        is_in_stock=in_stock,
        result_count=len(view.trusted_offers),
        status="success",
        provider_codes=provider_codes,
    )
    session.add(snapshot)
    session.flush()
    session.refresh(config)
    config.previous_lowest_price = config.current_lowest_price
    config.current_lowest_price = winner.total_price if winner else None
    config.last_check_at = now
    config.next_check_at = calculate_next_check_at(config.frequency_tier, now)
    config.last_check_status = "success"
    config.last_provider_codes = provider_codes
    config.last_error_summary = None
    config.consecutive_failures = 0
    config.failure_notification_sent = False
    if in_stock is not None:
        config.last_in_stock = in_stock
    if winner and (
        config.historical_online_lowest_price is None
        or winner.total_price < config.historical_online_lowest_price
    ):
        config.historical_online_lowest_price = winner.total_price

    notification_count = 0
    if config.enabled and winner is not None:
        if winner.total_price <= config.effective_target_price:
            notification = _create_notification(
                session, config, snapshot, "target_reached",
                f"target_reached:{config.product_id}:{winner.total_price}", now=now, settings=settings,
            )
            if notification is not None:
                config.last_target_reached_at = now
                notification_count += 1
        if previous_historical_low is not None and winner.total_price < previous_historical_low:
            notification = _create_notification(
                session, config, snapshot, "new_historical_low",
                f"new_historical_low:{config.product_id}:{winner.total_price}", now=now, settings=settings,
            )
            notification_count += int(notification is not None)
        if previous_in_stock is False and in_stock is True and config.monitor_restock:
            notification = _create_notification(
                session, config, snapshot, "restocked",
                f"restocked:{config.product_id}:{snapshot.id}", now=now, settings=settings,
            )
            notification_count += int(notification is not None)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        session.refresh(config)
    return MonitorCheckResult(config.id, config.product_id, "success", snapshot.id, notification_count)


def _scheduler_state(session: Session) -> MonitorSchedulerState:
    state = session.scalar(select(MonitorSchedulerState).where(MonitorSchedulerState.code == "default"))
    if state is None:
        state = MonitorSchedulerState(code="default")
        session.add(state)
        session.flush()
    return state


def run_due_watch_checks(
    session: Session,
    *,
    providers: list[PriceProvider] | None = None,
    now: datetime | None = None,
    settings: MonitorSettings | None = None,
) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    settings = settings or get_monitor_settings()
    state = _scheduler_state(session)
    state.last_scan_started_at = now
    session.commit()
    due_ids = [config.id for config in select_due_watch_configs(session, now=now, limit=settings.max_items_per_cycle)]
    success_count = failure_count = 0
    errors: list[str] = []
    for config_id in due_ids:
        try:
            result = run_watch_check(
                session, config_id, providers=providers, now=now, settings=settings,
            )
            if result.status == "success":
                success_count += 1
            elif result.status == "failed":
                failure_count += 1
                if result.error_summary:
                    errors.append(result.error_summary)
        except Exception as exc:
            session.rollback()
            failure_count += 1
            errors.append(f"配置{config_id}:{type(exc).__name__}")
    state = _scheduler_state(session)
    state.last_scan_completed_at = datetime.now(timezone.utc)
    state.last_success_count = success_count
    state.last_failure_count = failure_count
    state.last_error_summary = "；".join(errors)[:1000] or None
    session.commit()
    return success_count, failure_count


def run_due_monitor_cycle() -> tuple[int, int]:
    if not _cycle_lock.acquire(blocking=False):
        return 0, 0
    try:
        with SessionLocal() as session:
            return run_due_watch_checks(session)
    finally:
        _cycle_lock.release()


def run_single_monitor_cycle(product_id: int) -> MonitorCheckResult:
    if not _cycle_lock.acquire(blocking=False):
        raise RuntimeError("监控任务正在运行，请稍后重试")
    try:
        with SessionLocal() as session:
            config = session.scalar(
                select(ProductWatchConfig).where(ProductWatchConfig.product_id == product_id)
            )
            if config is None:
                raise LookupError("关注配置不存在")
            return run_watch_check(session, config.id, force=True)
    finally:
        _cycle_lock.release()


def list_notifications(session: Session, *, include_archived: bool = False) -> list[ProductWatchNotification]:
    query = select(ProductWatchNotification).options(
        selectinload(ProductWatchNotification.product),
        selectinload(ProductWatchNotification.snapshot).selectinload(ProductWatchSnapshot.lookup_history),
    )
    if not include_archived:
        query = query.where(ProductWatchNotification.archived_at.is_(None))
    return list(session.scalars(
        query.order_by(ProductWatchNotification.is_read, ProductWatchNotification.triggered_at.desc())
    ))


def unread_notification_count(session: Session) -> int:
    return session.scalar(
        select(func.count(ProductWatchNotification.id)).where(
            ProductWatchNotification.is_read.is_(False),
            ProductWatchNotification.archived_at.is_(None),
        )
    ) or 0


def mark_notification_read(session: Session, notification_id: int) -> ProductWatchNotification:
    notification = session.get(ProductWatchNotification, notification_id)
    if notification is None:
        raise LookupError("通知不存在")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(timezone.utc)
        session.commit()
    return notification


def bulk_mark_notifications_read(session: Session, notification_ids: set[int]) -> int:
    notifications = list(session.scalars(
        select(ProductWatchNotification).where(
            ProductWatchNotification.id.in_(notification_ids),
            ProductWatchNotification.is_read.is_(False),
        )
    )) if notification_ids else []
    now = datetime.now(timezone.utc)
    for notification in notifications:
        notification.is_read = True
        notification.read_at = now
    session.commit()
    return len(notifications)


def archive_read_notifications(session: Session) -> int:
    notifications = list(session.scalars(
        select(ProductWatchNotification).where(
            ProductWatchNotification.is_read.is_(True),
            ProductWatchNotification.archived_at.is_(None),
        )
    ))
    now = datetime.now(timezone.utc)
    for notification in notifications:
        notification.archived_at = now
    session.commit()
    return len(notifications)


def monitor_dashboard(session: Session, *, now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    enabled_count = session.scalar(
        select(func.count(ProductWatchConfig.id)).where(ProductWatchConfig.enabled.is_(True))
    ) or 0
    due_count = session.scalar(
        select(func.count(ProductWatchConfig.id))
        .join(Product, Product.id == ProductWatchConfig.product_id)
        .where(
            ProductWatchConfig.enabled.is_(True),
            ProductWatchConfig.effective_target_price.is_not(None),
            Product.jan.is_not(None),
            Product.jan != "",
            or_(ProductWatchConfig.next_check_at.is_(None), ProductWatchConfig.next_check_at <= now),
        )
    ) or 0
    failures = list(session.scalars(
        select(ProductWatchConfig)
        .where(ProductWatchConfig.consecutive_failures > 0)
        .options(selectinload(ProductWatchConfig.product))
        .order_by(ProductWatchConfig.consecutive_failures.desc(), ProductWatchConfig.id)
    ))
    return {
        "settings": get_monitor_settings(),
        "enabled_count": enabled_count,
        "due_count": due_count,
        "state": session.scalar(select(MonitorSchedulerState).where(MonitorSchedulerState.code == "default")),
        "failures": failures,
    }
