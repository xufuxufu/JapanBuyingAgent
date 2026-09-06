from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.main as main_module

from app.models import (
    Location, Marketplace, PriceSearchRun, Product, ProductOffer, ProductWatchConfig,
    ProductWatchRecommendation, PurchaseBatch, PurchaseBatchItem, Receipt, ReceiptBatch, ReceiptItem,
)
from app.watch_service import (
    accept_recommendations, add_watch, bulk_enable_watches, calculate_recommended_target,
    generate_watch_recommendations, list_watch_groups, refresh_recommended_target, remove_watch,
    set_watch_enabled, update_watch,
)


def make_product(db, suffix: str, *, purchase_price=None) -> Product:
    product = Product(
        jan=f"0490000000{suffix:0>3}", name_cn=f"关注商品{suffix}", name_ja=f"商品{suffix}",
        purchase_price=purchase_price,
    )
    db.add(product)
    db.flush()
    return product


def locations(db) -> tuple[Location, Location]:
    local = db.scalar(select(Location).where(Location.internal_code == "WATCH-LOCAL"))
    qinsi = db.scalar(select(Location).where(Location.internal_code == "WATCH-QINSI"))
    if local is None:
        local = Location(internal_code="WATCH-LOCAL", display_name="关注测试本地", location_type="local_physical")
        qinsi = Location(
            internal_code="WATCH-QINSI", display_name="关注测试秦丝", location_type="qinsi_warehouse",
            is_qinsi_warehouse=True,
        )
        db.add_all([local, qinsi])
        db.flush()
    return local, qinsi


def add_purchase(db, product: Product, price: int, *, status="confirmed", days=0, quantity=1) -> PurchaseBatch:
    local, qinsi = locations(db)
    seq = (db.scalar(select(func.count(ReceiptBatch.id))) or 0) + 1
    purchased_at = datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=days)
    source = ReceiptBatch(batch_no=f"WATCH-RB-{seq}", status="confirmed")
    receipt = Receipt(
        batch=source, raw_store_name="关注测试店", purchased_at=purchased_at,
        confirmation_status="confirmed", review_status="reviewed", confirmed_at=purchased_at,
    )
    receipt_item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name=product.name_ja or product.name_cn,
        product_id=product.id, quantity=quantity, unit_price=price, line_total=price * quantity,
        discount_amount=0, confidence=1, review_status="confirmed",
    )
    db.add_all([source, receipt, receipt_item])
    db.flush()
    batch = PurchaseBatch(
        batch_no=f"WATCH-PB-{seq}", receipt_id=receipt.id, gpt_batch_id=source.id,
        purchased_at=purchased_at, confirmed_at=purchased_at, status=status,
        default_initial_location_id=local.id, default_qinsi_warehouse_id=qinsi.id,
    )
    db.add(batch)
    db.flush()
    db.add(PurchaseBatchItem(
        purchase_batch_id=batch.id, product_id=product.id, receipt_item_id=receipt_item.id,
        quantity=quantity, unit_price=price, actual_line_amount=price * quantity,
        initial_location_id=local.id, qinsi_target_warehouse_id=qinsi.id,
    ))
    db.commit()
    return batch


