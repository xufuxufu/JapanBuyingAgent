from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.analytics_service import analytics_dashboard, inventory_distribution, resolve_date_range
from app.models import (
    ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, RestockList,
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
    outside = analytics_dashboard(
        db, resolve_date_range("custom", date(2026, 7, 17), date(2026, 7, 17), today=date(2026, 7, 17)),
    )
    assert outside["metrics"]["purchase_count"] == 0 and outside["metrics"]["amount"] == 0

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
    source, receipt, products, locations = make_confirmable_receipt(db, item_count=1)
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
    snapshot = QinsiInventorySnapshot(
        batch_no="ANALYTICS-LOW", original_filename="low.xlsx", file_hash="analytics-low",
        file_content=b"snapshot", imported_at=datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc),
        data_at=datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc), status="completed",
        total_rows=1, success_rows=1,
    )
    db.add(snapshot)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snapshot.id, original_row_no=2, raw_product_name="低库存达价商品",
        raw_summary_json="{}", product_id=product.id, warehouse_id=locations["新日本仓库"].id,
        quantity=1, matching_status="matched", warehouse_status="matched",
    ))
    active_list = RestockList(
        name="分析联动清单", store_id=store.id, status="active", source_type="purchase_analysis",
    )
    db.add(active_list)
    db.commit()

    period = resolve_date_range("custom", date(2026, 7, 16), date(2026, 7, 16), today=date(2026, 7, 17))
    view = analytics_dashboard(db, period)
    assert view["metrics"]["low_stock_watched"] == 1
    assert [row["product"].id for row in view["restock_candidates"]] == [product.id]
    dashboard = http.get("/purchase-analytics?range=custom&start_date=2026-07-16&end_date=2026-07-16")
    assert dashboard.status_code == 200
    assert "采购次数与数量趋势" in dashboard.text and "整单优惠不分摊" in dashboard.text
    assert "低库存或达价关注商品候选" in dashboard.text and "选择门店并生成候选" in dashboard.text
    assert f'/restock-lists/{active_list.id}/items' in dashboard.text
    candidate_page = http.get(f"/restock-lists/new?source=purchase_analysis&product_id={product.id}")
    assert candidate_page.status_code == 200 and f'name="product_id" value="{product.id}"' in candidate_page.text
    bucket = "2026-07-16"
    drilldown = http.get(f"/purchase-analytics?range=custom&start_date=2026-07-16&end_date=2026-07-16&bucket={bucket}")
    product_page = http.get(f"/products/{product.id}")
    store_page = http.get(f"/stores/{store.id}")
    store_month_page = http.get(f"/stores/{store.id}?month=2026-07")
    assert drilldown.status_code == product_page.status_code == store_page.status_code == store_month_page.status_code == 200
    assert "查看原小票" in drilldown.text and "查看采购批次" in drilldown.text
    assert "采购价" in product_page.text and "线上监控最低价" in product_page.text
    assert "月度采购趋势" in store_page.text and "最近购买日期不代表当前仍有货" in store_page.text
    assert "2026-07 采购来源" in store_month_page.text and "原小票行" in store_month_page.text


def test_empty_dashboard_and_stale_latest_snapshot_distribution_are_safe(client):
    http, db, _ = client
    empty = http.get("/purchase-analytics")
    assert empty.status_code == 200 and "所选范围暂无采购数据" in empty.text

    _, _, products, locations = make_confirmable_receipt(db, item_count=2)
    now = datetime.now(timezone.utc)
    snapshot = QinsiInventorySnapshot(
        batch_no="ANALYTICS-STALE", original_filename="stale.xlsx", file_hash="analytics-stale",
        file_content=b"snapshot", imported_at=now - timedelta(hours=100),
        data_at=now - timedelta(hours=100), status="completed",
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
    latest_snapshot = QinsiInventorySnapshot(
        batch_no="ANALYTICS-LATEST", original_filename="latest.xlsx", file_hash="analytics-latest",
        file_content=b"snapshot", imported_at=now, data_at=now, status="completed",
        total_rows=1, success_rows=1,
    )
    db.add(latest_snapshot)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=latest_snapshot.id, original_row_no=2, raw_product_name="最新缺货商品",
        raw_summary_json="{}", product_id=products[1].id,
        warehouse_id=locations["新日本仓库"].id, quantity=0,
        matching_status="matched", warehouse_status="matched",
    ))
    db.commit()
    latest, rows = inventory_distribution(db, now=now)
    counts = {row["key"]: row["count"] for row in rows}
    assert latest.id == latest_snapshot.id
    assert counts["snapshot_stale"] == 1 and counts["out_of_stock"] == 1
