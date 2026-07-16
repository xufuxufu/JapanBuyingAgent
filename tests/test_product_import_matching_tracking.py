from __future__ import annotations

from datetime import datetime, timezone
import io
from pathlib import Path
import re
from xml.sax.saxutils import escape
import zipfile

from sqlalchemy import func, select

from app.models import (
    ImportRow, Product, ProductAlias, ProductMatchLog, Receipt, ReceiptBatch,
    ReceiptImage, ReceiptItem, ZipPackageItem, ZipPackageJob,
)
from app.product_matching import bind_product, match_item, validate_jan
from app.qinsi_import import confirm_import, create_import_preview
from app.services import confirm_receipt, repair_confirmed_review_statuses


ROOT = Path(__file__).resolve().parents[1]
QINSI_BOOK = ROOT / "reference" / "qinsi" / "goodsImportTemplate已有商品模版-可到导入到本地数据库.xlsx"


def make_xlsx(rows: list[list[str]]) -> bytes:
    headers = ["名称（必填）", "货号（必填且唯一）", "条码", "状态"]
    all_rows = [headers, *rows]
    xml_rows = []
    for row_no, values in enumerate(all_rows, 1):
        cells = []
        for column, value in enumerate(values):
            ref = f"{chr(65 + column)}{row_no}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        xml_rows.append(f'<row r="{row_no}">{"".join(cells)}</row>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="商品导入" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))
    return output.getvalue()


def make_batch(db, receipt_count=1):
    sequence = (db.scalar(select(func.count()).select_from(ReceiptBatch)) or 0) + 1
    batch = ReceiptBatch(batch_no=f"TEST-{id(db)}-{receipt_count}-{sequence}", status="review", image_status="ready", gpt_status="json_imported")
    db.add(batch)
    receipts = []
    for index in range(receipt_count):
        receipt = Receipt(batch=batch, raw_store_name=f"店铺{index + 1}", confirmation_status="pending", review_status="pending")
        receipt.items.append(ReceiptItem(
            line_no=1, raw_name=f"商品{index + 1}", jan_candidate=None, quantity=1,
            confidence=0.9, review_status="pending", match_status="unmatched",
        ))
        db.add(receipt)
        receipts.append(receipt)
    db.commit()
    return batch, receipts


def test_final_confirmation_synchronizes_receipt_and_all_non_ignored_items(db_session):
    batch, receipts = make_batch(db_session)
    ignored = ReceiptItem(receipt=receipts[0], line_no=2, raw_name="忽略行", quantity=1, confidence=0.1, review_status="ignored", match_status="unmatched")
    db_session.add(ignored)
    db_session.commit()
    confirm_receipt(db_session, batch, receipts[0])
    assert receipts[0].confirmation_status == "confirmed"
    assert receipts[0].review_status == "reviewed"
    assert receipts[0].items[0].review_status == "confirmed"
    assert ignored.review_status == "ignored"
    assert batch.gpt_status == "reviewed"


def test_batch_and_gpt_job_wait_for_every_receipt(db_session):
    batch, receipts = make_batch(db_session, receipt_count=2)
    image = ReceiptImage(
        batch=batch, original_filename="a.jpg", recognition_filename="A_P01.jpg", stored_filename="a.jpg",
        original_path="data/uploads/original/a.jpg", page_no=1, file_hash="a" * 64, mime_type="image/jpeg",
        file_size=1, preprocessing_status="processed",
    )
    job = ZipPackageJob(job_no="JOB-STATUS", selection_key="s" * 64, batch_count=1, image_count=1, gpt_status="review_pending")
    db_session.add_all([image, job])
    db_session.flush()
    db_session.add(ZipPackageItem(job_id=job.id, batch_id=batch.id, image_id=image.id, recognition_filename=image.recognition_filename))
    db_session.commit()
    confirm_receipt(db_session, batch, receipts[0])
    assert batch.gpt_status == "json_imported" and job.gpt_status == "review_pending"
    confirm_receipt(db_session, batch, receipts[1])
    assert batch.gpt_status == "reviewed" and job.gpt_status == "reviewed"


