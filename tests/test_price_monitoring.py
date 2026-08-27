from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import Product, ProductWatchConfig, ProductWatchNotification
from app.monitor_service import (
    MonitorSettings,
    archive_read_notifications,
    bulk_mark_notifications_read,
    calculate_next_check_at,
    mark_notification_read,
    run_due_watch_checks,
    run_watch_check,
    select_due_watch_configs,
)
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse


NOW = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)


@dataclass
class FakeProvider(PriceProvider):
    responses: dict[str, ProviderResponse | Exception]
    code: str = "monitor_fake"
    display_name: str = "Monitor Fake"
    base_url: str | None = "https://example.test/"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        response = self.responses[jan]
        if isinstance(response, Exception):
            raise response
        return response


def settings(*, max_failures: int = 3) -> MonitorSettings:
    return MonitorSettings(False, 3600, 20, max_failures, 1.0, 60)


def offer(jan: str, price: int, *, stock_status: str = "in_stock") -> ProviderResponse:
    return ProviderResponse("success", (PriceCandidate(
        title="监控商品 100ml", url=f"https://example.test/{jan}/{price}", seller="测试店",
        item_price=price, shipping_price=0, jan=jan, stock_status=stock_status,
        jan_verified=True, match_type="JAN_EXACT", confidence=1.0,
    ),))


def watch(db, suffix: int, *, enabled: bool = True, target: int | None = 1000,
          next_check_at: datetime | None = None, monitor_restock: bool = False):
    base = f"490000000{suffix:03d}"
    check_digit = (10 - sum((1 if index % 2 == 0 else 3) * int(value) for index, value in enumerate(base)) % 10) % 10
    jan = f"{base}{check_digit}"
    product = Product(jan=jan, name_cn=f"监控商品{suffix}", name_ja=f"監視商品{suffix}")
    db.add(product)
    db.flush()
    config = ProductWatchConfig(
        product_id=product.id, enabled=enabled, user_target_price=target,
        effective_target_price=target, recommended_target_price=target,
        frequency_tier="normal", next_check_at=next_check_at,
        monitor_restock=monitor_restock,
    )
    db.add(config)
    db.commit()
    return product, config


def test_due_selection_and_frequency(db_session):
    due = watch(db_session, 1, next_check_at=NOW - timedelta(seconds=1))[1]
    watch(db_session, 2, next_check_at=NOW + timedelta(seconds=1))
    watch(db_session, 3, enabled=False, next_check_at=NOW - timedelta(days=1))
    watch(db_session, 4, target=None, next_check_at=NOW - timedelta(days=1))
    selected = select_due_watch_configs(db_session, now=NOW)
    assert [item.id for item in selected] == [due.id]
    assert calculate_next_check_at("normal", NOW) == NOW + timedelta(hours=12)
    assert calculate_next_check_at("normal", NOW, consecutive_failures=2) == NOW + timedelta(hours=48)


def test_target_dedupe_lower_price_and_pause(db_session):
    product, config = watch(db_session, 10)
    provider = FakeProvider({product.jan: offer(product.jan, 900)})
    run_watch_check(db_session, config.id, providers=[provider], now=NOW, force=True, settings=settings())
    run_watch_check(db_session, config.id, providers=[provider], now=NOW + timedelta(hours=1), force=True, settings=settings())
    provider.responses[product.jan] = offer(product.jan, 800)
    run_watch_check(db_session, config.id, providers=[provider], now=NOW + timedelta(hours=2), force=True, settings=settings())
    target_events = list(db_session.scalars(select(ProductWatchNotification).where(
        ProductWatchNotification.event_type == "target_reached"
    ).order_by(ProductWatchNotification.current_price)))
    assert [item.current_price for item in target_events] == [800, 900]
    config.enabled = False
    db_session.commit()
    provider.responses[product.jan] = offer(product.jan, 700)
    assert run_watch_check(db_session, config.id, providers=[provider], now=NOW + timedelta(hours=3), force=True, settings=settings()).status == "skipped"
    assert db_session.scalar(select(func.count()).select_from(ProductWatchNotification).where(
        ProductWatchNotification.event_type == "target_reached"
    )) == 2


def test_restock_notification(db_session):
    product, config = watch(db_session, 20, target=1, monitor_restock=True)
    provider = FakeProvider({product.jan: offer(product.jan, 900, stock_status="out_of_stock")})
    run_watch_check(db_session, config.id, providers=[provider], now=NOW, force=True, settings=settings())
    provider.responses[product.jan] = offer(product.jan, 900)
    run_watch_check(db_session, config.id, providers=[provider], now=NOW + timedelta(hours=1), force=True, settings=settings())
    assert db_session.scalar(select(func.count()).select_from(ProductWatchNotification).where(
        ProductWatchNotification.event_type == "restocked"
    )) == 1


def test_provider_failure_does_not_stop_batch_and_failure_notifies_once(db_session):
    failed_product, failed = watch(db_session, 30, next_check_at=NOW)
    ok_product, _ = watch(db_session, 31, next_check_at=NOW)
    provider = FakeProvider({failed_product.jan: RuntimeError("boom"), ok_product.jan: offer(ok_product.jan, 900)})
    assert run_due_watch_checks(db_session, providers=[provider], now=NOW, settings=settings(max_failures=2)) == (1, 1)
    run_watch_check(db_session, failed.id, providers=[provider], now=NOW + timedelta(hours=1), force=True, settings=settings(max_failures=2))
    run_watch_check(db_session, failed.id, providers=[provider], now=NOW + timedelta(hours=2), force=True, settings=settings(max_failures=2))
    assert db_session.scalar(select(func.count()).select_from(ProductWatchNotification).where(
        ProductWatchNotification.event_type == "monitor_failed"
    )) == 1


def test_notification_read_bulk_read_and_archive(db_session):
    product, config = watch(db_session, 40)
    provider = FakeProvider({product.jan: offer(product.jan, 900)})
    run_watch_check(db_session, config.id, providers=[provider], now=NOW, force=True, settings=settings())
    provider.responses[product.jan] = offer(product.jan, 800)
    run_watch_check(db_session, config.id, providers=[provider], now=NOW + timedelta(hours=1), force=True, settings=settings())
    notifications = list(db_session.scalars(select(ProductWatchNotification).order_by(ProductWatchNotification.id)))
    mark_notification_read(db_session, notifications[0].id)
    assert notifications[0].is_read and notifications[0].read_at is not None
    assert bulk_mark_notifications_read(db_session, {item.id for item in notifications}) == len(notifications) - 1
    assert archive_read_notifications(db_session) == len(notifications)
    assert all(item.archived_at is not None for item in notifications)


def test_monitor_pages_return_200(client):
    http, db, _ = client
    watch(db, 50)
    assert http.get("/watched-products").status_code == 200
    assert http.get("/notifications").status_code == 200
    assert http.get("/monitor-status").status_code == 200
