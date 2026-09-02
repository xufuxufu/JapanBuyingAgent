from __future__ import annotations

import base64
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from openpyxl import Workbook
from sqlalchemy import func, select

from app.analytics_service import _latest_inventory_states
from app.location_service import initialize_default_locations
from app.models import (
    Location, Product, ProductWatchConfig, PurchaseBatch, PurchaseBatchItem,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, QinsiProductMapping,
    Receipt, ReceiptBatch, ReceiptItem,
)
from app.qinsi_export import ExportRow, _template_bytes
from app.qinsi_inventory import (
    INVENTORY_REGION_CHINA, INVENTORY_REGION_JAPAN,
    analyze_multi_file_completeness, create_inventory_snapshot, create_inventory_snapshot_from_files,
    inventory_region_for_location, latest_inventory_for_product, manual_match_line, map_line_warehouse,
    preview_inventory_snapshot_files, purchase_assistance,
)


NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)


def product(db, name: str, *, jan: str | None = None, code: str | None = None) -> Product:
    item = Product(name_cn=name, name_ja=name, jan=jan, qinsi_product_code=code, product_origin="qinsi")
    db.add(item)
    db.commit()
    return item


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


def pending_purchase(db, item: Product, quantity: int) -> None:
    locations = {row.display_name: row for row in initialize_default_locations(db)}
    source = ReceiptBatch(batch_no=f"INV-RB-{item.id}", status="confirmed")
    receipt = Receipt(
        batch=source, raw_store_name="库存测试店", confirmation_status="confirmed",
        review_status="reviewed", confirmed_at=NOW,
    )
    receipt_item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name=item.name_cn, product_id=item.id,
        quantity=quantity, unit_price=100, line_total=quantity * 100,
        discount_amount=0, confidence=1, review_status="confirmed",
    )
    db.add_all([source, receipt, receipt_item])
    db.flush()
    batch = PurchaseBatch(
        batch_no=f"INV-PB-{item.id}", receipt_id=receipt.id, gpt_batch_id=source.id,
        confirmed_at=NOW, status="confirmed",
        default_initial_location_id=locations["日本家里库存"].id,
        default_qinsi_warehouse_id=locations["新日本仓库"].id,
    )
    db.add(batch)
    db.flush()
    db.add(PurchaseBatchItem(
        purchase_batch_id=batch.id, product_id=item.id, receipt_item_id=receipt_item.id,
        quantity=quantity, unit_price=100, actual_line_amount=quantity * 100,
        initial_location_id=locations["日本家里库存"].id,
        qinsi_target_warehouse_id=locations["新日本仓库"].id,
    ))
    db.commit()


def test_legal_snapshot_and_duplicate_file_are_idempotent(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "合法快照商品", jan="4901234567894", code="QS-LEGAL")
    content = workbook([{"name": item.name_cn, "jan": item.jan, "code": item.qinsi_product_code, "quantity": 6}])
    first, reused_first = create_inventory_snapshot(db_session, "inventory.xlsx", content, data_at=NOW, now=NOW)
    second, reused_second = create_inventory_snapshot(db_session, "inventory.xlsx", content, data_at=NOW, now=NOW)
    assert not reused_first and reused_second and first.id == second.id
    assert (first.total_rows, first.success_rows, first.unmatched_rows, first.exception_rows) == (1, 1, 0, 0)
    assert first.lines[0].quantity == 6 and first.lines[0].product_id == item.id
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1


def test_matching_cross_validates_code_and_jan(db_session):
    initialize_default_locations(db_session)
    verified = product(db_session, "双重确认商品", jan="4570110290418", code="VERIFIED-1")
    by_code = product(db_session, "货号唯一商品", jan="4901234567895", code="CODE-ONLY")
    by_jan = product(db_session, "JAN唯一商品", jan="4901234567894")
    no_jan = product(db_session, "无JAN货号商品", code="NO-JAN-CODE")
    snapshot, _ = create_inventory_snapshot(db_session, "priority.xlsx", workbook([
        {"name": "双重确认行", "code": verified.qinsi_product_code, "jan": verified.jan, "quantity": 1},
        {"name": "货号命中行", "code": by_code.qinsi_product_code, "quantity": 2},
        {"name": "JAN命中行", "code": "UNKNOWN-CODE", "jan": by_jan.jan, "quantity": 3},
        {"name": "无JAN行", "code": no_jan.qinsi_product_code, "quantity": 4},
    ]), now=NOW)
    assert [(line.product_id, line.matching_method, line.matching_status) for line in snapshot.lines] == [
        (verified.id, "code_jan_verified", "matched"),
        (by_code.id, "qinsi_product_code", "matched"),
        (by_jan.id, "jan", "matched"),
        (no_jan.id, "qinsi_product_code", "matched"),
    ]


