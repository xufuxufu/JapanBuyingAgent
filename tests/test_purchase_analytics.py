from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.analytics_service import analytics_dashboard, inventory_distribution, resolve_date_range
from app.models import (
    ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine,
)
from app.schemas import StoreCreateInput
from app.services import confirm_receipt
from app.store_service import confirm_receipt_store, create_store
from tests.test_purchase_batches import make_confirmable_receipt


def test_dashboard_uses_confirmed_facts_and_keeps_counts_quantities_and_amounts_separate(client):
    http, db, _ = client
    store = create_store(db, StoreCreateInput(name_cn="分析门店", name_ja="分析店舗"))
    source, receipt, products, _ = make_confirmable_receipt(db, item_count=2)
    confirm_receipt_store(db, receipt, store)
    confirm_receipt(db, source, receipt)
    receipt.discount_total = 777
    db.commit()

    period = resolve_date_range("custom", date(2026, 7, 16), date(2026, 7, 16), today=date(2026, 7, 17))
    view = analytics_dashboard(db, period)
    assert view["metrics"]["purchase_count"] == 1
    assert view["metrics"]["quantity"] == 3
    assert view["metrics"]["amount"] == 270
    assert view["metrics"]["store_count"] == 1
    assert view["stores"][0]["store"].id == store.id
    assert {row["product"].id for row in view["products"]} == {product.id for product in products}

    purchase = db.scalar(select(PurchaseBatch))
    purchase.status = "cancelled"
    db.commit()
    cancelled = analytics_dashboard(db, period)
    assert cancelled["metrics"]["purchase_count"] == 0
    assert cancelled["metrics"]["quantity"] == 0
    assert cancelled["metrics"]["amount"] == 0


def test_analytics_and_traceable_detail_pages_return_200_with_distinct_price_sources(client):
    http, db, _ = client
    store = create_store(db, StoreCreateInput(name_cn="价格门店", name_ja="価格店舗"))
    source, receipt, products, _ = make_confirmable_receipt(db, item_count=1)
    confirm_receipt_store(db, receipt, store)
    confirm_receipt(db, source, receipt)
    product = products[0]
    watch = ProductWatchConfig(
        product_id=product.id, enabled=True, frequency_tier="normal",
        effective_target_price=120, current_lowest_price=100,
    )
    db.add(watch)
    db.flush()
    db.add(ProductWatchSnapshot(
        watch_config_id=watch.id, product_id=product.id,
        checked_at=datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc),
        total_price=100, status="success", result_count=1, marketplace="manual", seller="线上店",
    ))
    db.commit()

    dashboard = http.get("/purchase-analytics?range=custom&start_date=2026-07-16&end_date=2026-07-16")
    assert dashboard.status_code == 200
    assert "采购次数与数量趋势" in dashboard.text and "整单优惠不分摊" in dashboard.text
    bucket = "2026-07-16"
    drilldown = http.get(f"/purchase-analytics?range=custom&start_date=2026-07-16&end_date=2026-07-16&bucket={bucket}")
    product_page = http.get(f"/products/{product.id}")
    store_page = http.get(f"/stores/{store.id}")
    assert drilldown.status_code == product_page.status_code == store_page.status_code == 200
    assert "查看原小票" in drilldown.text and "查看采购批次" in drilldown.text
    assert "采购价" in product_page.text and "线上监控最低价" in product_page.text
    assert "月度采购趋势" in store_page.text and "最近购买日期不代表当前仍有货" in store_page.text


def test_empty_dashboard_and_stale_latest_snapshot_distribution_are_safe(client):
    http, db, _ = client
    empty = http.get("/purchase-analytics")
    assert empty.status_code == 200 and "所选范围暂无采购数据" in empty.text

    _, _, products, locations = make_confirmable_receipt(db, item_count=1)
    snapshot = QinsiInventorySnapshot(
        batch_no="ANALYTICS-STALE", original_filename="stale.xlsx", file_hash="analytics-stale",
        file_content=b"snapshot", imported_at=datetime.now(timezone.utc) - timedelta(hours=100),
        data_at=datetime.now(timezone.utc) - timedelta(hours=100), status="completed",
        total_rows=1, success_rows=1,
    )
    db.add(snapshot)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snapshot.id, original_row_no=2, raw_product_name="过期库存商品",
        raw_summary_json="{}", product_id=products[0].id,
        warehouse_id=locations["新日本仓库"].id, quantity=5,
        matching_status="matched", warehouse_status="matched",
    ))
    db.commit()
    latest, rows = inventory_distribution(db)
    assert latest.id == snapshot.id
    assert {row["key"]: row["count"] for row in rows}["snapshot_stale"] == 1
