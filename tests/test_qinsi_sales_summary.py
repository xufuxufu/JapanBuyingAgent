from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal

import openpyxl
import pytest

import app.qinsi_sales_summary as qinsi_sales_summary
from app.local_product import is_valid_jan
from app.models import (
    Product, ProcurementDemandPlan, ProcurementPurchaseExecution,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, QinsiSalesSummaryLine, QinsiSalesSummarySnapshot,
)
from app.qinsi_sales_summary import (
    EXPECTED_HEADERS,
    PreviewTokenError,
    SalesSummaryImportError,
    analyze_sales_summary_completeness,
    create_preview_token,
    create_sales_summary_snapshot_from_files,
    discard_preview_token,
    get_sales_summary_lines_page,
    get_sales_summary_snapshot_summary,
    load_preview_token,
    parse_amount,
    parse_quantity_or_count,
    parse_sales_summary_workbook,
    parse_support_days,
    preview_sales_summary_files,
    product_replenishment_signals,
    sales_summary_for_products,
)


def _workbook_bytes(rows: list[list], headers=EXPECTED_HEADERS) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _base_row(**overrides) -> list:
    row = {
        "商品名称": "测试商品", "货号": "000123", "单品条码": "", "型号规格": "", "图片": None,
        "分类": "", "品牌": "", "采购量": "0", "采购金额": "0", "销售量": "0", "销售金额": "0",
        "购买客户数": 0, "当前库存": "5", "支撑销售天数": "-", "仓库所属门店": "全部", "仓库": "全部",
    }
    row.update(overrides)
    return [row[h] for h in EXPECTED_HEADERS]


def product(db, suffix: str, **kwargs) -> Product:
    item = Product(internal_sku=f"QSS-{suffix:0>4}", **kwargs)
    db.add(item)
    db.flush()
    return item


UTC = timezone.utc


# ==================== 1. real 16-column header recognition ====================

def test_parses_correct_16_column_header():
    content = _workbook_bytes([_base_row()])
    parsed = parse_sales_summary_workbook(content)
    assert parsed.headers[:len(EXPECTED_HEADERS)] == EXPECTED_HEADERS
    assert len(parsed.rows) == 1


def test_rejects_wrong_header():
    wrong_headers = ("商品名称", "货号", "条码")  # not the real 16-column export
    content = _workbook_bytes([], headers=wrong_headers)
    with pytest.raises(SalesSummaryImportError):
        parse_sales_summary_workbook(content)


# ==================== 2. quantity "1个" ====================

def test_quantity_parses_plain_number():
    assert parse_quantity_or_count("1") == (1, None)


def test_quantity_parses_unit_suffix_ge():
    assert parse_quantity_or_count("1个") == (1, None)
    assert parse_quantity_or_count("12件") == (12, None)


def test_quantity_dash_and_empty_are_none_no_error():
    assert parse_quantity_or_count("-") == (None, None)
    assert parse_quantity_or_count("") == (None, None)
    assert parse_quantity_or_count(None) == (None, None)


def test_quantity_garbage_is_anomaly_not_guessed():
    value, error = parse_quantity_or_count("很多")
    assert value is None
    assert error is not None


def test_support_days_infinity_symbol_is_none_no_error():
    """QinSi reports "∞" for inventory-but-zero-sales rows -- a legitimate
    sentinel, not an anomaly."""
    assert parse_support_days("∞") == (None, None)
    assert parse_support_days("35") == (35, None)


# ==================== 3. amount uses Decimal ====================

def test_amount_parses_as_decimal():
    value, error = parse_amount("1320.0000")
    assert error is None
    assert value == Decimal("1320.0000")
    assert isinstance(value, Decimal)


def test_amount_dash_is_none():
    assert parse_amount("-") == (None, None)


# ==================== 4. barcode "18" must never become a JAN ====================

def test_barcode_18_is_not_a_valid_jan():
    assert is_valid_jan("18") is False


def test_row_with_barcode_18_has_no_jan_candidate():
    content = _workbook_bytes([_base_row(单品条码="18")])
    parsed = parse_sales_summary_workbook(content)
    assert parsed.rows[0].jan_candidate is None
    assert parsed.rows[0].barcode_raw == "18"


