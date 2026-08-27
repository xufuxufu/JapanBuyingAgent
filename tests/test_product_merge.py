from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    FieldPurchaseBatch,
    FieldPurchaseItem,
    ImportJob,
    ImportRow,
    InventoryTransaction,
    Marketplace,
    PriceLookupHistory,
    PriceSearchRun,
    Product,
    ProductAlias,
    ProductBarcode,
    ProductEnrichmentTask,
    ProductOffer,
    ProductSerial,
    ProductWatchConfig,
    ProductWatchNotification,
    ProductWatchRecommendation,
    ProductWatchSnapshot,
    PurchaseBatch,
    PurchaseBatchItem,
    QinsiExportJob,
    QinsiExportLine,
    QinsiGoodsImportRow,
    QinsiImportBatch,
    QinsiInventorySnapshot,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportJob,
    QinsiPurchaseExportLine,
    Receipt,
    ReceiptBatch,
    ReceiptItem,
    RestockList,
    RestockListItem,
    Store,
)
from app.product_merge import list_duplicate_jan_groups, merge_duplicate_jan_group


def _allow_legacy_duplicate_jans(db_session) -> None:
    db_session.connection().exec_driver_sql("DROP INDEX IF EXISTS uq_products_jan_not_null")
    db_session.connection().exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_products_jan_not_null ON products (jan) WHERE jan IS NOT NULL"
    )


