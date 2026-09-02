from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.location_service import initialize_default_locations
from app.models import (
    PriceLookupHistory, PriceSearchRun, Product, ProductEnrichmentTask,
    PurchaseBatch, PurchaseBatchItem, QinsiInventorySnapshot,
    Receipt, ReceiptBatch, ReceiptItem,
)
from app.product_identity import assert_jan_available, update_product_identifiers
from app.product_merge import merge_product_into, merge_products_with_jan_into_primary, list_duplicate_jan_groups
from app.qinsi_export import ExportRow, _template_bytes
from app.qinsi_goods_import import confirm_import, create_import_preview
from app.qinsi_inventory import create_inventory_snapshot_from_files, preview_inventory_snapshot_files


NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def workbook(rows: list[dict], warehouse: str = "新日本仓库") -> bytes:
    exports = []
    for index, values in enumerate(rows, 1):
        model = SimpleNamespace(
            name_cn=values.get("name", f"库存商品{index}"), name_ja=None,
            internal_sku=values.get("internal_sku", f"NJ-TEST-{index}"),
            jan=values.get("jan"), model_spec=None, specification=None,
            purchase_price=100, sale_price=150, minimum_sale_price=None,
            status="active", image_url=None, location_code=None,
        )
        detail = SimpleNamespace(quantity=values.get("quantity", 1), unit_price=100)
        exports.append(ExportRow(detail, model, values.get("code") or model.internal_sku))
    return _template_bytes(exports, warehouse)


def _receipt_purchase(db, product: Product, *, quantity: int = 1) -> tuple[ReceiptItem, PurchaseBatchItem]:
    locations = {loc.display_name: loc for loc in initialize_default_locations(db, commit=False)}
    batch = ReceiptBatch(batch_no=f"RCPT-REPAIR-{product.id}", status="confirmed")
    receipt = Receipt(batch=batch, raw_store_name="修复测试店", confirmation_status="confirmed", confirmed_at=NOW)
    item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name=product.name_cn, product_id=product.id,
        quantity=quantity, unit_price=100, line_total=quantity * 100, discount_amount=0,
        confidence=1, review_status="confirmed", match_status="matched_existing",
    )
    db.add_all([batch, receipt, item])
    db.flush()
    purchase_batch = PurchaseBatch(
        batch_no=f"PB-REPAIR-{product.id}", receipt_id=receipt.id, gpt_batch_id=batch.id,
        confirmed_at=NOW, status="confirmed",
        default_initial_location_id=locations["日本家里库存"].id,
        default_qinsi_warehouse_id=locations["新日本仓库"].id,
    )
    db.add(purchase_batch)
    db.flush()
    purchase_item = PurchaseBatchItem(
        purchase_batch_id=purchase_batch.id, product_id=product.id, receipt_item_id=item.id,
        quantity=quantity, unit_price=100, actual_line_amount=quantity * 100,
        initial_location_id=locations["日本家里库存"].id,
        qinsi_target_warehouse_id=locations["新日本仓库"].id,
    )
    db.add(purchase_item)
    db.commit()
    return item, purchase_item


# ---------------- pattern A: primary already holds the right code, wrong JAN;
# a placeholder already holds the confirmed-correct JAN ----------------