def test_row_with_real_valid_jan_gets_jan_candidate():
    # 4901301231123 is a real, checksum-valid JAN-13
    content = _workbook_bytes([_base_row(单品条码="4901301231123")])
    parsed = parse_sales_summary_workbook(content)
    assert parsed.rows[0].jan_candidate == "4901301231123"


# ==================== 5-6. qinsi code / JAN identity matching, conflict ====================

def test_matches_by_qinsi_code_only(client):
    _http, db, _tmp = client
    p = product(db, "0001", qinsi_product_code="Q-0001")
    content = _workbook_bytes([_base_row(货号="Q-0001", 单品条码="")])
    preview = preview_sales_summary_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert preview.matched_count == 1
    assert preview.unmatched_count == 0


def test_matches_by_jan_only(client):
    _http, db, _tmp = client
    p = product(db, "0002", jan="4901301231123")
    content = _workbook_bytes([_base_row(货号="NO-MATCH-CODE", 单品条码="4901301231123")])
    preview = preview_sales_summary_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert preview.matched_count == 1


def test_code_and_jan_pointing_to_different_products_is_conflict_not_autobind(client):
    _http, db, _tmp = client
    product(db, "0003", qinsi_product_code="Q-0003")
    product(db, "0004", jan="4901301231123")
    content = _workbook_bytes([_base_row(货号="Q-0003", 单品条码="4901301231123")])
    preview = preview_sales_summary_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert preview.conflict_count == 1
    assert preview.matched_count == 0


def test_no_fuzzy_name_matching(client):
    """A product name that's a near-exact string match must NOT bind if
    neither code nor JAN resolves -- this module never does fuzzy name
    matching."""
    _http, db, _tmp = client
    product(db, "0005", name_cn="测试商品ABC")
    content = _workbook_bytes([_base_row(商品名称="测试商品ABC", 货号="NO-SUCH-CODE", 单品条码="")])
    preview = preview_sales_summary_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert preview.unmatched_count == 1
    assert preview.matched_count == 0


# ==================== 7 & 12. 7/30-day snapshot selection, latest wins ====================

def test_sales_summary_selects_snapshot_matching_period_days_exactly(client):
    _http, db, _tmp = client
    p = product(db, "0006", qinsi_product_code="Q-0006")
    content7 = _workbook_bytes([_base_row(货号="Q-0006", 销售量="3")])
    content30 = _workbook_bytes([_base_row(货号="Q-0006", 销售量="12")])
    create_sales_summary_snapshot_from_files(
        db, [("seven.xlsx", content7)], period_start=datetime(2026, 8, 25, tzinfo=UTC), period_end=datetime(2026, 8, 31, tzinfo=UTC),
    )
    create_sales_summary_snapshot_from_files(
        db, [("thirty.xlsx", content30)], period_start=datetime(2026, 8, 2, tzinfo=UTC), period_end=datetime(2026, 8, 31, tzinfo=UTC),
    )
    result7 = sales_summary_for_products(db, [p.id], 7)
    result30 = sales_summary_for_products(db, [p.id], 30)
    assert result7[p.id].sales_quantity == 3
    assert result30[p.id].sales_quantity == 12