def add_online_price(db, product: Product, price: int) -> None:
    marketplace = Marketplace(code=f"watch-{product.id}", name="关注测试商城", active=True)
    run = PriceSearchRun(
        product_id=product.id, jan=product.jan, status="completed",
        completed_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    db.add_all([marketplace, run])
    db.flush()
    db.add(ProductOffer(
        search_run_id=run.id, marketplace_id=marketplace.id, product_id=product.id, jan=product.jan,
        title=product.name_ja, url="https://example.test/watch", item_price=price, shipping_price=0,
        total_price=price, is_trusted=True, fetched_at=run.completed_at,
    ))
    db.commit()


def test_one_watch_config_per_product_and_user_target_wins(db_session):
    product = make_product(db_session, "1", purchase_price=1200)
    first = add_watch(db_session, product.id, user_target_price=900)
    second = add_watch(db_session, product.id)
    assert first.id == second.id
    assert (second.user_target_price, second.recommended_target_price, second.effective_target_price) == (900, 1200, 900)
    duplicate = ProductWatchConfig(product_id=product.id)
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_recommendation_refresh_never_overwrites_user_target(db_session):
    product = make_product(db_session, "2", purchase_price=1000)
    config = add_watch(db_session, product.id, user_target_price=800)
    product.purchase_price = 700
    refresh_recommended_target(db_session, config)
    db_session.commit()
    assert (config.user_target_price, config.recommended_target_price, config.effective_target_price) == (800, 700, 800)


def test_recommended_price_falls_back_history_recent_online_and_ignores_cancelled(db_session):
    history = make_product(db_session, "3")
    add_purchase(db_session, history, 1100, days=1)
    add_purchase(db_session, history, 900, days=2)
    add_purchase(db_session, history, 100, status="cancelled", days=3)
    assert calculate_recommended_target(db_session, history.id)[:2] == (900, "historical_lowest_purchase")

    recent = make_product(db_session, "4", purchase_price=850)
    db_session.commit()
    assert calculate_recommended_target(db_session, recent.id)[:2] == (850, "latest_purchase")

    online = make_product(db_session, "5")
    db_session.commit()
    add_online_price(db_session, online, 760)
    assert calculate_recommended_target(db_session, online.id)[:2] == (760, "trusted_online_lowest")


def test_missing_target_cannot_enable_monitoring(db_session):
    product = make_product(db_session, "6")
    config = add_watch(db_session, product.id)
    assert config.effective_target_price is None
    with pytest.raises(ValueError, match="没有有效目标价"):
        set_watch_enabled(db_session, product.id, True)
    db_session.rollback()
    assert config.enabled is False


def test_recommendation_is_deduplicated_and_accept_does_not_enable(db_session, monkeypatch):
    monkeypatch.setenv("JBA_WATCH_PURCHASE_QUANTITY_THRESHOLD", "2")
    product = make_product(db_session, "7", purchase_price=600)
    add_purchase(db_session, product, 600, quantity=2)
    generate_watch_recommendations(db_session)
    generate_watch_recommendations(db_session)
    recommendations = list(db_session.scalars(select(ProductWatchRecommendation).where(ProductWatchRecommendation.product_id == product.id)))
    assert len(recommendations) == 1
    configs = accept_recommendations(db_session, {recommendations[0].id})
    assert len(configs) == 1 and configs[0].enabled is False and configs[0].frequency_tier == "low"


def test_bulk_accept_and_enable_only_legal_items(db_session):
    recommended = make_product(db_session, "8", purchase_price=500)
    ignored = make_product(db_session, "9", purchase_price=400)
    no_price = make_product(db_session, "10")
    db_session.flush()
    pending = ProductWatchRecommendation(product_id=recommended.id, reason="scan_count")
    handled = ProductWatchRecommendation(product_id=ignored.id, reason="scan_count", ignored=True)
    db_session.add_all([pending, handled])
    db_session.commit()
    accepted = accept_recommendations(db_session, {pending.id, handled.id, 999999})
    assert [item.product_id for item in accepted] == [recommended.id]
    no_price_config = add_watch(db_session, no_price.id)
    enabled = bulk_enable_watches(db_session, {recommended.id, no_price.id, 999999})
    assert [item.product_id for item in enabled] == [recommended.id]
    assert no_price_config.enabled is False


def test_watch_list_and_product_detail_return_200(client):
    http, db, _ = client
    product = make_product(db, "11", purchase_price=700)
    add_watch(db, product.id, user_target_price=650)
    listing = http.get("/watched-products")
    detail = http.get(f"/products/{product.id}")
    assert listing.status_code == 200 and "关注商品11" in listing.text
    assert detail.status_code == 200 and "关注状态" in detail.text and "650" in detail.text


def test_product_detail_shows_dash_not_none_for_missing_watch_target_price(client):
    http, db, _ = client
    product = make_product(db, "17")  # no purchase_price, no online price -> no valid target
    config = add_watch(db, product.id)
    assert config.effective_target_price is None

    detail = http.get(f"/products/{product.id}")

    assert detail.status_code == 200
    assert ">None<" not in detail.text
    assert "关注目标价" in detail.text


def test_update_frequency_restock_and_pause(db_session):
    product = make_product(db_session, "12", purchase_price=900)
    add_watch(db_session, product.id)
    config = update_watch(
        db_session, product.id, user_target_price="800", frequency_tier="urgent", monitor_restock=True,
    )
    set_watch_enabled(db_session, product.id, True)
    paused = set_watch_enabled(db_session, product.id, False)
    assert (config.frequency_tier, config.monitor_restock) == ("urgent", True)
    assert paused.enabled is False and paused.pause_reason == "user_paused"


def test_watch_decimal_price_and_remove_are_idempotent(db_session):
    product = make_product(db_session, "13", purchase_price=1200)
    db_session.commit()
    product_id = product.id
    db_session.expire_all()
    config = add_watch(db_session, product_id)
    assert config.recommended_target_price == 1200
    assert remove_watch(db_session, product_id) is True
    assert remove_watch(db_session, product_id) is False


def test_watch_ajax_success_duplicate_and_remove(client):
    http, db, _ = client
    product = make_product(db, "14", purchase_price=700)
    db.commit()
    headers = {"X-Requested-With": "fetch"}
    first = http.post("/watched-products/add", data={"product_id": product.id}, headers=headers)
    duplicate = http.post("/watched-products/add", data={"product_id": product.id}, headers=headers)
    removed = http.post(f"/watched-products/{product.id}/remove", data={}, headers=headers)
    repeated = http.post(f"/watched-products/{product.id}/remove", data={}, headers=headers)
    assert first.json() == {"ok": True, "message": "已关注"}
    assert duplicate.json() == {"ok": True, "message": "已关注"}
    assert removed.json() == {"ok": True, "message": "已取消关注"}
    assert repeated.json() == {"ok": True, "message": "当前未关注"}


def test_manual_watch_resolves_pending_recommendation_no_duplicate_listing(db_session):
    # Root cause of "点击关注没有正确加入关注商品列表": add_watch used to leave
    # any pre-existing pending recommendation untouched, so the same product
    # showed up twice -- once under its new watch, again under "系统推荐"
    # still demanding accept/ignore -- which reads as "watching didn't work".
    product = make_product(db_session, "16", purchase_price=300)
    recommendation = ProductWatchRecommendation(product_id=product.id, reason="scan_count")
    db_session.add(recommendation)
    db_session.commit()

    add_watch(db_session, product.id, source="manual")

    db_session.refresh(recommendation)
    assert recommendation.accepted is True
    groups = list_watch_groups(db_session)
    assert [row.product.id for row in groups["watched"]] == [product.id]
    assert groups["recommended"] == []


def test_watch_ajax_database_error_is_masked(client, monkeypatch):
    http, db, _ = client
    product = make_product(db, "15", purchase_price=700)
    db.commit()
    monkeypatch.setattr(main_module, "add_watch", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private traceback")))
    # Unexpected programming errors remain visible to test infrastructure; database errors are the user-facing case.
    from sqlalchemy.exc import OperationalError
    monkeypatch.setattr(main_module, "add_watch", lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationalError("stmt", {}, Exception("db"))))
    response = http.post("/watched-products/add", data={"product_id": product.id}, headers={"X-Requested-With": "fetch"})
    assert response.status_code == 503
    assert response.json() == {"ok": False, "message": "关注保存失败，请稍后重试"}
    assert "stmt" not in response.text and "private traceback" not in response.text
