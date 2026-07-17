from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.location_service import initialize_default_locations
from app.models import (
    Location, Product, ProductWatchConfig, ProductWatchSnapshot, PurchaseBatch, PurchaseBatchItem,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, Receipt, ReceiptBatch, ReceiptItem,
    RestockList, RestockListItem, Store,
)
from app.restock_service import (
    add_product_to_list, copy_restock_list, create_restock_list, link_purchase_item,
    recent_lists_for_store, restock_candidates, trace_rows, update_restock_item,
)


NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)


def product(db, suffix: str, *, name: str | None = None) -> Product:
    item = Product(
        internal_sku=f"RST-{suffix:0>4}", jan=f"0499000000{suffix:0>3}",
        name_cn=name or f"补货商品{suffix}", name_ja=f"補充商品{suffix}",
    )
    db.add(item)
    db.flush()
    return item


def store(db, suffix: str) -> Store:
    row = Store(name=f"restock-store-{suffix}", name_cn=f"补货门店{suffix}", name_ja=f"補充店舗{suffix}")
    db.add(row)
    db.flush()
    return row


def locations(db) -> tuple[Location, Location]:
    rows = {row.display_name: row for row in initialize_default_locations(db)}
    return rows["日本家里库存"], rows["新日本仓库"]


def add_purchase(
    db, item: Product, shop: Store, *, price: int = 500, quantity: int = 1,
    days_ago: int = 1, status: str = "confirmed",
) -> PurchaseBatchItem:
    local, qinsi = locations(db)
    seq = db.scalar(select(func.count()).select_from(ReceiptBatch)) + 1
    purchased_at = NOW - timedelta(days=days_ago)
    source = ReceiptBatch(batch_no=f"RST-RB-{seq}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(
        batch=source, raw_store_name=shop.display_name, store_id=shop.id, purchased_at=purchased_at,
        confirmation_status="confirmed", review_status="reviewed", confirmed_at=purchased_at,
    )
    receipt_item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name=item.name_cn, product_id=item.id,
        quantity=quantity, unit_price=price, line_total=price * quantity,
        discount_amount=0, confidence=1, review_status="confirmed",
    )
    db.add_all([source, receipt, receipt_item])
    db.flush()
    batch = PurchaseBatch(
        batch_no=f"RST-PB-{seq}", receipt_id=receipt.id, gpt_batch_id=source.id,
        purchased_at=purchased_at, store_name=shop.display_name, store_id=shop.id,
        confirmed_at=purchased_at, status=status, default_initial_location_id=local.id,
        default_qinsi_warehouse_id=qinsi.id,
    )
    db.add(batch)
    db.flush()
    detail = PurchaseBatchItem(
        purchase_batch_id=batch.id, product_id=item.id, receipt_item_id=receipt_item.id,
        quantity=quantity, unit_price=price, actual_line_amount=price * quantity,
        initial_location_id=local.id, qinsi_target_warehouse_id=qinsi.id,
    )
    db.add(detail)
    db.commit()
    return detail


def add_inventory(db, item: Product, quantity: int, *, data_at: datetime = NOW) -> None:
    _, qinsi = locations(db)
    snapshot = QinsiInventorySnapshot(
        batch_no=f"RST-INV-{item.id}", original_filename=f"inv-{item.id}.xlsx",
        file_hash=f"hash-{item.id}", file_content=b"inventory", imported_at=data_at,
        data_at=data_at, total_rows=1, success_rows=1, status="completed",
    )
    db.add(snapshot)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snapshot.id, original_row_no=1, raw_product_name=item.name_cn,
        jan=item.jan, internal_sku=item.internal_sku, raw_warehouse_name=qinsi.display_name,
        quantity=quantity, raw_summary_json="{}", product_id=item.id, warehouse_id=qinsi.id,
        matching_method="manual", matching_status="matched", warehouse_status="matched",
    ))
    db.commit()


def add_watch_and_price(db, item: Product, *, target: int, online: int) -> None:
    config = ProductWatchConfig(
        product_id=item.id, enabled=True, user_target_price=target, effective_target_price=target,
        frequency_tier="normal", current_lowest_price=online, last_check_at=NOW,
    )
    db.add(config)
    db.flush()
    db.add(ProductWatchSnapshot(
        watch_config_id=config.id, product_id=item.id, checked_at=NOW,
        lowest_item_price=online, shipping_price=0, total_price=online,
        marketplace="manual", seller="测试卖家", url="https://example.test/offer",
        is_in_stock=True, result_count=1, status="success",
    ))
    db.commit()