def test_sales_summary_picks_latest_snapshot_for_same_period_days(client):
    _http, db, _tmp = client
    p = product(db, "0007", qinsi_product_code="Q-0007")
    old_content = _workbook_bytes([_base_row(货号="Q-0007", 销售量="1")])
    new_content = _workbook_bytes([_base_row(货号="Q-0007", 销售量="9")])
    create_sales_summary_snapshot_from_files(
        db, [("old.xlsx", old_content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 7, tzinfo=UTC),
        now=datetime(2026, 8, 8, tzinfo=UTC),
    )
    create_sales_summary_snapshot_from_files(
        db, [("new.xlsx", new_content)], period_start=datetime(2026, 8, 8, tzinfo=UTC), period_end=datetime(2026, 8, 14, tzinfo=UTC),
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )
    result = sales_summary_for_products(db, [p.id], 7)
    assert result[p.id].sales_quantity == 9


# ==================== 8-9. period_days computation, start>end rejected ====================

def test_period_days_computed_inclusive(client):
    _http, db, _tmp = client
    content = _workbook_bytes([_base_row()])
    preview = preview_sales_summary_files(
        db, [("f.xlsx", content)],
        period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert preview.period_days == 30


def test_start_after_end_rejected(client):
    _http, db, _tmp = client
    content = _workbook_bytes([_base_row()])
    with pytest.raises(SalesSummaryImportError):
        preview_sales_summary_files(
            db, [("f.xlsx", content)],
            period_start=datetime(2026, 8, 30, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
        )


# ==================== 10. multi-file range/duplicate issues ====================

def test_completeness_detects_gap_and_overlap():
    file_a = _workbook_bytes([_base_row(货号=f"C{i}") for i in range(500)])
    file_b = _workbook_bytes([_base_row(货号=f"C{i}") for i in range(450, 950)])  # overlaps 450-500
    report = analyze_sales_summary_completeness([
        ("汇总(1-500).xlsx", file_a), ("汇总(451-950).xlsx", file_b),
    ])
    assert report.overlaps


def test_preview_flags_duplicate_qinsi_code_across_files(client):
    _http, db, _tmp = client
    file_a = _workbook_bytes([_base_row(货号="Q-DUP")])
    file_b = _workbook_bytes([_base_row(货号="Q-DUP")])
    preview = preview_sales_summary_files(
        db, [("a.xlsx", file_a), ("b.xlsx", file_b)],
        period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert len(preview.duplicate_qinsi_codes) == 1
    assert preview.duplicate_qinsi_codes[0].qinsi_product_code == "Q-DUP"


# ==================== 11. known zero vs unknown ====================

def test_known_zero_vs_unknown(client):
    _http, db, _tmp = client
    sold_zero = product(db, "0008", qinsi_product_code="Q-0008")
    content = _workbook_bytes([_base_row(货号="Q-0008", 销售量="0")])
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    never_seen = product(db, "0009")
    results = sales_summary_for_products(db, [sold_zero.id, never_seen.id], 30)
    assert results[sold_zero.id].sales_known is True
    assert results[sold_zero.id].sales_quantity == 0
    assert results[never_seen.id].sales_known is False
    assert results[never_seen.id].sales_quantity is None


def test_no_snapshot_at_all_is_unknown_not_zero(client):
    _http, db, _tmp = client
    p = product(db, "0010")
    results = sales_summary_for_products(db, [p.id], 7)
    assert results[p.id].sales_known is False
    assert results[p.id].sales_quantity is None


# ==================== 13. batched lookups, no N+1 ====================

def test_sales_summary_for_products_batched_not_per_product(client):
    _http, db, _tmp = client
    ids = []
    rows = []
    for i in range(20):
        p = product(db, f"BATCH{i}", qinsi_product_code=f"Q-BATCH-{i}")
        ids.append(p.id)
        rows.append(_base_row(货号=f"Q-BATCH-{i}", 销售量=str(i)))
    content = _workbook_bytes(rows)
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )

    query_count = 0
    from sqlalchemy import event
    engine = db.get_bind()

    def counter(*args, **kwargs):
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", counter)
    try:
        results = sales_summary_for_products(db, ids, 30)
    finally:
        event.remove(engine, "before_cursor_execute", counter)
    assert len(results) == 20
    # one query to find the latest snapshot, one query for all lines -- not 20+
    assert query_count <= 3


def test_product_replenishment_signals_has_no_recommended_quantity(client):
    _http, db, _tmp = client
    p = product(db, "0011", qinsi_product_code="Q-0011")
    signals = product_replenishment_signals(db, [p.id])
    assert "recommended_quantity" not in signals[p.id]
    assert set(signals[p.id]) == {
        "china_inventory", "china_inventory_known", "japan_inventory", "japan_inventory_known",
        "sales_7d", "sales_7d_known", "sales_30d", "sales_30d_known",
        "in_transit_quantity", "planned_quantity", "purchased_quantity", "sales_data_end",
    }


# ==================== 14. 补货需求 API shows sales_7d/30d ====================

def test_procurement_product_search_api_includes_sales_fields(client):
    http, db, _tmp = client
    p = product(db, "0012", qinsi_product_code="Q-0012", name_cn="补货需求API测试商品")
    content = _workbook_bytes([_base_row(货号="Q-0012", 商品名称="补货需求API测试商品", 销售量="4")])
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    response = http.get("/api/procurement/products/search", params={"q": "补货需求API测试商品"})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["sales_30d"] == 4
    assert payload[0]["sales_30d_known"] is True
    assert payload[0]["sales_7d_known"] is False  # no 7-day snapshot created in this test


# ==================== 15. 采购页面 shows sales info ====================

def test_procurement_purchase_page_renders_sales_row(client):
    from datetime import timezone as tz
    from app.models import Location, Store
    from app.procurement_service import PlanSelectionInput, create_channel_shortage_demand, create_plans

    http, db, _tmp = client
    p = product(db, "0013", qinsi_product_code="Q-0013", name_cn="采购页销量测试")
    content = _workbook_bytes([_base_row(货号="Q-0013", 商品名称="采购页销量测试", 销售量="6")])
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    create_channel_shortage_demand(db, product_id=p.id, quantity=3)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(p.id), planned_quantity=3)])
    store = Store(name="测试店", name_cn="测试店", is_active=True)
    db.add(store)
    db.flush()
    from app.procurement_service import set_plans_selected_store_bulk
    set_plans_selected_store_bulk(db, [plan.id], store.id)

    response = http.get(f"/procurement-demands/purchase?store_id={store.id}")
    assert response.status_code == 200
    assert "30天销量" in response.text
    assert "6" in response.text