def test_historical_confirmed_pending_repair_is_idempotent(db_session):
    batch, receipts = make_batch(db_session)
    receipts[0].confirmation_status = "confirmed"
    receipts[0].items.append(ReceiptItem(line_no=2, raw_name="保留忽略", quantity=1, confidence=0, review_status="ignored", match_status="unmatched"))
    db_session.commit()
    first = repair_confirmed_review_statuses(db_session)
    second = repair_confirmed_review_statuses(db_session)
    assert first["receipts"] == 1 and first["items"] == 1
    assert second == {"receipts": 0, "items": 0, "batches": 0, "gpt_jobs": 0}
    assert receipts[0].items[1].review_status == "ignored"


def test_existing_qinsi_workbook_preview_mapping_warning_and_import(db_session):
    job = create_import_preview(db_session, QINSI_BOOK.name, QINSI_BOOK.read_bytes())
    assert (job.total_rows, job.skipped_count, job.warning_count, job.conflict_count, job.error_count) == (2000, 1998, 2, 0, 0)
    rows = list(db_session.scalars(select(ImportRow).where(ImportRow.import_job_id == job.id, ImportRow.status == "warning").order_by(ImportRow.row_no)))
    assert len(rows) == 2 and all("184" in (row.warnings_json or "") for row in rows)
    confirm_import(db_session, job)
    products = list(db_session.scalars(select(Product).order_by(Product.id)))
    assert job.success_count == 2
    assert products[0].jan == "4570110290418" and products[0].qinsi_product_code == "1234321"
    assert products[1].jan == "4901872962495" and products[1].qinsi_product_code == "4901872097296"
    assert products[0].specification == "184"  # quarantined as warned; never guessed away


def test_repeat_qinsi_import_is_incremental_and_does_not_blank_fields(db_session):
    content = QINSI_BOOK.read_bytes()
    first = create_import_preview(db_session, QINSI_BOOK.name, content)
    confirm_import(db_session, first)
    product = db_session.scalar(select(Product).where(Product.jan == "4570110290418"))
    product.name_ja = "既有非空字段"
    db_session.commit()
    second = create_import_preview(db_session, QINSI_BOOK.name, content)
    confirm_import(db_session, second)
    assert db_session.scalar(select(func.count()).select_from(Product)) == 2
    assert second.success_count == 0 and second.skipped_count == 2000
    assert product.name_ja == "既有非空字段"


def test_excel_identifiers_preserve_leading_zero_and_allow_empty_jan(db_session):
    content = make_xlsx([["前导零商品", "0000456", "00123457", "启用"], ["空JAN商品", "0000789", "", "启用"]])
    job = create_import_preview(db_session, "leading-zero.xlsx", content)
    confirm_import(db_session, job)
    products = list(db_session.scalars(select(Product).order_by(Product.id)))
    assert [(product.qinsi_product_code, product.jan) for product in products] == [("0000456", "00123457"), ("0000789", None)]
    assert all(re.fullmatch(r"NJ-\d{8}-\d{6}", product.internal_sku) for product in products)


def test_excel_duplicate_non_empty_jan_is_conflict_and_not_imported(db_session):
    content = make_xlsx([["重复一", "C-1", "00123457", "启用"], ["重复二", "C-2", "00123457", "启用"]])
    job = create_import_preview(db_session, "duplicate.xlsx", content)
    assert job.conflict_count == 2
    confirm_import(db_session, job)
    assert db_session.scalar(select(func.count()).select_from(Product)) == 0


def test_jan8_jan13_and_confirmed_gtin_formats_validate_checksum():
    assert validate_jan("12345670")
    assert validate_jan("4901234567894")
    assert validate_jan("00012345600012")
    assert not validate_jan("4901234567890")
    assert not validate_jan("1234ABC0")


def test_matching_states_and_repeated_purchase_facts_remain_independent(db_session):
    product = Product(jan="4901234567894", name_cn="已有商品", product_origin="qinsi")
    batch, receipts = make_batch(db_session)
    receipt = receipts[0]
    receipt.items[0].jan_candidate = "4901234567894"
    receipt.items[0].review_status = "confirmed"
    receipt.items.extend([
        ReceiptItem(line_no=2, raw_name="新商品", jan_candidate="4570110290418", quantity=2, confidence=1, review_status="confirmed", match_status="unmatched"),
        ReceiptItem(line_no=3, raw_name="空JAN", jan_candidate=None, quantity=1, confidence=1, review_status="confirmed", match_status="unmatched"),
        ReceiptItem(line_no=4, raw_name="非法JAN", jan_candidate="4901234567890", quantity=1, confidence=1, review_status="confirmed", match_status="unmatched"),
        ReceiptItem(line_no=5, raw_name="重复采购", jan_candidate="4901234567894", quantity=3, confidence=1, review_status="confirmed", match_status="unmatched"),
    ])
    db_session.add(product)
    db_session.commit()
    for item in receipt.items:
        match_item(db_session, item)
    db_session.commit()
    assert [item.match_status for item in receipt.items] == ["matched_existing", "new_product", "needs_review", "invalid_jan", "matched_existing"]
    assert receipt.items[0].product_id == receipt.items[4].product_id == product.id
    assert len(receipt.items) == 5
    assert db_session.scalar(select(func.count()).select_from(ProductMatchLog)) == 5


