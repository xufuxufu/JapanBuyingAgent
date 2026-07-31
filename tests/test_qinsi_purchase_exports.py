from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    PriceProviderAttempt, Product, PurchaseBatch, PurchaseBatchItem, QinsiExportJob, QinsiPurchaseExportJob,
    QinsiPurchaseExportLine, QinsiPurchaseExportLineSource, Receipt, ReceiptBatch, ReceiptItem,
)
from app.qinsi_export import (
    QINSI_GOODS_TEMPLATE_HEADERS, QINSI_PURCHASE_TEMPLATE_HEADERS,
    cancel_qinsi_product_export_confirmation, confirm_qinsi_export,
    confirm_qinsi_product_export, create_qinsi_product_export,
    generate_purchase_batch_exports, retry_failed_qinsi_lines,
)
from app.schemas import QinsiExportConfirmationInput


def make_purchase(db, products: list[dict], *, same_warehouse: bool = False):
    locations = {location.display_name: location for location in initialize_default_locations(db)}
    models = [Product(
        name_cn=values["name"], name_ja=values.get("name_ja", "日本語名"), jan=values.get("jan"),
        qinsi_product_code=values.get("code"), product_origin=values.get("origin", "manual"),
        status=values.get("status", "qinsi_product_imported" if values.get("origin") == "qinsi" else "new_pending_review"),
        purchase_price=values.get("purchase_price", 120), sale_price=values.get("sale_price", 180),
        main_image_source_url=values.get("image_url"),
    ) for values in products]
    db.add_all(models)
    db.flush()
    gpt_batch = ReceiptBatch(batch_no=f"QINSI-EXPORT-{id(db)}-{len(models)}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(
        batch=gpt_batch, raw_store_name="秦丝闭环测试店", purchased_at=datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc),
        raw_store_address="东京都测试区一丁目",
        confirmation_status="confirmed", review_status="reviewed", confirmed_at=datetime.now(timezone.utc),
    )
    db.add(receipt)
    db.flush()
    purchase = PurchaseBatch(
        batch_no=f"PB-QINSI-{receipt.id:08d}", receipt_id=receipt.id, gpt_batch_id=gpt_batch.id,
        purchased_at=receipt.purchased_at, store_name=receipt.raw_store_name, confirmed_at=receipt.confirmed_at,
        status="confirmed", default_initial_location_id=locations["日本家里库存"].id,
        default_qinsi_warehouse_id=locations["新日本仓库"].id,
    )
    db.add(purchase)
    db.flush()
    for index, product in enumerate(models, 1):
        receipt_item = ReceiptItem(
            receipt_id=receipt.id, line_no=index, raw_name=product.name_cn, jan_candidate=product.jan,
            product_id=product.id, match_status="matched_existing", quantity=index, unit_price=100 + index,
            discount_amount=0, line_total=(100 + index) * index, confidence=1, review_status="confirmed",
        )
        db.add(receipt_item)
        db.flush()
        if same_warehouse:
            warehouse = locations["新日本仓库"]
        elif not product.jan:
            warehouse = locations["无条码商品"]
        elif product.product_origin == "qinsi":
            warehouse = locations["新日本仓库"]
        else:
            warehouse = locations["日本家里库存"]
        db.add(PurchaseBatchItem(
            purchase_batch_id=purchase.id, product_id=product.id, receipt_item_id=receipt_item.id,
            quantity=index, unit_price=100 + index, discount_amount=0, actual_line_amount=(100 + index) * index,
            initial_location_id=locations["日本家里库存"].id, qinsi_target_warehouse_id=warehouse.id,
        ))
    db.commit()
    return purchase, models, locations


def workbook(content: bytes):
    return load_workbook(BytesIO(content), data_only=True)