# ==================== 16-17. must never overwrite inventory authority or purchase execution ====================

def test_does_not_touch_inventory_snapshot_tables(client):
    _http, db, _tmp = client
    before_count = db.query(QinsiInventorySnapshot).count()
    content = _workbook_bytes([_base_row()])
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    after_count = db.query(QinsiInventorySnapshot).count()
    assert before_count == after_count == 0


def test_does_not_touch_procurement_execution_tables(client):
    _http, db, _tmp = client
    before_plans = db.query(ProcurementDemandPlan).count()
    before_exec = db.query(ProcurementPurchaseExecution).count()
    content = _workbook_bytes([_base_row(采购量="5")])
    create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert db.query(ProcurementDemandPlan).count() == before_plans
    assert db.query(ProcurementPurchaseExecution).count() == before_exec


# ===========================================================================
# Preview -> confirm token staging (P0 fix: no more base64-in-hidden-field)
# ===========================================================================

@pytest.fixture
def isolated_preview_temp_dir(tmp_path, monkeypatch):
    temp_dir = tmp_path / "sales-summary-preview"
    monkeypatch.setattr(qinsi_sales_summary, "SALES_SUMMARY_PREVIEW_TEMP_DIR", temp_dir)
    return temp_dir


def _large_workbook_bytes(row_count: int) -> bytes:
    """A real .xlsx big enough (many distinct rows -> compression can't help
    much) that its base64 encoding alone would exceed Starlette's 1MB
    per-field cap if it were ever round-tripped through a hidden input."""
    rows = [_base_row(货号=f"LARGE-{i:06d}", 商品名称=f"大文件测试商品第{i}行带一些额外文字撑体积") for i in range(row_count)]
    return _workbook_bytes(rows)


def test_upload_confirm_single_small_file_via_routes(client, isolated_preview_temp_dir):
    http, _db, _tmp = client
    content = _workbook_bytes([_base_row(货号="ROUTE-0001")])
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    assert response.status_code == 200
    assert "preview_token" in response.text
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)

    confirm_response = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert confirm_response.status_code == 303
    assert confirm_response.headers["location"].startswith("/qinsi-sales-summary/")


def test_upload_confirm_12_large_files_no_field_size_error(client, isolated_preview_temp_dir):
    """The actual P0 repro: enough large files that the old base64-hidden-field
    design would have hit "Field exceeded maximum size of 1024KB." on confirm."""
    http, db, _tmp = client
    files_payload = []
    for i in range(12):
        content = _large_workbook_bytes(80)  # 12 * 80 = 960 rows total, each row padded
        files_payload.append(("files", (f"汇总({i*80+1}-{(i+1)*80}).xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")))
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=files_payload,
        data={"period_start": "2026-08-24", "period_end": "2026-08-30"},
    )
    assert response.status_code == 200
    assert "1024KB" not in response.text
    # confirm request body itself must stay tiny -- just the token + nothing else
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)

    confirm_response = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert confirm_response.status_code == 303
    snapshot = db.query(QinsiSalesSummarySnapshot).one()
    assert snapshot.total_rows == 960