def test_manual_binding_creates_confirmed_alias_and_tracking_pages(client):
    http, db, _ = client
    product = Product(jan="4901234567894", qinsi_product_code="Q-001", name_cn="追踪商品", product_origin="qinsi")
    batch, receipts = make_batch(db)
    item = receipts[0].items[0]
    item.raw_name = "票面别名"
    item.review_status = "confirmed"
    receipts[0].confirmation_status = "confirmed"
    receipts[0].review_status = "reviewed"
    receipts[0].purchased_at = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    image1 = ReceiptImage(batch=batch, original_filename="one.jpg", recognition_filename="ONE_P01.jpg", stored_filename="one.jpg", original_path="data/uploads/original/one.jpg", processed_path="data/uploads/preview/one.jpg", page_no=1, file_hash="1" * 64, mime_type="image/jpeg", file_size=1, preprocessing_status="processed")
    db.add_all([product, image1])
    db.commit()
    item.source_image_id = image1.id
    bind_product(db, item, product)
    batch2, receipts2 = make_batch(db)
    item2 = receipts2[0].items[0]
    item2.raw_name = "第二张票同商品"
    item2.quantity = 3
    item2.review_status = "confirmed"
    receipts2[0].confirmation_status = "confirmed"
    receipts2[0].review_status = "reviewed"
    receipts2[0].purchased_at = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    image2 = ReceiptImage(batch=batch2, original_filename="two.jpg", recognition_filename="TWO_P02.jpg", stored_filename="two.jpg", original_path="data/uploads/original/two.jpg", processed_path="data/uploads/preview/two.jpg", page_no=2, file_hash="2" * 64, mime_type="image/jpeg", file_size=1, preprocessing_status="processed")
    db.add(image2)
    db.commit()
    item2.source_image_id = image2.id
    bind_product(db, item2, product)
    alias = db.scalar(select(ProductAlias).where(ProductAlias.product_id == product.id))
    assert alias and alias.confirmed and alias.created_from_item_id == item.id
    forward = http.get(f"/receipts/{batch.id}/review?receipt_id={receipts[0].id}")
    reverse = http.get(f"/products/{product.id}")
    assert forward.status_code == 200 and "追踪商品" in forward.text and "查看商品" in forward.text
    assert reverse.status_code == 200 and "<strong>2</strong>采购次数" in reverse.text and reverse.text.count("打开并定位") == 2
    assert "ONE_P01.jpg" in reverse.text and "TWO_P02.jpg" in reverse.text
    assert f"#receipt-item-{item.id}" in reverse.text and f"#receipt-item-{item2.id}" in reverse.text


def test_products_pages_do_not_expose_inventory_balance(client):
    http, db, _ = client
    product = Product(name_cn="无库存字段商品", product_origin="manual")
    db.add(product)
    db.commit()
    assert "库存余额" not in http.get("/products").text
    assert "库存余额" not in http.get(f"/products/{product.id}").text


def test_no_jan_product_can_be_reviewed_created_and_tracked_both_directions(client):
    http, db, _ = client
    batch, receipts = make_batch(db)
    receipt, item = receipts[0], receipts[0].items[0]
    receipt.purchased_at = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    confirm_receipt(db, batch, receipt)
    assert item.review_status == "confirmed" and item.match_status == "needs_review"
    response = http.post(f"/receipts/{batch.id}/review/items/{item.id}/new-product", follow_redirects=False)
    assert response.status_code == 303
    db.refresh(item)
    product = db.get(Product, item.product_id)
    assert product is not None and product.jan is None and product.internal_sku
    forward = http.get(f"/receipts/{batch.id}/review?receipt_id={receipt.id}")
    reverse = http.get(f"/products/{product.id}")
    assert forward.status_code == reverse.status_code == 200
    assert product.internal_sku in forward.text and batch.batch_no in reverse.text
