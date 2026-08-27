from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.product_admin as product_admin
from app.models import (
    Location,
    Product,
    ProductPlaceholderCleanupLog,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiExportJob,
    QinsiExportLine,
    QinsiPurchaseExportJob,
    QinsiPurchaseExportLine,
    Receipt,
    ReceiptBatch,
    ReceiptItem,
)


def _ean13(stem12: str) -> str:
    digits = [int(value) for value in stem12]
    checksum = (10 - ((sum(digits[::2]) + 3 * sum(digits[1::2])) % 10)) % 10
    return stem12 + str(checksum)


def _formal(jan: str, goods_no: str, name: str = "正式秦丝商品") -> Product:
    return Product(
        jan=jan,
        qinsi_product_code=goods_no,
        qinsi_name=name,
        name_cn=name,
        image_url="https://img.example.test/qinsi.jpg",
        purchase_price=550,
        source="qinsi_import",
        product_origin="qinsi",
        status="qinsi_product_imported",
    )


def _placeholder(jan: str, name: str = "缺商品") -> Product:
    return Product(
        jan=jan,
        name_cn=name,
        source="receipt",
        product_origin="receipt",
        status="new_pending_completion",
        needs_review=True,
    )


def _purchase_links(db, product: Product) -> tuple[ReceiptItem, PurchaseBatchItem, QinsiPurchaseExportLine, QinsiExportLine]:
    location = Location(internal_code=f"LOC-{product.id}", display_name="测试仓", location_type="local_physical")
    batch = ReceiptBatch(batch_no=f"RB-{product.id}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(batch=batch, raw_store_name="测试店", confirmation_status="confirmed", review_status="reviewed")
    db.add_all([location, receipt])
    db.flush()
    receipt_item = ReceiptItem(
        receipt_id=receipt.id,
        line_no=1,
        raw_name="小票原始名",
        jan_candidate=product.jan,
        product_id=product.id,
        match_status="new_product",
        quantity=2,
        unit_price=275,
        discount_amount=0,
        line_total=550,
        confidence=1,
        review_status="confirmed",
    )
    db.add(receipt_item)
    db.flush()
    purchase_batch = PurchaseBatch(
        batch_no=f"PB-{product.id}",
        receipt_id=receipt.id,
        gpt_batch_id=batch.id,
        purchased_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        confirmed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        default_initial_location_id=location.id,
        default_qinsi_warehouse_id=location.id,
    )
    db.add(purchase_batch)
    db.flush()
    purchase_item = PurchaseBatchItem(
        purchase_batch_id=purchase_batch.id,
        product_id=product.id,
        receipt_item_id=receipt_item.id,
        quantity=2,
        unit_price=275,
        discount_amount=0,
        actual_line_amount=550,
        initial_location_id=location.id,
        qinsi_target_warehouse_id=location.id,
    )
    db.add(purchase_item)
    db.flush()
    purchase_job = QinsiPurchaseExportJob(
        export_no=f"QPE-{product.id}",
        selection_key=f"QPE-{product.id}",
        export_type="restock",
        purchase_batch_id=purchase_batch.id,
        qinsi_target_warehouse_id=location.id,
        filename="purchase.xlsx",
        file_content=b"xlsx",
        line_count=1,
    )
    product_job = QinsiExportJob(status="pending")
    db.add_all([purchase_job, product_job])
    db.flush()
    purchase_export_line = QinsiPurchaseExportLine(
        export_job_id=purchase_job.id,
        purchase_batch_id=purchase_batch.id,
        purchase_batch_item_id=purchase_item.id,
        receipt_id=receipt.id,
        receipt_item_id=receipt_item.id,
        product_id=product.id,
        qinsi_target_warehouse_id=location.id,
        row_no=1,
        internal_sku=product.internal_sku,
        jan=product.jan,
        qinsi_product_code="OLD-TEMP",
        product_name="缺商品",
        quantity=2,
        purchase_price=275,
    )
    product_export_line = QinsiExportLine(
        job_id=product_job.id,
        product_id=product.id,
        qinsi_product_code="OLD-TEMP",
        product_name="缺商品",
        quantity=2,
        purchase_price=275,
    )
    db.add_all([purchase_export_line, product_export_line])
    db.commit()
    return receipt_item, purchase_item, purchase_export_line, product_export_line


def test_preview_and_confirm_deletes_unique_unlinked_placeholder_and_health(client):
    http, db, _ = client
    jan = _ean13("490000000001")
    temp = _placeholder(jan)
    db.add(temp)
    db.commit()
    temp_id = temp.id
    db.add(_formal(jan, "QINSI-001"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    preview = http.get("/products/placeholder-cleanup")
    assert preview.status_code == 200
    assert "清理临时缺商品" in preview.text

    response = http.post("/products/placeholder-cleanup/confirm", data={"actor": "pytest"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get(Product, temp_id) is not None
    assert db.scalar(select(ProductPlaceholderCleanupLog).where(ProductPlaceholderCleanupLog.jan == jan)) is None
    assert http.get("/health").status_code == 200


def test_unique_formal_migrates_purchase_links_preserves_facts_no_copy_and_458_case(client):
    _, db, _ = client
    jan = "4580805230308"
    temp = _placeholder(jan)
    db.add(temp)
    db.commit()
    temp_id = temp.id
    receipt_item, purchase_item, purchase_export_line, product_export_line = _purchase_links(db, temp)
    before_purchase_item_count = db.scalar(select(func.count()).select_from(PurchaseBatchItem))
    db.add(_formal(jan, "QINSI-458", "秦丝正式 458 商品"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    result = product_admin.execute_placeholder_cleanup(db, actor="pytest")

    assert result.migrated_product_count == 0
    assert result.deleted_product_count == 0
    assert result.migrated_association_count == 0
    assert db.get(Product, temp_id) is not None
    assert db.scalar(select(func.count()).select_from(Product).where(Product.jan == jan)) == 1
    assert db.get(ReceiptItem, receipt_item.id).product_id == temp_id
    migrated_purchase_item = db.get(PurchaseBatchItem, purchase_item.id)
    assert migrated_purchase_item.product_id == temp_id
    assert migrated_purchase_item.quantity == 2
    assert migrated_purchase_item.unit_price == 275
    assert migrated_purchase_item.actual_line_amount == 550
    assert migrated_purchase_item.purchase_batch_id == purchase_item.purchase_batch_id
    assert db.scalar(select(func.count()).select_from(PurchaseBatchItem)) == before_purchase_item_count
    assert db.get(QinsiPurchaseExportLine, purchase_export_line.id).product_id == temp_id
    assert db.get(QinsiExportLine, product_export_line.id).product_id == temp_id


def test_multiple_formal_same_jan_and_no_formal_are_retained(db_session):
    jan_multi = _ean13("490000000002")
    formal_a = _formal(jan_multi, "QINSI-A", "颜色A")
    formal_b = _formal(jan_multi, "QINSI-B", "颜色B")
    jan_no_formal = _ean13("490000000003")
    temp_no_formal = _placeholder(jan_no_formal)
    db_session.add_all([formal_a, temp_no_formal])
    db_session.commit()
    db_session.add(formal_b)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    preview = product_admin.preview_placeholder_cleanup(db_session)
    actions = {row.old_product_id: row.action for row in preview.rows}
    assert actions[temp_no_formal.id] == "keep_no_formal"

    result = product_admin.execute_placeholder_cleanup(db_session)
    assert result.deleted_product_count == 0
    assert db_session.get(Product, temp_no_formal.id) is not None
    assert len(list(db_session.scalars(select(Product).where(Product.jan == jan_multi)))) == 1
    assert db_session.get(Product, formal_a.id).qinsi_product_code == "QINSI-A"


def test_cleanup_failure_rolls_back_migrated_purchase_links(db_session, monkeypatch):
    jan = _ean13("490000000004")
    temp = _placeholder(jan)
    db_session.add(temp)
    db_session.commit()
    temp_id = temp.id
    receipt_item, purchase_item, _, _ = _purchase_links(db_session, temp)
    db_session.add(_formal(jan, "QINSI-ROLLBACK"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    assert db_session.get(Product, temp_id) is not None
    assert db_session.get(ReceiptItem, receipt_item.id).product_id == temp_id
    assert db_session.get(PurchaseBatchItem, purchase_item.id).product_id == temp_id