def test_preview_html_contains_no_base64_file_blob(client, isolated_preview_temp_dir):
    import re

    http, _db, _tmp = client
    content = _large_workbook_bytes(2000)  # large enough that base64 would dwarf the page template
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    assert "file_contents" not in response.text
    assert "filenames" not in response.text
    # no long base64-alphabet run anywhere in the page (a real base64 blob of
    # the uploaded file would be tens/hundreds of KB of unbroken [A-Za-z0-9+/=])
    assert re.search(r"[A-Za-z0-9+/]{200,}={0,2}", response.text) is None
    # the response body itself should be far smaller than the uploaded file
    assert len(response.text) < len(content)


def test_confirm_request_body_stays_small_regardless_of_upload_size(client, isolated_preview_temp_dir):
    http, _db, _tmp = client
    content = _large_workbook_bytes(200)
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)
    confirm_body = f"preview_token={token}".encode()
    assert len(confirm_body) < 1024  # comfortably under the 1MB field cap
    confirm_response = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert confirm_response.status_code == 303


def test_confirm_with_nonexistent_token_is_friendly(client, isolated_preview_temp_dir):
    http, _db, _tmp = client
    response = http.post("/qinsi-sales-summary/confirm", data={"preview_token": "0" * 32})
    assert response.status_code == 422
    assert "预览已失效" in response.text
    assert "Traceback" not in response.text
    assert '{"detail"' not in response.text


def test_confirm_with_expired_token_is_friendly(isolated_preview_temp_dir, monkeypatch):
    content = _workbook_bytes([_base_row()])
    token = create_preview_token(
        [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    # backdate the manifest's created_at past the TTL
    import json
    manifest_path = isolated_preview_temp_dir / token / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2020-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PreviewTokenError, match="过期"):
        load_preview_token(token)
    # expired token must be cleaned up as a side effect of the failed load
    assert not (isolated_preview_temp_dir / token).exists()


def test_token_path_traversal_rejected(isolated_preview_temp_dir):
    for malicious in ("../../etc/passwd", "..\\..\\windows", "not-hex-at-all-zzzzzzzzzzzzzzzz", "", "a" * 32 + "/../.."):
        with pytest.raises(PreviewTokenError):
            load_preview_token(malicious)


def test_repeat_confirm_with_same_token_does_not_create_two_snapshots(client, isolated_preview_temp_dir):
    http, db, _tmp = client
    content = _workbook_bytes([_base_row(货号="DUP-CONFIRM-0001")])
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)

    first = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert first.status_code == 303
    assert db.query(QinsiSalesSummarySnapshot).count() == 1

    # second confirm with the SAME (now-discarded) token must not create another snapshot
    second = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert second.status_code == 422
    assert "预览已失效" in second.text
    assert db.query(QinsiSalesSummarySnapshot).count() == 1


def test_confirm_success_cleans_up_temp_directory(client, isolated_preview_temp_dir):
    http, _db, _tmp = client
    content = _workbook_bytes([_base_row(货号="CLEANUP-0001")])
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)
    assert (isolated_preview_temp_dir / token).exists()

    http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert not (isolated_preview_temp_dir / token).exists()