def test_store_history_candidates_and_restock_list_deduplicates_items(db_session):
    shop = store(db_session, "A")
    item = product(db_session, "1")
    add_purchase(db_session, item, shop, price=498, quantity=2)

    candidates = restock_candidates(db_session, shop.id, now=NOW)
    assert [candidate.product.id for candidate in candidates] == [item.id]
    assert candidates[0].latest_store_price == 498 and "曾在该店购买" in "；".join(candidates[0].reasons)

    restock_list = create_restock_list(
        db_session, name="门店历史清单", store_id=shop.id, product_ids={item.id},
        source_type="store_history",
    )
    add_product_to_list(db_session, restock_list.id, item.id)
    db_session.add(RestockListItem(restock_list_id=restock_list.id, product_id=item.id, added_source="manual"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(RestockListItem)) == 1


def test_watched_products_route_and_recommendation_priority(client):
    http, db, _ = client
    shop = store(db, "B")
    hot = product(db, "2", name="达价低库存")
    watched = product(db, "3", name="仅关注")
    add_purchase(db, hot, shop, price=900)
    add_inventory(db, hot, 0)
    add_watch_and_price(db, hot, target=800, online=700)
    add_watch_and_price(db, watched, target=600, online=900)

    candidates = restock_candidates(db, shop.id, now=NOW)
    assert candidates[0].product.id == hot.id and candidates[0].priority == 10

    response = http.post(
        "/restock-lists/from-watches",
        data={"store_id": str(shop.id), "name": "关注生成清单", "restock_product_ids": [str(hot.id), str(watched.id)]},
        follow_redirects=False,
    )
    created = db.scalar(select(RestockList).where(RestockList.source_type == "watched_products"))
    assert response.status_code == 303
    assert created is not None and {item.product_id for item in created.items} == {hot.id, watched.id}


def test_purchased_marker_is_temporary_and_formal_receipt_link_does_not_duplicate(db_session):
    shop = store(db_session, "C")
    item = product(db_session, "4")
    purchase_item = add_purchase(db_session, item, shop, price=1200, quantity=2)
    restock_list = create_restock_list(
        db_session, name="现场清单", store_id=shop.id, product_ids={item.id}, source_type="store_history",
    )
    restock_item = restock_list.items[0]
    before_batches = db_session.scalar(select(func.count()).select_from(PurchaseBatch))
    before_items = db_session.scalar(select(func.count()).select_from(PurchaseBatchItem))

    update_restock_item(
        db_session, restock_item.id, status="purchased",
        actual_quantity="1", actual_price="1190", planned_quantity="", notes="现场临时",
    )
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == before_batches
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatchItem)) == before_items

    linked = link_purchase_item(db_session, restock_item.id, purchase_item.id)
    assert linked.purchase_batch_item_id == purchase_item.id
    assert linked.actual_purchase_quantity == 2 and linked.actual_purchase_price == 1200
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == before_batches


def test_cancelled_purchase_and_cancelled_list_are_excluded_and_copy_resets_results(db_session):
    shop = store(db_session, "D")
    cancelled_only = product(db_session, "5")
    add_purchase(db_session, cancelled_only, shop, status="cancelled")
    assert restock_candidates(db_session, shop.id, now=NOW) == []

    item = product(db_session, "6")
    add_purchase(db_session, item, shop)
    restock_list = create_restock_list(
        db_session, name="待复制清单", store_id=shop.id, product_ids={item.id}, source_type="store_history",
    )
    update_restock_item(
        db_session, restock_list.items[0].id, status="purchased",
        actual_quantity="3", actual_price="499", planned_quantity="", notes="",
    )
    restock_list.status = "cancelled"
    db_session.commit()
    assert recent_lists_for_store(db_session, shop.id) == []

    restock_list.status = "completed"
    db_session.commit()
    copied = copy_restock_list(db_session, restock_list.id)
    assert copied.status == "draft"
    assert copied.items[0].actual_purchase_quantity is None
    assert copied.items[0].purchase_batch_item_id is None


def test_restock_pages_and_bidirectional_trace_return_200(client):
    http, db, _ = client
    shop = store(db, "E")
    item = product(db, "7")
    add_purchase(db, item, shop, price=888)
    restock_list = create_restock_list(
        db, name="页面清单", store_id=shop.id, product_ids={item.id}, source_type="store_history",
    )

    traces = trace_rows(db, restock_list)
    assert traces[restock_list.items[0].id][0].item.product_id == item.id

    assert http.get("/restock-lists").status_code == 200
    assert http.get(f"/restock-lists/new?store_id={shop.id}").status_code == 200
    detail = http.get(f"/restock-lists/{restock_list.id}")
    store_page = http.get(f"/stores/{shop.id}")
    product_page = http.get(f"/products/{item.id}")
    assert detail.status_code == store_page.status_code == product_page.status_code == 200
    assert "查看原小票" in detail.text or "原小票" in detail.text
    assert "补货清单" in store_page.text and "补货清单" in product_page.text