def test_wrong_jan_correction_via_merge_preserves_receipt_history(db_session):
    primary = Product(
        qinsi_product_code="INTIME-CODE", jan="0000000000017", name_cn="¥intime私处保湿乳100g",
        status="qinsi_product_imported", product_origin="qinsi",
    )
    placeholder = Product(jan="4901234567894", name_cn="安蒂姆有机玫瑰私密护理液100g", status="new_pending_review")
    db_session.add_all([primary, placeholder])
    db_session.commit()
    receipt_item, purchase_item = _receipt_purchase(db_session, primary)
    db_session.add(ProductEnrichmentTask(jan=placeholder.jan, status="completed", trigger_source="test", product_id=placeholder.id))
    db_session.commit()
    placeholder_id = placeholder.id

    # Step 1: clear the wrong JAN so it stops colliding with the unique index.
    primary.jan = None
    db_session.flush()
    # Step 2: reuse the existing merge helper -- migrates placeholder's
    # associations into primary, then sets primary.jan to the confirmed value.
    result = merge_products_with_jan_into_primary(db_session, "4901234567894", primary, actor="pytest")

    assert result is not None
    assert placeholder_id in result.merged_product_ids
    db_session.refresh(primary)
    assert primary.jan == "4901234567894"
    assert primary.qinsi_product_code == "INTIME-CODE"
    assert db_session.get(Product, placeholder_id) is None
    # Purchase facts (quantity/price) untouched, only the product link was ever wrong.
    db_session.refresh(receipt_item)
    db_session.refresh(purchase_item)
    assert receipt_item.product_id == primary.id and receipt_item.quantity == 1
    assert purchase_item.product_id == primary.id and purchase_item.actual_line_amount == 100
    assert db_session.scalar(
        select(func.count()).select_from(ProductEnrichmentTask).where(ProductEnrichmentTask.product_id == primary.id)
    ) == 1
    assert list_duplicate_jan_groups(db_session) == []


# ---------------- pattern B: the confirmed-correct JAN is not currently held
# by either product (it was recorded as a qinsi_product_code by mistake) ----------------


def test_identity_correction_via_merge_then_explicit_jan_assignment(db_session):
    primary = Product(
        qinsi_product_code="OSTB-CODE", jan="0000000000024", name_cn="OSTB原液美容面膜VC&视黄醇 布丁狗7片装",
        status="qinsi_product_imported", product_origin="qinsi",
    )
    placeholder = Product(jan="OSTB-CODE", name_cn="小票名称待确认", status="qinsi_product_imported")
    db_session.add_all([primary, placeholder])
    db_session.commit()
    receipt_item, purchase_item = _receipt_purchase(db_session, placeholder)
    placeholder_id = placeholder.id

    primary.jan = None
    db_session.flush()
    result = merge_product_into(db_session, placeholder, primary, actor="pytest")
    assert placeholder_id in result.merged_product_ids
    # primary.jan is intentionally untouched by merge_product_into itself.
    assert primary.jan is None

    update_product_identifiers(db_session, primary, jan="4571507660562", qinsi_product_code=primary.qinsi_product_code)
    db_session.commit()
    db_session.refresh(primary)

    assert primary.jan == "4571507660562"
    assert primary.qinsi_product_code == "OSTB-CODE"
    assert db_session.get(Product, placeholder_id) is None
    db_session.refresh(receipt_item)
    db_session.refresh(purchase_item)
    assert receipt_item.product_id == primary.id
    assert purchase_item.product_id == primary.id


def test_jan_assignment_refuses_to_steal_jan_already_used_by_a_third_product(db_session):
    # Regression for the real audit finding: the "confirmed correct" JAN can
    # turn out to already belong to a completely different, unrelated
    # product. update_product_identifiers must refuse, not silently steal it.
    primary = Product(qinsi_product_code="TARGET-CODE", jan=None, name_cn="待修正商品", status="qinsi_product_imported")
    unrelated = Product(qinsi_product_code="UNRELATED-CODE", jan="4571507660562", name_cn="完全不同的第三个商品", status="qinsi_product_imported")
    db_session.add_all([primary, unrelated])
    db_session.commit()

    with pytest.raises(ValueError, match="已被商品"):
        update_product_identifiers(db_session, primary, jan="4571507660562", qinsi_product_code=primary.qinsi_product_code)
    db_session.rollback()
    primary = db_session.get(Product, primary.id)
    unrelated = db_session.get(Product, unrelated.id)
    assert primary.jan is None
    assert unrelated.jan == "4571507660562"


# ---------------- QinSi-only product creation reuses the existing goods-import pipeline ----------------


