from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
    create_inventory_snapshot, latest_inventory_for_product, manual_match_line,
    map_line_warehouse, purchase_assistance,
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


def test_matching_priority_jan_and_no_jan_code(db_session):
    initialize_default_locations(db_session)
    by_code = product(db_session, "货号优先", jan="4570110290418", code="CODE-FIRST")
    by_jan = product(db_session, "JAN候选", jan="4901234567894")
    no_jan = product(db_session, "无JAN货号商品", code="NO-JAN-CODE")
    snapshot, _ = create_inventory_snapshot(db_session, "priority.xlsx", workbook([
        {"name": "冲突标识行", "code": by_code.qinsi_product_code, "jan": by_jan.jan, "quantity": 1},
        {"name": "JAN匹配行", "code": "UNKNOWN-CODE", "jan": by_jan.jan, "quantity": 2},
        {"name": "无JAN行", "code": no_jan.qinsi_product_code, "quantity": 3},
    ]), now=NOW)
    assert [(line.product_id, line.matching_method) for line in snapshot.lines] == [
        (by_code.id, "qinsi_product_code"), (by_jan.id, "jan"), (no_jan.id, "qinsi_product_code"),
    ]


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
    locations = {row.display_name: row for row in initialize_default_locations(db_session)}
    item = product(db_session, "聚合商品", code="AGG-1")
    snapshot, _ = create_inventory_snapshot(db_session, "aggregate.xlsx", workbook([
        {"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 2},
        {"name": item.name_cn, "code": item.qinsi_product_code, "quantity": 3},
    ]), data_at=NOW - timedelta(hours=80), now=NOW)
    map_line_warehouse(db_session, snapshot.lines[1].id, locations["2025千羽"].id)
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