def test_qinsi_product_wins_duplicate_jan_merge_and_migrates_business_links(db_session):
    _allow_legacy_duplicate_jans(db_session)
    jan = "4571609352419"
    primary = Product(
        jan=jan,
        qinsi_product_code="QINSI-PRIMARY",
        name_cn="秦丝主商品",
        status="qinsi_product_imported",
        product_origin="qinsi",
    )
    duplicate = Product(
        jan=jan,
        name_cn="本地重复商品",
        status="new_pending_review",
        product_origin="receipt",
    )
    db_session.add_all([primary, duplicate])
    db_session.flush()
    primary_id = primary.id
    duplicate_id = duplicate.id
    locations = initialize_default_locations(db_session, commit=False)
    initial_location = locations[0]
    store = Store(name="测试店")
    receipt_batch = ReceiptBatch(batch_no="RCPT-MERGE-TEST", image_count=1)
    receipt = Receipt(batch=receipt_batch, raw_store_name="测试店", confirmation_status="confirmed")
    receipt_item = ReceiptItem(
        receipt=receipt,
        line_no=1,
        raw_name="本地重复商品",
        jan_candidate=jan,
        product_id=duplicate_id,
        match_status="matched_existing",
        quantity=2,
        unit_price=100,
        line_total=200,
    )
    purchase_batch = PurchaseBatch(
        batch_no="PB-MERGE-TEST",
        receipt=receipt,
        gpt_batch=receipt_batch,
        default_initial_location_id=initial_location.id,
        default_qinsi_warehouse_id=initial_location.id,
        confirmed_at=datetime.now(timezone.utc),
    )
    purchase_item = PurchaseBatchItem(
        purchase_batch=purchase_batch,
        product_id=duplicate_id,
        receipt_item=receipt_item,
        quantity=2,
        unit_price=100,
        actual_line_amount=200,
        initial_location_id=initial_location.id,
        qinsi_target_warehouse_id=initial_location.id,
    )
    qinsi_purchase_job = QinsiPurchaseExportJob(
        export_no="QPE-MERGE",
        selection_key="QPE-MERGE",
        export_type="restock",
        purchase_batch=purchase_batch,
        qinsi_target_warehouse_id=initial_location.id,
        filename="merge.xlsx",
        file_content=b"xlsx",
        line_count=1,
    )
    qinsi_purchase_line = QinsiPurchaseExportLine(
        export_job=qinsi_purchase_job,
        purchase_batch=purchase_batch,
        purchase_batch_item=purchase_item,
        receipt=receipt,
        receipt_item=receipt_item,
        product_id=duplicate_id,
        qinsi_target_warehouse_id=initial_location.id,
        row_no=1,
        internal_sku="NJ-DUP",
        jan=jan,
        qinsi_product_code="LOCAL-OLD",
        product_name="本地重复商品",
        quantity=2,
        purchase_price=100,
    )
    qinsi_export_job = QinsiExportJob(status="pending")
    qinsi_export_line = QinsiExportLine(
        job_id=1,
        product_id=duplicate_id,
        qinsi_product_code="LOCAL-OLD",
        product_name="本地重复商品",
        quantity=2,
    )
    inventory_snapshot = QinsiInventorySnapshot(
        batch_no="QINV-MERGE",
        original_filename="inventory.xlsx",
        file_hash="inventory-merge",
        file_content=b"xlsx",
        total_rows=1,
        success_rows=1,
        status="completed",
    )
    inventory_line = QinsiInventorySnapshotLine(
        snapshot=inventory_snapshot,
        original_row_no=1,
        jan=jan,
        qinsi_product_code="LOCAL-OLD",
        quantity=2,
        raw_summary_json="{}",
        product_id=duplicate_id,
        warehouse_id=initial_location.id,
        matching_status="matched",
    )
    qinsi_import_batch = QinsiImportBatch(
        source_system="qinsi",
        original_filename="goods.xlsx",
        file_hash="goods-merge",
        file_content=b"xlsx",
        status="completed",
    )
    qinsi_import_row = QinsiGoodsImportRow(
        import_batch_id=1,
        source_file_name="goods.xlsx",
        excel_row_number=1,
        parsed_data="{}",
        raw_json="{}",
        validation_status="imported",
        product_id=duplicate_id,
    )
    import_job = ImportJob(job_type="product", status="completed")
    import_row = ImportRow(import_job_id=1, row_no=1, raw_json="{}", status="success", product_id=duplicate_id)
    field_batch = FieldPurchaseBatch(batch_no="FP-MERGE", client_request_id="fp-merge", operator_name="采购员A")
    field_item = FieldPurchaseItem(
        batch=field_batch,
        product_id=duplicate_id,
        jan=jan,
        quantity=2,
        status="CONFIRMED",
        captured_by="采购员A",
    )
    restock_list = RestockList(name="补货清单", store=store)
    restock_item = RestockListItem(restock_list=restock_list, product_id=duplicate_id, planned_quantity=2)
    marketplace = Marketplace(code="rakuten-test", name="Rakuten")
    price_run = PriceSearchRun(product_id=duplicate_id, jan=jan, status="completed")
    offer = ProductOffer(
        search_run=price_run,
        marketplace=marketplace,
        product_id=duplicate_id,
        jan=jan,
        url="https://example.test/item",
        item_price=100,
        shipping_price=0,
        total_price=100,
        fetched_at=datetime.now(timezone.utc),
    )
    lookup = PriceLookupHistory(search_run=price_run, product_id=duplicate_id, jan=jan)
    watch_config = ProductWatchConfig(product_id=duplicate_id, enabled=True)
    watch_snapshot = ProductWatchSnapshot(
        watch_config=watch_config,
        product_id=duplicate_id,
        status="success",
        result_count=1,
    )
    watch_notification = ProductWatchNotification(
        watch_config=watch_config,
        product_id=duplicate_id,
        event_type="target_reached",
        dedupe_key="merge-notification",
        triggered_at=datetime.now(timezone.utc),
        data_updated_at=datetime.now(timezone.utc),
    )
    watch_recommendation = ProductWatchRecommendation(product_id=duplicate_id, reason="scan_count")
    enrichment_task = ProductEnrichmentTask(jan=jan, status="completed", trigger_source="test", product_id=duplicate_id)
    db_session.add_all([
        store,
        receipt_batch,
        receipt,
        receipt_item,
        purchase_batch,
        purchase_item,
        qinsi_purchase_job,
        qinsi_purchase_line,
        qinsi_export_job,
        qinsi_export_line,
        inventory_snapshot,
        inventory_line,
        QinsiProductMapping(qinsi_product_code="LOCAL-OLD", product_id=duplicate_id),
        qinsi_import_batch,
        qinsi_import_row,
        import_job,
        import_row,
        field_batch,
        field_item,
        restock_list,
        restock_item,
        ProductSerial(product_id=duplicate_id, serial_value="SERIAL-MERGE"),
        ProductAlias(product_id=duplicate_id, alias="本地别名", normalized_alias="本地别名"),
        ProductBarcode(product_id=duplicate_id, barcode="BARCODE-MERGE"),
        InventoryTransaction(product_id=duplicate_id, transaction_type="receipt_purchase", quantity=2),
        marketplace,
        price_run,
        offer,
        lookup,
        watch_config,
        watch_snapshot,
        watch_notification,
        watch_recommendation,
        enrichment_task,
    ])
    db_session.flush()
    qinsi_export_line.job_id = qinsi_export_job.id
    qinsi_import_row.import_batch_id = qinsi_import_batch.id
    import_row.import_job_id = import_job.id
    db_session.commit()

    group = list_duplicate_jan_groups(db_session)[0]
    assert group.recommended_primary.id == primary_id

    result = merge_duplicate_jan_group(db_session, jan, actor="pytest")

    assert result is not None
    assert result.primary_product_id == primary_id
    assert duplicate_id in result.merged_product_ids
    assert db_session.get(Product, duplicate_id) is None
    assert db_session.scalar(select(func.count()).select_from(Product).where(Product.jan == jan)) == 1
    for model in (
        ReceiptItem,
        PurchaseBatchItem,
        QinsiPurchaseExportLine,
        QinsiInventorySnapshotLine,
        QinsiProductMapping,
        QinsiGoodsImportRow,
        ImportRow,
        FieldPurchaseItem,
        RestockListItem,
        ProductSerial,
        ProductAlias,
        ProductBarcode,
        InventoryTransaction,
        PriceSearchRun,
        ProductOffer,
        PriceLookupHistory,
        ProductWatchConfig,
        ProductWatchSnapshot,
        ProductWatchNotification,
        ProductWatchRecommendation,
        ProductEnrichmentTask,
    ):
        assert db_session.scalar(select(func.count()).select_from(model).where(model.product_id == duplicate_id)) == 0
        assert db_session.scalar(select(func.count()).select_from(model).where(model.product_id == primary_id)) >= 1
    assert db_session.scalar(select(ReceiptItem.product_id).where(ReceiptItem.id == receipt_item.id)) == primary_id
    assert db_session.scalar(select(PurchaseBatchItem.product_id).where(PurchaseBatchItem.id == purchase_item.id)) == primary_id
    assert db_session.scalar(select(QinsiExportLine.product_id).where(QinsiExportLine.id == qinsi_export_line.id)) == primary_id

    db_session.connection().exec_driver_sql("DROP INDEX IF EXISTS ix_products_jan_not_null")
    db_session.connection().exec_driver_sql(
        "CREATE UNIQUE INDEX uq_products_jan_not_null ON products (jan) WHERE jan IS NOT NULL"
    )
    indexes = {row[1]: row[2] for row in db_session.connection().exec_driver_sql("PRAGMA index_list('products')")}
    assert indexes["uq_products_jan_not_null"] == 1