def test_code_and_jan_pointing_to_different_products_is_conflict_not_silent_pick(db_session):
    # Regression for the audited bug: code hitting one Product and JAN
    # hitting a DIFFERENT Product must never silently pick one side. It must
    # become matching_status="conflict", product_id stays unbound, and the
    # raw code/JAN/warehouse/quantity are preserved untouched for review.
    initialize_default_locations(db_session)
    by_code = product(db_session, "货号商品", jan="4570110290418", code="CODE-FIRST")
    by_jan = product(db_session, "JAN商品", jan="4901234567894")
    snapshot, _ = create_inventory_snapshot(db_session, "conflict.xlsx", workbook([
        {"name": "冲突行", "code": by_code.qinsi_product_code, "jan": by_jan.jan, "quantity": 5},
    ]), now=NOW)
    line = snapshot.lines[0]
    assert line.matching_status == "conflict"
    assert line.product_id is None
    assert line.qinsi_product_code == by_code.qinsi_product_code
    assert line.jan == by_jan.jan
    assert line.raw_warehouse_name == "新日本仓库"
    assert line.quantity == 5
    assert by_code.internal_sku in line.error_message and by_jan.internal_sku in line.error_message
    assert snapshot.exception_rows == 1


def test_same_code_same_warehouse_different_quantity_is_conflict(db_session):
    # Two rows for the same qinsi_product_code + same warehouse disagreeing
    # on quantity must never be silently summed, max'd, or last-wins'd.
    initialize_default_locations(db_session)
    item = product(db_session, "数量冲突商品", code="QTY-CONFLICT-1")
    snapshot, _ = create_inventory_snapshot(db_session, "qty-conflict.xlsx", workbook([
        {"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 2},
        {"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 9},
    ]), now=NOW)
    assert [line.matching_status for line in snapshot.lines] == ["ignored", "ignored"]
    assert [line.matching_method for line in snapshot.lines] == ["quantity_conflict", "quantity_conflict"]
    assert all(line.quantity in (2, 9) for line in snapshot.lines)
    view = latest_inventory_for_product(db_session, item.id, now=NOW)
    assert view.total_quantity in (None, 0)


def test_fuzzy_name_never_auto_matches_and_manual_mapping_is_reused(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "完全相同商品名称")
    first, _ = create_inventory_snapshot(db_session, "unmatched.xlsx", workbook([
        {"name": "完全相同商品名称", "code": "MAP-LATER", "quantity": 1},
    ]), now=NOW)
    assert first.lines[0].matching_status == "unmatched" and first.lines[0].product_id is None
    manual_match_line(db_session, first.lines[0].id, item.id)
    assert db_session.scalar(select(QinsiProductMapping).where(QinsiProductMapping.qinsi_product_code == "MAP-LATER")).product_id == item.id
    second, _ = create_inventory_snapshot(db_session, "mapped.xlsx", workbook([
        {"name": "任意不同名称", "code": "MAP-LATER", "quantity": 2},
    ]), now=NOW + timedelta(minutes=1))
    assert second.lines[0].product_id == item.id and second.lines[0].matching_method == "confirmed_mapping"


def test_unknown_warehouse_is_exception_and_not_created(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "未知仓商品", code="UNKNOWN-WH")
    snapshot, _ = create_inventory_snapshot(db_session, "unknown-warehouse.xlsx", workbook([
        {"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 4},
    ], warehouse="不存在仓库"), now=NOW)
    line = snapshot.lines[0]
    assert line.matching_status == "matched" and line.warehouse_id is None and line.warehouse_status == "unknown"
    assert snapshot.exception_rows == 1 and snapshot.status == "completed_with_issues"
    assert db_session.scalar(select(func.count()).select_from(Location).where(Location.display_name == "不存在仓库")) == 0


def test_latest_inventory_aggregates_by_warehouse_and_marks_stale(db_session):
    # Two distinct warehouses merged into ONE snapshot via multi-file import
    # (each raw row correctly names its own warehouse) -- this is the
    # realistic shape; two rows sharing one raw warehouse name with differing
    # quantities is now a quantity_conflict, not "the same product counted
    # twice", see test_same_code_same_warehouse_different_quantity_is_conflict.
    initialize_default_locations(db_session)
    item = product(db_session, "聚合商品", code="AGG-1")
    files = [
        ("japan.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 2}], "新日本仓库")),
        ("qianyu.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 3}], "2025千羽")),
    ]
    create_inventory_snapshot_from_files(db_session, files, data_at=NOW - timedelta(hours=80), now=NOW)
    view = latest_inventory_for_product(db_session, item.id, now=NOW)
    assert {row.warehouse.display_name: row.quantity for row in view.warehouses} == {"新日本仓库": 2, "2025千羽": 3}
    assert view.total_quantity == 5 and view.is_stale is True


def test_newer_single_warehouse_snapshot_does_not_hide_other_warehouses(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "分仓最新快照商品", code="PER-WAREHOUSE-1")
    create_inventory_snapshot(
        db_session,
        "new-japan.xlsx",
        workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 2}], "新日本仓库"),
        data_at=NOW,
        now=NOW,
    )
    create_inventory_snapshot(
        db_session,
        "qianyu.xlsx",
        workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 3}], "2025千羽"),
        data_at=NOW + timedelta(minutes=1),
        now=NOW + timedelta(minutes=1),
    )
    view = latest_inventory_for_product(db_session, item.id, now=NOW + timedelta(minutes=2))
    _, states = _latest_inventory_states(
        db_session, {item.id}, now=NOW + timedelta(minutes=2),
    )
    assert {row.warehouse.display_name: row.quantity for row in view.warehouses} == {
        "新日本仓库": 2,
        "2025千羽": 3,
    }
    assert view.total_quantity == 5
    assert states[item.id]["quantity"] == 5