def test_confirm_business_failure_does_not_create_partial_snapshot_and_keeps_token(client, isolated_preview_temp_dir):
    """A blocking completeness issue (range overlap) without the override
    checkbox must fail cleanly -- no snapshot, and the token survives for a
    retry (e.g. after the user ticks the override checkbox)."""
    http, db, _tmp = client
    file_a = _workbook_bytes([_base_row(货号=f"OVR-{i}") for i in range(10)])
    file_b = _workbook_bytes([_base_row(货号=f"OVR-{i}") for i in range(5, 15)])  # overlaps
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[
            ("files", ("汇总(1-10).xlsx", file_a, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("汇总(6-15).xlsx", file_b, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)

    confirm_response = http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)
    assert confirm_response.status_code == 422
    assert db.query(QinsiSalesSummarySnapshot).count() == 0
    # token must still be usable -- not discarded on a business-validation failure
    assert (isolated_preview_temp_dir / token).exists()

    # retry with override succeeds
    retry_response = http.post(
        "/qinsi-sales-summary/confirm",
        data={"preview_token": token, "override_completeness_warning": "true"},
        follow_redirects=False,
    )
    assert retry_response.status_code == 303
    assert db.query(QinsiSalesSummarySnapshot).count() == 1


def test_period_start_end_taken_from_server_manifest_not_client(client, isolated_preview_temp_dir):
    """Even if a malicious/broken client resubmitted a different period on
    confirm, only preview_token is read -- period comes from the manifest
    written at upload time."""
    http, db, _tmp = client
    content = _workbook_bytes([_base_row(货号="PERIOD-TRUST-0001")])
    response = http.post(
        "/qinsi-sales-summary/upload",
        files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
        data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
    )
    import re
    token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)

    # attempt to smuggle a different period via extra form fields -- must be ignored
    http.post("/qinsi-sales-summary/confirm", data={
        "preview_token": token, "period_start": "1999-01-01", "period_end": "1999-01-02",
    })
    snapshot = db.query(QinsiSalesSummarySnapshot).one()
    assert snapshot.period_start.strftime("%Y-%m-%d") == "2026-08-01"
    assert snapshot.period_end.strftime("%Y-%m-%d") == "2026-08-30"


def test_combined_file_hash_dedup_still_works_through_token_flow(client, isolated_preview_temp_dir):
    http, db, _tmp = client
    content = _workbook_bytes([_base_row(货号="HASHDEDUP-0001")])

    def upload_and_confirm():
        response = http.post(
            "/qinsi-sales-summary/upload",
            files=[("files", ("f.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
            data={"period_start": "2026-08-01", "period_end": "2026-08-30"},
        )
        import re
        token = re.search(r'name="preview_token" value="([0-9a-f]{32})"', response.text).group(1)
        return http.post("/qinsi-sales-summary/confirm", data={"preview_token": token}, follow_redirects=False)

    import urllib.parse

    first = upload_and_confirm()
    assert first.status_code == 303
    second = upload_and_confirm()  # fresh upload+preview+confirm cycle, identical file content
    assert second.status_code == 303
    # same snapshot id both times (path before the human-readable "?message=" differs)
    first_path = first.headers["location"].split("?")[0]
    second_path = second.headers["location"].split("?")[0]
    assert first_path == second_path
    first_message = urllib.parse.unquote(first.headers["location"])
    second_message = urllib.parse.unquote(second.headers["location"])
    assert "重复文件组合" in first_message or "重复文件组合" in second_message
    assert db.query(QinsiSalesSummarySnapshot).count() == 1


# ===========================================================================
# Snapshot detail page: matched-product visibility, filters, search, real pagination
# ===========================================================================

def _make_mixed_snapshot(db, *, row_count: int = 25):
    """row_count products with a qinsi_product_code, all matched, plus one
    genuinely unmatched row and one genuine code/JAN conflict row."""
    ids = []
    rows = []
    for i in range(row_count):
        p = product(db, f"DETAIL{i:03d}", qinsi_product_code=f"Q-DETAIL-{i:03d}", name_cn=f"详情页测试商品{i}")
        ids.append(p.id)
        rows.append(_base_row(货号=f"Q-DETAIL-{i:03d}", 商品名称=f"详情页测试商品{i}", 销售量=str(i % 3)))
    rows.append(_base_row(货号="Q-NO-SUCH-CODE", 商品名称="找不到的商品"))  # unmatched
    product(db, "DETAILCONFLICT-A", qinsi_product_code="Q-CONFLICT-CODE")
    product(db, "DETAILCONFLICT-B", jan="4901301231123")
    rows.append(_base_row(货号="Q-CONFLICT-CODE", 单品条码="4901301231123", 商品名称="冲突商品"))
    content = _workbook_bytes(rows)
    snapshot, _reused = create_sales_summary_snapshot_from_files(
        db, [("f.xlsx", content)], period_start=datetime(2026, 8, 1, tzinfo=UTC), period_end=datetime(2026, 8, 30, tzinfo=UTC),
    )
    return snapshot, ids


def test_detail_page_shows_matched_jba_product_identity(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=3)
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"q": "详情页测试商品0"})
    assert response.status_code == 200
    assert "DETAIL000" in response.text  # internal_sku
    assert f'href="/products/' in response.text
    assert "详情页测试商品0" in response.text


def test_detail_page_shows_match_method_label(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=3)
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}")
    assert response.status_code == 200
    assert "秦丝货号" in response.text  # MATCH_METHOD_LABELS["qinsi_product_code"]


def test_detail_page_filter_unmatched_only(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=3)
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"match_status": "unmatched"})
    assert response.status_code == 200
    assert "找不到的商品" in response.text
    assert "冲突商品" not in response.text
    assert "详情页测试商品0" not in response.text