def test_qinsi_only_products_created_via_goods_import_pipeline_without_fuzzy_merge(db_session):
    # A similarly-named but genuinely different existing product must not be
    # reused/merged just because the name looks alike.
    lookalike = Product(qinsi_product_code="LOOKALIKE-CODE", jan="4900000000005", name_cn="三丽鸥护甲油紫色美乐蒂")
    db_session.add(lookalike)
    db_session.commit()
    lookalike_id = lookalike.id

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["商品名称", "货号", "条码", "品牌", "分类", "单位", "采购价", "销售价", "备注"])
    ws.append(["三丽鸥护甲油紫色kitty", "4977324137414", "", "", "", "", 770, 770, ""])
    ws.append(["妮维雅唇膏柠檬香草味", "4901301271594", "", "妮维雅", "面霜", "个", 428, 428, ""])
    buffer = io.BytesIO()
    wb.save(buffer)

    batch = create_import_preview(db_session, "unmatched_new.xlsx", buffer.getvalue())
    assert batch.status == "previewed" and batch.new_count == 2 and batch.conflict_count == 0
    confirmed = confirm_import(db_session, batch)
    assert confirmed.new_count == 2 and confirmed.conflict_count == 0

    kitty = db_session.scalar(select(Product).where(Product.qinsi_product_code == "4977324137414"))
    niveau = db_session.scalar(select(Product).where(Product.qinsi_product_code == "4901301271594"))
    assert kitty is not None and kitty.jan == "4977324137414" and kitty.name_cn == "三丽鸥护甲油紫色kitty"
    assert niveau is not None and niveau.jan == "4901301271594"
    assert kitty.id != lookalike_id and niveau.id != lookalike_id
    # the lookalike must be completely untouched
    db_session.refresh(lookalike)
    assert lookalike.jan == "4900000000005" and lookalike.qinsi_product_code == "LOOKALIKE-CODE"
    # creating products must not have created any inventory snapshot as a side effect
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 0


# ---------------- end-to-end: repair conflicts + create missing products, then re-preview ----------------


def test_multi_file_preview_reaches_clean_state_after_repair_and_product_creation(db_session):
    initialize_default_locations(db_session)
    by_code = Product(qinsi_product_code="CONFLICT-CODE", jan="4900000000012", name_cn="冲突商品-货号侧")
    by_jan = Product(jan="4900000000029", name_cn="冲突商品-JAN侧")
    db_session.add_all([by_code, by_jan])
    db_session.commit()

    file_a = workbook([
        {"name": "冲突行", "code": "CONFLICT-CODE", "jan": "4900000000029", "quantity": 2},
        {"name": "未建档新品", "code": "NEW-CODE-1", "quantity": 3},
    ], "新日本仓库")
    row = {"name": "重复行", "code": "DUPE-CODE", "quantity": 1}
    file_b = workbook([row], "新日本仓库")
    file_c = workbook([row], "新日本仓库")
    files = [("a.xlsx", file_a), ("b.xlsx", file_b), ("c.xlsx", file_c)]

    before = preview_inventory_snapshot_files(db_session, files)
    assert before.conflict_count == 1
    # NEW-CODE-1's row and the *first* occurrence of the duplicate row are
    # both genuinely unmatched (neither code exists as a Product yet); the
    # duplicate row's second occurrence collapses separately as "duplicate".
    assert before.unmatched_count == 2
    assert before.duplicate_count == 1

    # --- repair: same code+jan cross-validation pattern as the real fix ---
    by_code.jan = None
    db_session.flush()
    merge_products_with_jan_into_primary(db_session, "4900000000029", by_code, actor="pytest")
    db_session.refresh(by_code)
    assert by_code.jan == "4900000000029"

    # Create the two genuinely-missing products (neither code is itself a
    # valid JAN, so jan stays None -- matches "无JAN商品仍允许正常建档").
    db_session.add_all([
        Product(qinsi_product_code="NEW-CODE-1", jan=None, name_cn="未建档新品"),
        Product(qinsi_product_code="DUPE-CODE", jan=None, name_cn="重复行"),
    ])
    db_session.commit()

    after = preview_inventory_snapshot_files(db_session, files)
    assert after.conflict_count == 0
    assert after.unmatched_count == 0
    assert after.duplicate_count == 1
    assert after.quantity_conflict_count == 0

    snapshot, _ = create_inventory_snapshot_from_files(db_session, files, now=NOW, data_at=NOW)
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1
    statuses = {line.matching_status for line in snapshot.lines}
    assert "conflict" not in statuses
    assert "unmatched" not in statuses