def test_new_existing_jan_no_jan_warehouse_template_and_full_source_tracking(client):
    _, db, _ = client
    missing = Product(jan="4901234567894", name_cn="4901234567894", name_ja="缺商品", status="new_pending_completion")
    rich = Product(
        jan="4570110290418", name_cn="中文商品", name_ja="日本語商品", status="new_pending_review",
        purchase_price=Decimal("980"), main_image_source_url="https://img.test/first.jpg",
        local_image_path="data/products/qinsi-localized/local.jpg",
        display_image_url="/product-local-images/123?v=local",
    )
    db.add_all([missing, rich])
    db.commit()
    job = create_qinsi_product_export(db, {missing.id, rich.id})
    book = workbook(job.file_content)
    assert book.sheetnames == ["商品导入", "配置"]
    sheet = book["商品导入"]
    assert tuple(cell.value for cell in sheet[1]) == QINSI_GOODS_TEMPLATE_HEADERS
    rows = {sheet.cell(row, 3).value: row for row in range(2, 4)}
    missing_row, rich_row = rows[missing.jan], rows[rich.jan]
    assert sheet.cell(missing_row, 1).value == f"{missing.jan}|缺商品"
    assert sheet.cell(rich_row, 1).value == "中文商品|日本語商品"
    assert sheet.cell(rich_row, 2).value == rich.jan and sheet.cell(rich_row, 3).value == rich.jan
    assert sheet.cell(rich_row, 2).data_type == "s" and sheet.cell(rich_row, 3).data_type == "s"
    assert sheet.cell(rich_row, 7).value == "个"
    assert sheet.cell(rich_row, 8).value == 980
    assert sheet.cell(rich_row, 9).value == 980
    assert sheet.cell(rich_row, 8).value == sheet.cell(rich_row, 9).value
    assert sheet.cell(rich_row, 8).data_type == "n" and sheet.cell(rich_row, 9).data_type == "n"
    assert sheet.cell(missing_row, 8).value is None
    assert sheet.cell(missing_row, 9).value is None
    assert sheet.cell(missing_row, 10).value is None
    assert sheet.cell(rich_row, 10).value is None
    assert all(sheet.cell(row, column).value not in (0, "0", "0.00") for row in (missing_row, rich_row) for column in (8, 9, 10))
    assert sheet.cell(rich_row, 11).value == 100 and sheet.cell(rich_row, 12).value == "启用"
    assert sheet.cell(rich_row, 13).value == "启用" and sheet.cell(rich_row, 17).value == "停用"
    assert sheet.cell(rich_row, 19).value == "https://img.test/first.jpg" and sheet.cell(rich_row, 24).value == "停用"
    assert "/product-local-images/" not in str(sheet.cell(rich_row, 19).value)
    assert "data/products" not in str(sheet.cell(rich_row, 19).value)
    assert all(sheet.cell(row, column).value is None for row in (missing_row, rich_row) for column in (26, 27, 28, 29))
    assert missing.status == "new_pending_completion" and rich.status == "pending_qinsi_product_import"
    with pytest.raises(ValueError, match="已有待确认"):
        create_qinsi_product_export(db, {rich.id})


def test_repeated_generation_and_download_reuse_same_records_and_bytes(client):
    _, db, _ = client
    purchase, products, _ = make_purchase(db, [
        {"name": "待导入商品", "jan": "4901234567894", "status": "new_pending_review"},
    ])
    with pytest.raises(ValueError, match="采购Excel已阻塞"):
        generate_purchase_batch_exports(db, purchase.id)
    product_job = create_qinsi_product_export(db, {products[0].id})
    confirm_qinsi_product_export(db, product_job, actor_name="测试操作人")
    assert products[0].status == "qinsi_product_imported" and products[0].qinsi_product_code is None
    purchase_job = generate_purchase_batch_exports(db, purchase.id)[0]
    assert purchase_job.export_type == "restock"
    cancel_qinsi_product_export_confirmation(db, product_job, actor_name="测试管理员")
    assert product_job.status == "exported" and product_job.cancelled_by == "测试管理员"
    assert product_job.cancelled_at is not None and products[0].status == "pending_qinsi_product_import"