def test_low_stock_target_price_and_pending_purchase_are_separate(db_session):
    initialize_default_locations(db_session)
    low = product(db_session, "低库存达价", code="LOW-1")
    low.low_stock_threshold = 3
    db_session.add(ProductWatchConfig(
        product_id=low.id, enabled=True, user_target_price=900, effective_target_price=900,
        frequency_tier="normal", current_lowest_price=800,
    ))
    db_session.commit()
    create_inventory_snapshot(db_session, "low.xlsx", workbook([
        {"name": low.name_cn, "code": low.qinsi_product_code, "quantity": 1},
    ]), data_at=NOW, now=NOW)
    low_help = purchase_assistance(db_session, low, now=NOW)
    assert low_help.base_status == "stock_low" and "可考虑补货" in low_help.message

    empty = product(db_session, "零库存待提交", code="EMPTY-1")
    create_inventory_snapshot(db_session, "empty.xlsx", workbook([
        {"name": empty.name_cn, "code": empty.qinsi_product_code, "quantity": 0},
    ]), data_at=NOW, now=NOW)
    pending_purchase(db_session, empty, 5)
    empty_view = latest_inventory_for_product(db_session, empty.id, now=NOW)
    empty_help = purchase_assistance(db_session, empty, inventory=empty_view, now=NOW)
    assert empty_view.total_quantity == 0
    assert empty_help.base_status == "out_of_stock" and empty_help.status == "incoming_or_pending"
    assert empty_help.pending_quantity == 5 and "未计入秦丝快照库存" in empty_help.message


