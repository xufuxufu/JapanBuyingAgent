from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import Location, Product, PurchaseBatch, PurchaseBatchItem, Receipt, ReceiptBatch, ReceiptItem, Store, StoreAlias
from app.purchase_service import ensure_purchase_batch_for_receipt
from app.schemas import PurchaseConfirmationInput
from app.schemas import StoreBrandCreateInput, StoreCreateInput
from app.services import confirm_receipt
from app.store_service import confirm_receipt_store, create_store, create_store_brand, match_receipt_store, product_store_summaries, store_product_summaries


def make_confirmable_receipt(db, *, item_count: int = 2, duplicate_status: str = "none"):
    locations = initialize_default_locations(db)
    with_jan = Product(jan="4901234567894", name_cn="有JAN商品")
    without_jan = Product(name_cn="无JAN商品")
    extra = Product(jan="4570110290418", name_cn="第二个有JAN商品")
    db.add_all([with_jan, without_jan, extra])
    db.flush()
    batch = ReceiptBatch(batch_no=f"PURCHASE-TEST-{id(db)}-{item_count}", status="review", image_status="ready", gpt_status="json_imported")
    receipt = Receipt(
        batch=batch, raw_store_name="采购测试店", purchased_at=datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc),
        confirmation_status="pending", review_status="pending", duplicate_status=duplicate_status,
    )
    products = [with_jan, without_jan, extra][:item_count]
    for line_no, product in enumerate(products, 1):
        receipt.items.append(ReceiptItem(
            line_no=line_no, raw_name=product.name_cn, jan_candidate=product.jan,
            product_id=product.id, match_method="manual", match_status="matched_existing",
            quantity=line_no, unit_price=100 * line_no, discount_amount=10,
            line_total=90 * line_no, confidence=1, review_status="pending",
        ))
    db.add(receipt)
    db.commit()
    return batch, receipt, products, {location.display_name: location for location in locations}


def test_confirmation_creates_one_batch_and_one_detail_per_receipt_item_with_defaults(db_session):
    batch, receipt, products, locations = make_confirmable_receipt(db_session)
    confirm_receipt(db_session, batch, receipt)
    purchase = db_session.scalar(select(PurchaseBatch))
    details = list(db_session.scalars(select(PurchaseBatchItem).order_by(PurchaseBatchItem.receipt_item_id)))
    assert purchase is not None and purchase.receipt_id == receipt.id and purchase.gpt_batch_id == batch.id
    assert purchase.purchased_at.replace(tzinfo=timezone.utc) == receipt.purchased_at and purchase.store_name == receipt.raw_store_name
    assert purchase.confirmed_at.replace(tzinfo=timezone.utc) == receipt.confirmed_at and purchase.status == "confirmed"
    assert len(details) == len(receipt.items) == 2
    assert [detail.receipt_item_id for detail in details] == [item.id for item in receipt.items]
    assert [detail.product_id for detail in details] == [product.id for product in products]
    assert all(detail.initial_location_id == locations["日本家里库存"].id for detail in details)
    assert details[0].qinsi_target_warehouse_id == locations["日本家里库存"].id
    assert details[1].qinsi_target_warehouse_id == locations["无条码商品"].id
    assert (details[0].quantity, details[0].unit_price, details[0].discount_amount, details[0].actual_line_amount) == (1, 100, 10, 90)


def test_repeated_confirmation_and_ensure_do_not_duplicate_batch(db_session):
    batch, receipt, _, _ = make_confirmable_receipt(db_session)
    confirm_receipt(db_session, batch, receipt)
    first = ensure_purchase_batch_for_receipt(db_session, receipt)
    confirm_receipt(db_session, batch, receipt)
    second = ensure_purchase_batch_for_receipt(db_session, receipt)
    db_session.commit()
    assert first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == 1
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatchItem)) == 2


def test_batch_level_locations_apply_to_all_lines(db_session):
    batch, receipt, _, locations = make_confirmable_receipt(db_session)
    settings = PurchaseConfirmationInput(
        initial_location_id=locations["2025千羽"].id,
        qinsi_target_warehouse_id=locations["新日本仓库"].id,
    )
    confirm_receipt(db_session, batch, receipt, settings)
    details = list(db_session.scalars(select(PurchaseBatchItem)))
    assert all(detail.initial_location_id == locations["2025千羽"].id for detail in details)
    assert all(detail.qinsi_target_warehouse_id == locations["新日本仓库"].id for detail in details)