def test_all_success_is_idempotent_and_success_line_cannot_retry(db_session):
    purchase, products, locations = make_purchase(db_session, [
        {"name": "加权商品", "jan": "4901234567894", "origin": "qinsi"},
    ])
    first = purchase.items[0]
    first.quantity, first.unit_price, first.actual_line_amount = 1, 101, 101
    receipt_item = ReceiptItem(
        receipt_id=purchase.receipt_id, line_no=2, raw_name=products[0].name_cn,
        jan_candidate=products[0].jan, product_id=products[0].id, match_status="matched_existing",
        quantity=3, unit_price=200, discount_amount=0, line_total=600,
        confidence=1, review_status="confirmed",
    )
    db_session.add(receipt_item)
    db_session.flush()
    db_session.add(PurchaseBatchItem(
        purchase_batch_id=purchase.id, product_id=products[0].id, receipt_item_id=receipt_item.id,
        quantity=3, unit_price=200, discount_amount=0, actual_line_amount=600,
        initial_location_id=locations["日本家里库存"].id,
        qinsi_target_warehouse_id=locations["新日本仓库"].id,
    ))
    db_session.commit()
    job = generate_purchase_batch_exports(db_session, purchase.id)[0]
    book = workbook(job.file_content)
    sheet = book["采购单商品导入"]
    assert book.sheetnames == ["采购单商品导入"] and sheet.max_column == 7
    assert tuple(cell.value for cell in sheet[1]) == QINSI_PURCHASE_TEMPLATE_HEADERS
    assert [sheet.cell(2, column).value for column in range(1, 8)] == [
        products[0].jan, products[0].jan, "个", 4, 175.25, None, purchase.batch_no[:20],
    ]
    assert sheet["A2"].data_type == "s" and sheet["B2"].data_type == "s"
    assert job.line_count == 1 and len(job.lines) == 2
    assert generate_purchase_batch_exports(db_session, purchase.id)[0].id == job.id