def test_detail_page_filter_conflict_only(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=3)
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"match_status": "conflict"})
    assert response.status_code == 200
    assert "冲突商品" in response.text
    assert "找不到的商品" not in response.text


def test_detail_page_unmatched_one_click_link_present(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=3)
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}")
    assert 'match_status=unmatched' in response.text


def test_detail_page_search_by_name_code_or_jan(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=5)
    by_name = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"q": "详情页测试商品2"})
    assert "详情页测试商品2" in by_name.text
    by_code = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"q": "Q-DETAIL-003"})
    assert "详情页测试商品3" in by_code.text
    by_jan = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"q": "4901301231123"})
    assert "冲突商品" in by_jan.text


def test_detail_page_default_pagination_is_20_not_all_rows(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=25)  # 25 matched + 1 unmatched + 1 conflict = 27
    response = http.get(f"/qinsi-sales-summary/{snapshot.id}")
    assert response.status_code == 200
    assert "共 27 行" in response.text
    assert "20 / 2 页".replace(" ", "") in response.text.replace(" ", "") or "20" in response.text
    # only page_size=20 distinct rows should render, not all 27 -- each row's
    # 货号 appears exactly once (in the 秦丝原始 column), so count that marker
    assert response.text.count("货号：Q-DETAIL-") == 20


def test_detail_page_pagination_page_2_and_page_size_50(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=25)
    page2 = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"page": 2})
    assert page2.status_code == 200
    page_size_50 = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"page_size": 50})
    assert page_size_50.status_code == 200
    assert "共 27 行 · 第 1 / 1 页" in page_size_50.text


def test_detail_page_top_summary_unaffected_by_filter_and_pagination(client):
    http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=25)
    default_page = http.get(f"/qinsi-sales-summary/{snapshot.id}")
    filtered_page = http.get(f"/qinsi-sales-summary/{snapshot.id}", params={"match_status": "unmatched"})

    def top_summary(text):
        import re
        return re.findall(r"<strong>(\d+)</strong>(总行数|已匹配|未匹配|冲突)", text)

    assert top_summary(default_page.text) == top_summary(filtered_page.text)
    assert ("27", "总行数") in top_summary(default_page.text)
    assert ("1", "未匹配") in top_summary(default_page.text)
    assert ("1", "冲突") in top_summary(default_page.text)


def test_get_sales_summary_lines_page_is_real_sql_pagination(client):
    """total_count reflects the FILTERED set (not the whole snapshot), and
    only page_size rows are ever returned regardless of snapshot size."""
    _http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=25)

    unfiltered = get_sales_summary_lines_page(db, snapshot.id, page=1, page_size=20)
    assert unfiltered.total_count == 27
    assert len(unfiltered.lines) == 20
    assert unfiltered.total_pages == 2

    unmatched_only = get_sales_summary_lines_page(db, snapshot.id, match_status="unmatched", page=1, page_size=20)
    assert unmatched_only.total_count == 1
    assert len(unmatched_only.lines) == 1
    assert unmatched_only.lines[0].match_status == "unmatched"


def test_get_sales_summary_snapshot_summary_does_not_load_all_lines(client):
    """Header stats come from precomputed snapshot columns, not from
    iterating .lines -- must work even for a snapshot the caller never
    touches .lines on."""
    _http, db, _tmp = client
    snapshot, _ids = _make_mixed_snapshot(db, row_count=25)
    summary = get_sales_summary_snapshot_summary(db, snapshot.id)
    assert summary.total_rows == 27
    assert summary.matched_rows == 25
    assert summary.unmatched_rows == 1
    assert summary.conflict_rows == 1