def test_line_level_qinsi_target_override_wins_over_batch_setting(client):
    http, db, _ = client
    batch, receipt, _, locations = make_confirmable_receipt(db, item_count=3)
    override_item = receipt.items[2]
    review = http.get(f"/receipts/{batch.id}/review?receipt_id={receipt.id}")
    assert review.status_code == 200 and "采购批次默认位置" in review.text and "异常行单独覆盖目标仓" in review.text
    response = http.post(f"/receipts/{batch.id}/review/confirm", data={
        "receipt_id": str(receipt.id),
        "initial_location_id": str(locations["日本家里库存"].id),
        "qinsi_target_warehouse_id": str(locations["新日本仓库"].id),
        f"line_qinsi_target_{override_item.id}": str(locations["无条码商品"].id),
    }, follow_redirects=False)
    assert response.status_code == 303
    details = list(db.scalars(select(PurchaseBatchItem).order_by(PurchaseBatchItem.receipt_item_id)))
    assert details[0].qinsi_target_warehouse_id == details[1].qinsi_target_warehouse_id == locations["新日本仓库"].id
    assert details[2].qinsi_target_warehouse_id == locations["无条码商品"].id
    assert details[2].target_warehouse_overridden is True


def test_purchase_batch_traces_receipt_items_and_product_detail_reverse_lookup(client):
    http, db, _ = client
    batch, receipt, products, _ = make_confirmable_receipt(db)
    confirm_receipt(db, batch, receipt)
    purchase = db.scalar(select(PurchaseBatch))
    listing = http.get("/purchase-batches")
    detail = http.get(f"/purchase-batches/{purchase.id}")
    product_page = http.get(f"/products/{products[0].id}")
    assert listing.status_code == detail.status_code == product_page.status_code == 200
    assert purchase.batch_no in listing.text and batch.batch_no in detail.text
    assert f"#receipt-item-{receipt.items[0].id}" in detail.text
    assert purchase.batch_no in product_page.text and "不代表实时可售库存" in product_page.text


def test_cancelled_or_abnormal_receipt_does_not_generate_purchase_batch(db_session):
    batch, receipt, _, _ = make_confirmable_receipt(db_session, duplicate_status="review_required")
    confirm_receipt(db_session, batch, receipt)
    assert db_session.scalar(select(PurchaseBatch)) is None
    receipt.confirmation_status = "cancelled"
    receipt.review_status = "reviewed"
    receipt.confirmed_at = datetime.now(timezone.utc)
    db_session.commit()
    assert ensure_purchase_batch_for_receipt(db_session, receipt) is None


def test_store_creation_deduplicates_phone_and_code_and_exact_match_is_high_confidence(db_session):
    brand = create_store_brand(db_session, StoreBrandCreateInput(name_cn="测试连锁", name_ja="テストチェーン"))
    first = create_store(db_session, StoreCreateInput(
        brand_id=brand.id, name_cn="新宿店", name_ja="新宿店", phone="03-1234-5678", receipt_store_code="S001",
    ))
    same_phone = create_store(db_session, StoreCreateInput(name_cn="重复电话店", phone="0312345678"))
    same_code = create_store(db_session, StoreCreateInput(name_cn="重复代码店", receipt_store_code="S001"))
    assert first.id == same_phone.id == same_code.id
    assert db_session.scalar(select(func.count()).select_from(Store)) == 1

    batch = ReceiptBatch(batch_no="STORE-MATCH-CODE", status="review", image_status="ready", gpt_status="json_imported")
    receipt = Receipt(batch=batch, raw_store_name="任意原始名称", raw_store_code="S001")
    db_session.add(receipt)
    db_session.flush()
    assert match_receipt_store(db_session, receipt).id == first.id
    assert receipt.store_match_method == "store_code" and receipt.store_match_confidence == 1