def test_snapshot_product_and_watch_pages_return_200(client):
    http, db, _ = client
    initialize_default_locations(db)
    item = product(db, "页面库存商品", jan="4901234567894", code="PAGE-1")
    db.add(ProductWatchConfig(product_id=item.id, enabled=False, frequency_tier="normal"))
    db.commit()
    content = workbook([
        {"name": item.name_cn, "jan": item.jan, "code": item.qinsi_product_code, "quantity": 2},
    ])
    upload = http.post(
        "/qinsi-inventory-snapshots/upload",
        files={"file": ("pages.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=False,
    )
    repeated = http.post(
        "/qinsi-inventory-snapshots/upload",
        files={"file": ("pages.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        follow_redirects=False,
    )
    snapshot = db.scalar(select(QinsiInventorySnapshot))
    assert upload.status_code == repeated.status_code == 303
    assert db.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1
    assert http.get("/qinsi-inventory-snapshots").status_code == 200
    detail = http.get(f"/qinsi-inventory-snapshots/{snapshot.id}")
    assert detail.status_code == 200 and "秦丝" in detail.text
    assert http.get(f"/products/{item.id}").status_code == 200
    assert http.get("/watched-products").status_code == 200
    download = http.get(f"/qinsi-inventory-snapshots/{snapshot.id}/download")
    assert download.status_code == 200 and download.content == snapshot.file_content


# ---------------- multi-file merge: one QinSi export, one snapshot ----------------
# A "segmented" QinSi inventory export (e.g. 16 range-named Excel files that are
# really one point-in-time export) must become exactly one QinsiInventorySnapshot
# with a single snapshot-wide imported_at/data_at, matching + duplicate detection
# run across the merged row set rather than per file.


def test_16_files_merge_into_a_single_snapshot(db_session):
    initialize_default_locations(db_session)
    files = [
        (f"导出({i * 2 + 1}-{i * 2 + 2}).xlsx", workbook([
            {"name": f"合并商品{i}A", "code": f"MERGE-{i}A", "quantity": 1},
            {"name": f"合并商品{i}B", "code": f"MERGE-{i}B", "quantity": 1},
        ]))
        for i in range(16)
    ]
    snapshot, reused = create_inventory_snapshot_from_files(db_session, files, now=NOW, data_at=NOW)
    assert not reused
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1
    assert snapshot.total_rows == 32
    assert len({line.snapshot_id for line in snapshot.lines}) == 1
    # SQLite round-trips DateTime(timezone=True) as naive, so compare
    # wall-clock values rather than exact tz-aware equality.
    assert all(line.snapshot.imported_at.replace(tzinfo=None) == NOW.replace(tzinfo=None) for line in snapshot.lines)
    assert all(line.snapshot.data_at.replace(tzinfo=None) == NOW.replace(tzinfo=None) for line in snapshot.lines)


def _formal_workbook(rows: list[tuple[str, str]]) -> bytes:
    # Minimal formal-format ("qinsi_product_list") fixture: unlike the old
    # qinsi_goods_template (workbook() below), this has no pre-formatted
    # padding rows, so row counts here reflect only the rows actually given.
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["商品名称", "货号", "品牌", "分类", "单位", "采购价"])
    for name, code in rows:
        ws.append([name, code, "", "", "个", "100"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_completeness_detects_contiguous_ranges(db_session):
    files = [
        ("导出(1-2).xlsx", _formal_workbook([("A", "C1"), ("B", "C2")])),
        ("导出(3-4).xlsx", _formal_workbook([("C", "C3"), ("D", "C4")])),
    ]
    report = analyze_multi_file_completeness(files)
    assert report.all_ranges_parsed
    assert report.gaps == () and report.overlaps == () and not report.header_mismatch
    assert report.expected_min == 1 and report.expected_max == 4 and report.expected_total_from_ranges == 4
    assert report.actual_total_rows == 4
    assert not report.has_blocking_issue


def test_completeness_detects_gap(db_session):
    files = [
        ("导出(1-2).xlsx", _formal_workbook([("A", "C1"), ("B", "C2")])),
        ("导出(5-6).xlsx", _formal_workbook([("C", "C3"), ("D", "C4")])),
    ]
    report = analyze_multi_file_completeness(files)
    assert report.gaps == ((3, 4),)
    assert report.has_blocking_issue


def test_completeness_detects_overlap(db_session):
    files = [
        ("导出(1-3).xlsx", _formal_workbook([("A", "C1")])),
        ("导出(2-4).xlsx", _formal_workbook([("B", "C2")])),
    ]
    report = analyze_multi_file_completeness(files)
    assert report.overlaps == ((1, 3, 2, 4),)
    assert report.has_blocking_issue


def test_completeness_detects_header_mismatch(db_session):
    files = [
        ("导出(1-1).xlsx", workbook([{"name": "A", "code": "C1", "quantity": 1}])),
        ("导出(2-2).xlsx", _formal_workbook([("格式不同商品", "FORMAL-1")])),
    ]
    report = analyze_multi_file_completeness(files)
    assert report.header_mismatch
    assert report.has_blocking_issue


def test_unparseable_filename_does_not_block_but_is_not_range_checked(db_session):
    files = [
        ("导出(1-1).xlsx", _formal_workbook([("A", "C1")])),
        ("随手导出的文件.xlsx", _formal_workbook([("B", "C2")])),
    ]
    report = analyze_multi_file_completeness(files)
    assert not report.all_ranges_parsed
    assert not report.has_blocking_issue
    assert report.actual_total_rows == 2


def test_exact_duplicate_row_across_files_counts_once(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "去重商品", jan="4901234567894", code="DUPE-1")
    row = {"name": item.name_cn, "jan": item.jan, "code": item.qinsi_product_code, "quantity": 7}
    file_a = workbook([row], "新日本仓库")
    file_b = workbook([row], "新日本仓库")
    snapshot, _ = create_inventory_snapshot_from_files(db_session, [("dup-a.xlsx", file_a), ("dup-b.xlsx", file_b)], now=NOW)
    assert [line.matching_status for line in snapshot.lines] == ["matched", "ignored"]
    assert snapshot.lines[1].matching_method == "duplicate"
    assert "完全重复行" in snapshot.lines[1].error_message
    assert snapshot.lines[1].qinsi_product_code == item.qinsi_product_code  # raw data preserved, not dropped
    view = latest_inventory_for_product(db_session, item.id, now=NOW)
    assert view.total_quantity == 7  # counted once, not 14


def test_same_product_different_warehouse_is_not_duplicate(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "跨仓商品", code="CROSS-WH-1")
    file_a = workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 4}], "新日本仓库")
    file_b = workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 4}], "2025千羽")
    snapshot, _ = create_inventory_snapshot_from_files(db_session, [("a.xlsx", file_a), ("b.xlsx", file_b)], now=NOW)
    assert [line.matching_status for line in snapshot.lines] == ["matched", "matched"]
    view = latest_inventory_for_product(db_session, item.id, now=NOW)
    assert view.total_quantity == 8


