from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    Product, PurchaseBatch, PurchaseBatchItem, QinsiPurchaseExportJob,
    QinsiPurchaseExportLine, QinsiPurchaseExportLineSource, Receipt, ReceiptBatch, ReceiptItem,
)
from app.qinsi_export import (
    QINSI_TEMPLATE_HEADERS, confirm_qinsi_export, generate_purchase_batch_exports,
    retry_failed_qinsi_lines,
)
from app.qinsi_import import read_product_sheet
from app.schemas import QinsiExportConfirmationInput


def make_purchase(db, products: list[dict], *, same_warehouse: bool = False):
    locations = {location.display_name: location for location in initialize_default_locations(db)}
    models = [Product(
        name_cn=values["name"], jan=values.get("jan"),
        qinsi_product_code=values.get("code"), product_origin=values.get("origin", "manual"),
        purchase_price=values.get("purchase_price", 120), sale_price=values.get("sale_price", 180),
    ) for values in products]
    db.add_all(models)
    db.flush()
    gpt_batch = ReceiptBatch(batch_no=f"QINSI-EXPORT-{id(db)}-{len(models)}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(
        batch=gpt_batch, raw_store_name="秦丝闭环测试店", purchased_at=datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc),
        confirmation_status="confirmed", review_status="reviewed", confirmed_at=datetime.now(timezone.utc),
    )
    db.add(receipt)
    db.flush()
    purchase = PurchaseBatch(
        batch_no=f"PB-QINSI-{receipt.id:08d}", receipt_id=receipt.id, gpt_batch_id=gpt_batch.id,
        purchased_at=receipt.purchased_at, store_name=receipt.raw_store_name, confirmed_at=receipt.confirmed_at,
        status="confirmed", default_initial_location_id=locations["日本家里库存"].id,
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


def first_export_row(job):
    return read_product_sheet(job.file_content)[0]


def test_new_existing_jan_no_jan_warehouse_template_and_full_source_tracking(client):
    http, db, _ = client
    purchase, products, _ = make_purchase(db, [
        {"name": "新商品有JAN", "jan": "04901234567894"},
        {"name": "新商品无JAN"},
        {"name": "已有商品补货", "jan": "4570110290418", "code": "QINSI-OLD-3", "origin": "qinsi"},
    ])
    jobs = generate_purchase_batch_exports(db, purchase.id)
    assert len(jobs) == 3 and {job.export_type for job in jobs} == {"new_product", "restock"}
    assert all(job.status == "generated" and job.line_count == 1 and job.file_content for job in jobs)
    for job in jobs:
        row_no, raw = first_export_row(job)
        line = job.lines[0]
        assert row_no == line.row_no == 2
        assert tuple(raw)[:28] == QINSI_TEMPLATE_HEADERS[:28]
        assert tuple(raw)[28] == job.qinsi_target_warehouse.display_name
        assert raw["盘点库存数量"] == str(line.quantity)
        assert raw[job.qinsi_target_warehouse.display_name] == str(line.quantity)
        assert line.purchase_batch_id == purchase.id
        assert line.purchase_batch_item_id == line.purchase_batch_item.id
        assert line.receipt_id == purchase.receipt_id
        assert line.receipt_item_id == line.purchase_batch_item.receipt_item_id
        assert line.product_id == line.purchase_batch_item.product_id
        assert line.source.purchase_batch_item_id == line.purchase_batch_item_id and line.source.is_active
    no_jan_job = next(job for job in jobs if job.lines[0].product_id == products[1].id)
    _, no_jan = first_export_row(no_jan_job)
    assert no_jan["条码"] == ""
    assert no_jan["货号（必填且唯一）"] == products[1].internal_sku
    with_jan_job = next(job for job in jobs if job.lines[0].product_id == products[0].id)
    assert first_export_row(with_jan_job)[1]["条码"] == "04901234567894"
    restock = next(job for job in jobs if job.export_type == "restock")
    assert first_export_row(restock)[1]["货号（必填且唯一）"] == "QINSI-OLD-3"
    assert http.get("/qinsi-exports").status_code == 200
    assert http.get(f"/qinsi-exports/{restock.id}").status_code == 200
    download = http.get(f"/qinsi-exports/{restock.id}/download")
    assert download.status_code == 200 and download.content == restock.file_content


def test_repeated_generation_and_download_reuse_same_records_and_bytes(client):
    http, db, _ = client
    purchase, _, _ = make_purchase(db, [{"name": "幂等新商品", "jan": "4901234567894"}])
    first = generate_purchase_batch_exports(db, purchase.id)
    original_bytes = first[0].file_content
    second = generate_purchase_batch_exports(db, purchase.id)
    assert [job.id for job in first] == [job.id for job in second]
    assert db.scalar(select(func.count()).select_from(QinsiPurchaseExportJob)) == 1
    assert db.scalar(select(func.count()).select_from(QinsiPurchaseExportLine)) == 1
    assert http.get(f"/qinsi-exports/{first[0].id}/download").content == original_bytes
    assert http.get(f"/qinsi-exports/{first[0].id}/download").content == original_bytes


def test_all_success_is_idempotent_and_success_line_cannot_retry(db_session):
    purchase, products, _ = make_purchase(db_session, [{"name": "确认成功无JAN"}])
    job = generate_purchase_batch_exports(db_session, purchase.id)[0]
    confirmation = QinsiExportConfirmationInput(result="all_success")
    confirmed = confirm_qinsi_export(db_session, job, confirmation)
    repeated = confirm_qinsi_export(db_session, confirmed, confirmation)
    assert confirmed.id == repeated.id and repeated.status == "imported"
    assert repeated.lines[0].status == "imported" and repeated.lines[0].source.is_active
    assert products[0].qinsi_product_code == products[0].internal_sku
    assert products[0].product_origin == "qinsi"
    assert db_session.scalar(select(func.count()).select_from(QinsiPurchaseExportLineSource).where(QinsiPurchaseExportLineSource.is_active.is_(True))) == 1
    with pytest.raises(ValueError, match="失败"):
        retry_failed_qinsi_lines(db_session, repeated, {repeated.lines[0].id})


def test_partial_failure_only_failed_line_reexports_and_success_stays_locked(db_session):
    purchase, _, _ = make_purchase(db_session, [
        {"name": "部分成功A", "jan": "4901234567894"},
        {"name": "部分失败B", "jan": "4570110290418"},
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
        {"name": "全部失败A", "jan": "4901234567894"},
        {"name": "全部失败B", "jan": "4570110290418"},
    ], same_warehouse=True)
    job = generate_purchase_batch_exports(db_session, purchase.id)[0]
    result = confirm_qinsi_export(db_session, job, QinsiExportConfirmationInput(result="all_failed"))
    assert result.status == "failed" and all(line.status == "failed" and not line.source.is_active for line in result.lines)
    retry = retry_failed_qinsi_lines(db_session, result, {line.id for line in result.lines})
    assert retry.status == "generated" and retry.line_count == 2


def test_pages_show_three_work_queues_mobile_selection_and_duplicate_submit_guard(client):
    http, db, _ = client
    purchase, _, _ = make_purchase(db, [
        {"name": "页面A", "jan": "4901234567894"},
        {"name": "页面B", "jan": "4570110290418"},
    ], same_warehouse=True)
    before = http.get(f"/purchase-batches/{purchase.id}")
    assert before.status_code == 200 and "待导出" in before.text and "single-submit" in before.text
    response = http.post(f"/purchase-batches/{purchase.id}/qinsi-exports", follow_redirects=False)
    assert response.status_code == 303
    job = db.scalar(select(QinsiPurchaseExportJob))
    detail = http.get(f"/qinsi-exports/{job.id}")
    assert detail.status_code == 200 and "已导出待确认" in detail.text
    assert "line-check" in detail.text and "disabled=true" in detail.text
    failed_id = job.lines[0].id
    partial = http.post(f"/qinsi-exports/{job.id}/confirm", data={"result": "partial_failure", "failed_line_ids": str(failed_id)}, follow_redirects=False)
    assert partial.status_code == 303
    after = http.get(f"/purchase-batches/{purchase.id}")
    assert "失败待重试" in after.text and "已提交秦丝" in after.text
    retry_page = http.get(f"/qinsi-exports/{job.id}")
    assert "全选失败行" in retry_page.text and "仅重新导出所选失败行" in retry_page.text