def test_fuzzy_name_does_not_merge_but_confirmed_alias_matches_next_receipt(db_session):
    store = create_store(db_session, StoreCreateInput(name_cn="中央店", name_ja="中央店"))
    create_store(db_session, StoreCreateInput(name_cn="中央东店", name_ja="中央東店"))
    first_batch = ReceiptBatch(batch_no="STORE-ALIAS-ONE", status="review", image_status="ready", gpt_status="json_imported")
    first = Receipt(batch=first_batch, raw_store_name="中央附近分店")
    db_session.add(first)
    db_session.flush()
    assert match_receipt_store(db_session, first) is None
    assert first.store_id is None and first.store_match_status == "pending"

    confirm_receipt_store(db_session, first, store)
    assert db_session.scalar(select(func.count()).select_from(StoreAlias)) == 1
    second_batch = ReceiptBatch(batch_no="STORE-ALIAS-TWO", status="review", image_status="ready", gpt_status="json_imported")
    second = Receipt(batch=second_batch, raw_store_name="中央附近分店")
    db_session.add(second)
    db_session.flush()
    assert match_receipt_store(db_session, second).id == store.id
    assert second.store_match_method == "confirmed_alias"


def test_product_store_reverse_tracking_pages_and_cancelled_exclusion(client):
    http, db, _ = client
    store = create_store(db, StoreCreateInput(name_cn="采购门店", name_ja="仕入店舗", phone="03-9999-0000"))
    batch, receipt, products, locations = make_confirmable_receipt(db, item_count=1)
    confirm_receipt_store(db, receipt, store)
    confirm_receipt(db, batch, receipt)
    purchase = db.scalar(select(PurchaseBatch))
    assert purchase.store_id == store.id

    cancelled_batch = ReceiptBatch(batch_no="STORE-CANCELLED-SOURCE", status="confirmed", image_status="ready", gpt_status="reviewed")
    cancelled_receipt = Receipt(
        batch=cancelled_batch, raw_store_name="采购门店", store_id=store.id,
        purchased_at=datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc), confirmation_status="confirmed", review_status="reviewed",
    )
    cancelled_item = ReceiptItem(
        receipt=cancelled_receipt, line_no=1, raw_name="取消采购", product_id=products[0].id,
        quantity=5, unit_price=999, line_total=4995, confidence=1, review_status="confirmed",
    )
    db.add_all([cancelled_receipt, cancelled_item])
    db.flush()
    cancelled_purchase = PurchaseBatch(
        batch_no="PB-CANCELLED-STORE", receipt_id=cancelled_receipt.id, gpt_batch_id=cancelled_batch.id,
        purchased_at=cancelled_receipt.purchased_at, store_name=cancelled_receipt.raw_store_name, store_id=store.id,
        confirmed_at=datetime.now(timezone.utc), status="cancelled",
        default_initial_location_id=locations["日本家里库存"].id,
    )
    db.add(cancelled_purchase)
    db.flush()
    db.add(PurchaseBatchItem(
        purchase_batch_id=cancelled_purchase.id, product_id=products[0].id, receipt_item_id=cancelled_item.id,
        quantity=5, unit_price=999, actual_line_amount=4995,
        initial_location_id=locations["日本家里库存"].id,
        qinsi_target_warehouse_id=locations["日本家里库存"].id,
    ))
    db.commit()

    product_rows = product_store_summaries(db, products[0].id)
    store_rows, facts = store_product_summaries(db, store.id)
    assert len(product_rows) == len(store_rows) == 1
    assert product_rows[0]["purchase_count"] == 1 and product_rows[0]["quantity"] == 1
    assert store_rows[0]["purchase_count"] == 1 and store_rows[0]["total_amount"] == 90
    assert len(facts) == 1 and facts[0].batch.id == purchase.id

    product_page = http.get(f"/products/{products[0].id}")
    stores_page = http.get("/stores")
    store_page = http.get(f"/stores/{store.id}")
    assert product_page.status_code == stores_page.status_code == store_page.status_code == 200
    assert f"/stores/{store.id}" in product_page.text
    assert f"/products/{products[0].id}" in store_page.text
    assert purchase.batch_no in store_page.text and "PB-CANCELLED-STORE" not in store_page.text