def test_multiple_receipts_create_distinct_batches_and_merge_export_tracks_all_batches(client):
    http, db_session, _ = client
    first, products, locations = make_purchase(db_session, [
        {"name": "跨批次加权商品", "jan": "4901234567894", "origin": "qinsi"},
    ], same_warehouse=True)
    first.items[0].quantity = 2
    first.items[0].unit_price = 100
    first.items[0].actual_line_amount = 180
    gpt_batch = ReceiptBatch(
        batch_no=f"QINSI-MERGE-{first.id}", status="confirmed", image_status="ready", gpt_status="reviewed",
    )
    receipt = Receipt(
        batch=gpt_batch, raw_store_name="第二张小票", confirmation_status="confirmed", review_status="reviewed",
        confirmed_at=datetime.now(timezone.utc),
    )
    db_session.add(receipt)
    db_session.flush()
    second = PurchaseBatch(
        batch_no=f"PB-MERGE-{receipt.id:08d}", receipt_id=receipt.id, gpt_batch_id=gpt_batch.id,
        store_name=receipt.raw_store_name, confirmed_at=receipt.confirmed_at, status="confirmed",
        default_initial_location_id=locations["日本家里库存"].id,
        default_qinsi_warehouse_id=locations["新日本仓库"].id,
    )
    db_session.add(second)
    db_session.flush()
    receipt_item = ReceiptItem(
        receipt_id=receipt.id, line_no=1, raw_name=products[0].name_cn, jan_candidate=products[0].jan,
        product_id=products[0].id, match_status="matched_existing", quantity=3, unit_price=200,
        discount_amount=0, line_total=600, confidence=1, review_status="confirmed",
    )
    db_session.add(receipt_item)
    db_session.flush()
    db_session.add(PurchaseBatchItem(
        purchase_batch_id=second.id, product_id=products[0].id, receipt_item_id=receipt_item.id,
        quantity=3, unit_price=200, discount_amount=0, actual_line_amount=600,
        initial_location_id=locations["日本家里库存"].id,
        qinsi_target_warehouse_id=locations["新日本仓库"].id,
    ))
    db_session.commit()

    response = http.post(
        "/purchase-batches/qinsi-exports/merge",
        data={"purchase_batch_ids": [str(first.id), str(second.id)]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job = db_session.scalar(select(QinsiPurchaseExportJob))
    download = http.get(f"/qinsi-exports/{job.id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    sheet = workbook(download.content)["采购单商品导入"]
    assert job.selected_batch_ids == sorted([first.id, second.id])
    assert {line.purchase_batch_id for line in job.lines} == {first.id, second.id}
    assert [sheet.cell(2, column).value for column in range(1, 6)] == [
        products[0].jan, products[0].jan, "个", 5, 156,
    ]


def test_local_price_check_is_immediate_and_uses_confirmed_receipt_actual_unit_price(client):
    http, db, _ = client
    _, products, _ = make_purchase(db, [{
        "name": "本地查价商品", "name_ja": "ローカル商品", "jan": "4901234567894",
        "origin": "qinsi", "purchase_price": 999, "image_url": "https://img.test/local.jpg",
    }])
    response = http.post("/price-check", data={"jan": products[0].jan}, follow_redirects=False)
    assert response.status_code == 303
    result = http.get(response.headers["location"])
    assert result.status_code == 200
    for expected in (
        "本地已有商品", "本地查价商品|ローカル商品", "https://img.test/local.jpg",
        "历史最低采购价", "上一次采购价", "¥101", "秦丝闭环测试店", "东京都测试区一丁目",
    ):
        assert expected in result.text
    assert db.scalar(select(func.count()).select_from(PriceProviderAttempt)) == 0


def test_partial_failure_only_failed_line_reexports_and_success_stays_locked(db_session):
    purchase, _, _ = make_purchase(db_session, [
        {"name": "部分成功A", "jan": "4901234567894", "origin": "qinsi"},
        {"name": "部分失败B", "jan": "4570110290418", "origin": "qinsi"},
    ], same_warehouse=True)
    job = generate_purchase_batch_exports(db_session, purchase.id)[0]
    failed_line, success_line = job.lines
    result = confirm_qinsi_export(db_session, job, QinsiExportConfirmationInput(
        result="partial_failure", failed_line_ids={failed_line.id},
    ))
    failed_line, success_line = result.lines
    assert result.status == "partially_failed"
    assert failed_line.status == "failed" and failed_line.source.is_active is False
    assert success_line.status == "imported" and success_line.source.is_active is True
    with pytest.raises(ValueError, match="失败行"):
        retry_failed_qinsi_lines(db_session, result, {success_line.id})
    retry = retry_failed_qinsi_lines(db_session, result, {failed_line.id})
    repeated_retry = retry_failed_qinsi_lines(db_session, result, {failed_line.id})
    assert retry.parent_export_job_id == result.id and retry.line_count == 1
    assert repeated_retry.id == retry.id
    assert retry.lines[0].purchase_batch_item_id == failed_line.purchase_batch_item_id
    assert retry.lines[0].purchase_batch_item_id != success_line.purchase_batch_item_id


def test_all_failed_rows_can_be_selected_for_retry(db_session):
    purchase, _, _ = make_purchase(db_session, [
        {"name": "全部失败A", "jan": "4901234567894", "origin": "qinsi"},
        {"name": "全部失败B", "jan": "4570110290418", "origin": "qinsi"},
    ], same_warehouse=True)
    job = generate_purchase_batch_exports(db_session, purchase.id)[0]
    result = confirm_qinsi_export(db_session, job, QinsiExportConfirmationInput(result="all_failed"))
    assert result.status == "failed" and all(line.status == "failed" and not line.source.is_active for line in result.lines)
    retry = retry_failed_qinsi_lines(db_session, result, {line.id for line in result.lines})
    assert retry.status == "generated" and retry.line_count == 2


def test_pages_show_three_work_queues_mobile_selection_and_duplicate_submit_guard(client):
    http, db, _ = client
    purchase, products, _ = make_purchase(db, [
        {"name": "页面新品", "jan": "4901234567894", "status": "new_pending_review"},
    ])
    blocked = http.get(f"/purchase-batches/{purchase.id}")
    assert blocked.status_code == 200 and "被 1 个新商品阻塞" in blocked.text
    assert "打开未登录商品列表" in blocked.text
    products_page = http.get("/products?status=new_pending_review")
    assert "未登录秦丝的新商品" in products_page.text and "全选" in products_page.text
    created = http.post(
        "/products/qinsi-new-exports", data={"product_ids": str(products[0].id)}, follow_redirects=False,
    )
    assert created.status_code == 303
    product_job = db.scalar(select(QinsiExportJob))
    detail = http.get(created.headers["location"])
    assert "秦丝商品导入成功" in detail.text and "采购入库分开确认" in detail.text
    http.post(f"/qinsi-product-exports/{product_job.id}/confirm", data={"actor_name": "页面操作人"})
    purchase_page = http.get(f"/purchase-batches/{purchase.id}")
    assert "生成秦丝采购单商品Excel" in purchase_page.text and "single-submit" in purchase_page.text
    response = http.post(f"/purchase-batches/{purchase.id}/qinsi-exports", follow_redirects=False)
    assert response.status_code == 303
    purchase_job = db.scalar(select(QinsiPurchaseExportJob))
    purchase_detail = http.get(f"/qinsi-exports/{purchase_job.id}")
    assert "秦丝采购入库成功" in purchase_detail.text and "新日本仓库" in purchase_detail.text