def test_preview_does_not_write_any_snapshot(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "预览商品", code="PREVIEW-1")
    files = [("preview.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 6}]))]
    preview = preview_inventory_snapshot_files(db_session, files)
    assert preview.matched_count == 1
    assert preview.total_rows == 1
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 0


def test_preview_reports_conflicts_and_duplicates(db_session):
    initialize_default_locations(db_session)
    by_code = product(db_session, "预览货号商品", jan="4570110290418", code="PREVIEW-CODE")
    by_jan = product(db_session, "预览JAN商品", jan="4901234567894")
    row = {"name": "重复行", "code": "DUP-CODE", "quantity": 1}
    files = [
        ("preview-a.xlsx", workbook([
            {"name": "冲突行", "code": by_code.qinsi_product_code, "jan": by_jan.jan, "quantity": 1},
            row,
        ])),
        ("preview-b.xlsx", workbook([row])),
    ]
    preview = preview_inventory_snapshot_files(db_session, files)
    assert preview.conflict_count == 1 and len(preview.conflicts) == 1
    assert preview.duplicate_count == 1 and len(preview.duplicates) == 1
    assert db_session.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 0


# ---------------- warehouse region mapping ----------------


def test_new_japan_warehouse_maps_to_japan_region(db_session):
    locations = {row.internal_code: row for row in initialize_default_locations(db_session)}
    assert inventory_region_for_location(locations["QW-NEW-JAPAN"]) == INVENTORY_REGION_JAPAN


def test_china_warehouses_including_no_barcode_map_to_china_region(db_session):
    locations = {row.internal_code: row for row in initialize_default_locations(db_session)}
    zhaocaimao_dian = Location(
        internal_code="QW-QINSI-1DE1039D20FB", display_name="招财猫店",
        location_type="qinsi_warehouse", is_qinsi_warehouse=True, is_active=True,
    )
    db_session.add(zhaocaimao_dian)
    db_session.commit()
    china_codes = ["QW-2025-QIANYU", "QW-2025-ZHAOCAIMAO", "QW-2026-QIANYU", "QW-2026-ZHAOCAIMAO", "QW-NO-BARCODE"]
    for code in china_codes:
        assert inventory_region_for_location(locations[code]) == INVENTORY_REGION_CHINA
    assert inventory_region_for_location(zhaocaimao_dian) == INVENTORY_REGION_CHINA
    # "无条码商品" must not be silently excluded just because of its name.
    assert inventory_region_for_location(locations["QW-NO-BARCODE"]) == INVENTORY_REGION_CHINA


def test_china_region_quantity_aggregates_across_four_warehouses(db_session):
    initialize_default_locations(db_session)
    item = product(db_session, "中国区域汇总商品", code="CHINA-AGG-1")
    files = [
        ("qianyu.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 2}], "2026千羽")),
        ("zhaocaimao.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 3}], "2026招财猫")),
        ("no-barcode.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 4}], "无条码商品")),
        ("japan.xlsx", workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 10}], "新日本仓库")),
    ]
    preview = preview_inventory_snapshot_files(db_session, files)
    assert preview.china_quantity == 2 + 3 + 4
    assert preview.japan_quantity == 10


# ---------------- multi-file upload HTTP flow: preview then confirm ----------------


def test_upload_batch_preview_then_confirm_creates_one_snapshot(client):
    http, db, _ = client
    initialize_default_locations(db)
    item = product(db, "批量上传商品", code="BATCH-UI-1")
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    bytes_a = workbook([{"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 3}], "新日本仓库")
    bytes_b = workbook([{"name": "另一商品", "code": "BATCH-UI-2", "quantity": 5}], "2025千羽")

    preview = http.post(
        "/qinsi-inventory-snapshots/upload-batch",
        files=[("files", ("导出(1-1).xlsx", bytes_a, content_type)), ("files", ("导出(2-2).xlsx", bytes_b, content_type))],
    )
    assert preview.status_code == 200
    assert "库存快照预览" in preview.text
    assert db.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 0

    payload = {
        "filenames": ["导出(1-1).xlsx", "导出(2-2).xlsx"],
        "file_contents": [base64.b64encode(bytes_a).decode("ascii"), base64.b64encode(bytes_b).decode("ascii")],
        "data_at": "",
    }
    confirm = http.post("/qinsi-inventory-snapshots/confirm-batch", data=payload, follow_redirects=False)
    assert confirm.status_code == 303
    assert db.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1
    snapshot = db.scalar(select(QinsiInventorySnapshot))
    assert snapshot.total_rows == 2
    assert snapshot.original_filename.endswith(".zip")

    download = http.get(f"/qinsi-inventory-snapshots/{snapshot.id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"


def test_confirm_batch_blocks_on_completeness_issue_without_override(client):
    http, db, _ = client
    initialize_default_locations(db)
    bytes_a = workbook([{"name": "A", "code": "OVR-1", "quantity": 1}])
    bytes_b = workbook([{"name": "B", "code": "OVR-2", "quantity": 1}])
    payload = {
        "filenames": ["导出(1-1).xlsx", "导出(5-5).xlsx"],
        "file_contents": [base64.b64encode(bytes_a).decode("ascii"), base64.b64encode(bytes_b).decode("ascii")],
        "data_at": "",
    }
    blocked = http.post("/qinsi-inventory-snapshots/confirm-batch", data=payload)
    assert blocked.status_code == 422
    assert db.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 0

    payload_with_override = {**payload, "override_completeness_warning": "true"}
    allowed = http.post("/qinsi-inventory-snapshots/confirm-batch", data=payload_with_override, follow_redirects=False)
    assert allowed.status_code == 303
    assert db.scalar(select(func.count()).select_from(QinsiInventorySnapshot)) == 1
