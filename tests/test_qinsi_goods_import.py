from __future__ import annotations

import json
from io import BytesIO
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    DurableBackgroundJob,
    Product,
    ProductBarcode,
    QinsiGoodsImportRow,
    QinsiImportBatch,
    QinsiInventorySnapshot,
    QinsiInventorySnapshotLine,
    QinsiMasterValue,
)
from app.qinsi_goods_import import (
    _mapped_row,
    confirm_import,
    create_import_preview,
    parse_qinsi_workbook,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_BOOK = ROOT / "tests" / "testExcelFiles" / "goodsImportTemplate_1-2000.xlsx"
REFERENCE_BOOK = ROOT / "reference" / "qinsi" / "goodsImportTemplate已有商品模版-可到导入到本地数据库.xlsx"
requires_real_book = pytest.mark.skipif(
    not REAL_BOOK.exists(),
    reason="本地真实秦丝Excel fixture未提供（tests/testExcelFiles 已被 gitignore）",
)


@requires_real_book
def test_real_qinsi_workbook_parses_logical_values_and_dynamic_config():
    workbook = parse_qinsi_workbook(REAL_BOOK.read_bytes())
    assert len(workbook.rows) == 2000
    assert len(workbook.headers) == 30
    assert workbook.inventory_warehouse == "新日本仓库"
    assert {key: len(value) for key, value in workbook.config.items()} == {
        "brand": 84,
        "category": 53,
        "unit": 11,
        "warehouse": 5,
        "product_status": 2,
        "points_status": 2,
    }
    assert {
        "blandValidate", "categoryValidate", "unitValidate", "storehouseValidate",
        "onSaleValidate", "onClientPointEnable",
    } <= set(workbook.defined_names)

    raw_rows = [raw for _, raw in workbook.rows]
    assert all(tuple(raw) == workbook.headers for raw in raw_rows)
    assert all(len(raw) == 30 for raw in raw_rows)
    assert all(raw["产地"] is None and raw["适用年龄"] is None and raw["库位"] is None for raw in raw_rows)
    assert not any(value == "184" for raw in raw_rows for value in raw.values())

    mapped = [_mapped_row(raw, workbook.config)[0] for raw in raw_rows]
    codes = [row["qinsi_product_code"] for row in mapped if row["qinsi_product_code"]]
    barcodes = [row["jan"] for row in mapped if row["jan"]]
    assert len(codes) == len(set(codes)) == 2000
    assert len(barcodes) == 865
    assert sum(row["jan"] is None for row in mapped) == 1135
    assert sum(
        row["purchase_price"] is not None and row["purchase_price"] % 1 != 0
        for row in mapped
    ) == 85
    assert sum(
        row["sale_price"] is not None and row["sale_price"] % 1 != 0
        for row in mapped
    ) == 4

    counted = [row["counted_inventory_quantity"] for row in mapped if row["counted_inventory_quantity"] is not None]
    assert len(counted) == 626
    assert sum(value == 0 for value in counted) == 575
    assert sum(value > 0 for value in counted) == 51
    assert sum(value < 0 for value in counted) == 0


@requires_real_book
def test_real_qinsi_preview_confirm_and_file_hash_idempotency(db_session):
    initialize_default_locations(db_session)
    content = REAL_BOOK.read_bytes()
    batch = create_import_preview(
        db_session,
        REAL_BOOK.name,
        content,
        business_batch_key="QINSI-5412-LOCAL-ACCEPTANCE",
    )
    assert batch.status == "previewed"
    assert (
        batch.total_rows, batch.new_count, batch.update_count, batch.unchanged_count,
        batch.skipped_count, batch.conflict_count, batch.error_count, batch.warning_count,
    ) == (2000, 1973, 0, 0, 0, 24, 3, 0)

    import_rows = list(db_session.scalars(select(QinsiGoodsImportRow).where(
        QinsiGoodsImportRow.import_batch_id == batch.id,
    ).order_by(QinsiGoodsImportRow.excel_row_number)))
    assert len(import_rows) == 2000
    assert sum(row.barcode is not None for row in import_rows) == 865
    assert sum(row.barcode is None for row in import_rows) == 1135
    assert not any(row.barcode == "184" for row in import_rows)
    assert not any("可疑值184" in (row.warnings or "") for row in import_rows)
    assert not any("重复非空条码：184" in (row.conflict_json or "") for row in import_rows)
    assert sum(row.validation_status == "error" for row in import_rows) == 3
    assert sum(row.validation_status == "conflict" for row in import_rows) == 24
    assert all(
        "合法 JAN-8/JAN-13" in (row.errors or "")
        for row in import_rows if row.validation_status == "error"
    )
    assert all(len(json.loads(row.raw_json)) == 30 for row in import_rows)

    confirm_import(db_session, batch)
    assert batch.status == "completed_with_issues"
    assert (batch.new_count, batch.update_count, batch.unchanged_count) == (1973, 0, 0)
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1973
    assert db_session.scalar(
        select(func.count()).select_from(Product).where(Product.product_note.is_not(None))
    ) == 0
    assert db_session.scalar(
        select(func.count()).select_from(Product).where(Product.name_ja.is_not(None))
    ) > 0
    assert sum(
        product.purchase_price is not None and product.purchase_price % Decimal("1") != 0
        for product in db_session.scalars(select(Product))
    ) == 85
    assert sum(
        product.sale_price is not None and product.sale_price % Decimal("1") != 0
        for product in db_session.scalars(select(Product))
    ) == 4

    master_counts = dict(db_session.execute(
        select(QinsiMasterValue.master_type, func.count())
        .group_by(QinsiMasterValue.master_type)
    ).all())
    assert master_counts == {
        "brand": 84,
        "category": 53,
        "unit": 11,
        "warehouse": 5,
        "product_status": 2,
        "points_status": 2,
    }

    snapshot = db_session.scalar(select(QinsiInventorySnapshot))
    assert snapshot is not None
    assert snapshot.source_system == "qinsi"
    assert snapshot.snapshot_type == "counted_inventory"
    assert snapshot.source_import_batch_id == batch.id
    assert snapshot.original_filename == REAL_BOOK.name
    assert (snapshot.total_rows, snapshot.success_rows, snapshot.unmatched_rows, snapshot.exception_rows) == (616, 616, 0, 0)
    lines = list(db_session.scalars(select(QinsiInventorySnapshotLine).where(
        QinsiInventorySnapshotLine.snapshot_id == snapshot.id,
    )))
    assert len(lines) == 616
    assert {line.raw_warehouse_name for line in lines} == {"新日本仓库"}
    assert sum(line.quantity == 0 for line in lines) == 568
    assert sum(line.quantity is not None and line.quantity > 0 for line in lines) == 48
    assert sum(line.quantity is not None and line.quantity < 0 for line in lines) == 0

    repeated = create_import_preview(
        db_session,
        "renamed-same-content.xlsx",
        content,
        business_batch_key="QINSI-5412-LOCAL-ACCEPTANCE",
    )
    assert repeated.id == batch.id
    assert repeated.status == "completed_with_issues"
    assert db_session.scalar(select(func.count()).select_from(QinsiImportBatch)) == 1
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1973
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1


def test_background_preview_status_page_and_confirm_flow(client):
    http, db, _ = client
    response = http.post(
        "/products/import/preview",
        files={
            "file": (
                REFERENCE_BOOK.name,
                REFERENCE_BOOK.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        data={"business_batch_key": "QINSI-PAGE-FLOW"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    batch = db.scalar(select(QinsiImportBatch))
    db.refresh(batch)
    assert batch.status == "previewed"
    status = http.get(f"/products/import/{batch.id}/status")
    page = http.get(f"/products/import?job_id={batch.id}")
    assert status.status_code == 200
    assert status.json()["new_count"] == 2
    assert page.status_code == 200
    assert "新增" in page.text and "配置 Master" in page.text

    confirmed = http.post(f"/products/import/{batch.id}/confirm", follow_redirects=False)
    assert confirmed.status_code == 303
    db.expire_all()
    batch = db.get(QinsiImportBatch, batch.id)
    assert batch.status == "completed"
    assert db.scalar(select(func.count()).select_from(Product)) == 2


def test_qinsi_import_creates_derived_barcode_alias_idempotently(db_session):
    reproduction_jan = "4550726010198"
    workbook = load_workbook(BytesIO(REFERENCE_BOOK.read_bytes()))
    sheet = workbook["商品导入"]
    headers = {str(cell.value).strip(): cell.column for cell in sheet[1] if cell.value}
    code_column = headers.get("货号（必填且唯一）") or headers["货号(必填且唯一)"]
    barcode_column = headers["条码"]
    sheet.cell(2, code_column).value = f"/{reproduction_jan}"
    sheet.cell(2, barcode_column).value = None
    if sheet.max_row > 2:
        sheet.delete_rows(3, sheet.max_row - 2)
    output = BytesIO()
    workbook.save(output)

    batch = create_import_preview(
        db_session,
        "derived-qinsi-barcode.xlsx",
        output.getvalue(),
        business_batch_key="QINSI-DERIVED-BARCODE",
    )
    assert batch.status == "previewed"
    confirm_import(db_session, batch)
    product = db_session.scalar(
        select(Product).where(Product.qinsi_product_code == f"/{reproduction_jan}")
    )
    alias = db_session.scalar(
        select(ProductBarcode).where(ProductBarcode.barcode == reproduction_jan)
    )
    assert product is not None
    assert alias.product_id == product.id
    assert alias.source_system == "qinsi_sku_derived"
    assert alias.is_primary is False

    repeated = create_import_preview(
        db_session,
        "derived-qinsi-barcode-renamed.xlsx",
        output.getvalue(),
        business_batch_key="QINSI-DERIVED-BARCODE",
    )
    assert repeated.id == batch.id
    assert db_session.scalar(
        select(func.count()).select_from(ProductBarcode).where(
            ProductBarcode.barcode == reproduction_jan
        )
    ) == 1


def test_qinsi_import_maps_japanese_name_and_queues_changed_image_without_clearing_old_display(
    db_session,
):
    def workbook_bytes(image_url: str) -> bytes:
        workbook = load_workbook(BytesIO(REFERENCE_BOOK.read_bytes()))
        sheet = workbook["商品导入"]
        headers = {str(cell.value).strip(): cell.column for cell in sheet[1] if cell.value}
        code_column = headers.get("货号（必填且唯一）") or headers["货号(必填且唯一)"]
        sheet.cell(2, code_column).value = "IMG-CHANGE-1"
        sheet.cell(2, headers["商品图片链接"]).value = image_url
        sheet.cell(2, headers["商品备注"]).value = "  日本語商品名  "
        if sheet.max_row > 2:
            sheet.delete_rows(3, sheet.max_row - 2)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    old_url = "https://images.qinsilk.com/old.png"
    new_url = "https://images.qinsilk.com/new.png"
    first = create_import_preview(db_session, "image-old.xlsx", workbook_bytes(old_url))
    confirm_import(db_session, first)
    product = db_session.scalar(select(Product).where(Product.qinsi_product_code == "IMG-CHANGE-1"))
    assert product.name_ja == "日本語商品名"
    assert product.product_note is None
    assert product.image_url == old_url
    product.display_image_url = f"/product-local-images/{product.id}?v=old"
    product.image_localization_source_url = old_url
    product.image_localization_status = "COMPLETED"
    db_session.commit()

    second = create_import_preview(db_session, "image-new.xlsx", workbook_bytes(new_url))
    assert second.update_count == 1
    confirm_import(db_session, second)
    db_session.refresh(product)
    jobs = list(db_session.scalars(
        select(DurableBackgroundJob)
        .where(DurableBackgroundJob.job_type == "PRODUCT_IMAGE_LOCALIZATION")
        .order_by(DurableBackgroundJob.id)
    ))
    assert len(jobs) == 2
    assert product.image_url == new_url
    assert product.display_image_url.endswith("?v=old")
    assert product.image_localization_source_url == old_url
    assert product.image_localization_status == "PENDING"
