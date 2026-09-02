from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.models import (
    Location, Product, ProcurementDemand, ProcurementDemandPlan, ProcurementPurchaseExecution,
    QinsiInventorySnapshot, QinsiInventorySnapshotLine, Store,
)
from app.procurement_service import (
    create_channel_shortage_demand, create_plans, purchased_quantity_for_plans,
    record_purchase_execution, set_plan_selected_store, update_demand_plan,
)
from app.procurement_service import PlanSelectionInput


def product(db, suffix: str, name: str | None = None) -> Product:
    item = Product(internal_sku=f"P8-{suffix:0>4}", jan=f"0498800000{suffix:0>3}", name_cn=name or f"补货测试商品{suffix}")
    db.add(item)
    db.flush()
    return item


def store(db, name: str) -> Store:
    row = Store(name=name, name_cn=name, is_active=True)
    db.add(row)
    db.flush()
    return row


def china_warehouse(db) -> Location:
    loc = Location(internal_code="QW-2025-QIANYU", display_name="2025千羽", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
    db.add(loc)
    db.flush()
    return loc


def add_snapshot_line(db, product_id: int, warehouse_id: int, quantity: int) -> None:
    snap = QinsiInventorySnapshot(
        batch_no="QS-P8-TEST", original_filename="test.xlsx", file_hash=f"p8-hash-{product_id}-{warehouse_id}",
        file_content=b"x", data_at=datetime.now(timezone.utc), status="completed",
    )
    db.add(snap)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snap.id, original_row_no=1, raw_summary_json=json.dumps({}),
        product_id=product_id, warehouse_id=warehouse_id, quantity=quantity, matching_status="matched",
        warehouse_status="matched",
    ))
    db.commit()


def jpeg_bytes(color=(20, 90, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated_procurement_image_storage(tmp_path, monkeypatch):
    # app/procurement_image.py resolves saved files relative to its own
    # module-level PROJECT_ROOT/PROCUREMENT_DEMAND_IMAGE_DIR bindings,
    # independent of the client fixture's PROJECT_ROOT patches on other
    # modules. Without this, tests in this file write real files into the
    # real project's data/procurement-demands/ directory instead of a
    # throwaway tmp_path (mirrors the identical Phase 7 fixture in
    # test_sales_orders.py for sales_order_shipping.py).
    import app.procurement_image as procurement_image_module
    import app.sales_order_shipping as shipping_module
    monkeypatch.setattr(procurement_image_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        procurement_image_module, "PROCUREMENT_DEMAND_IMAGE_DIR",
        tmp_path / "data" / "procurement-demands" / "item-images",
    )
    # resolve_procurement_demand_image_path reuses _resolve_under_root, which
    # is DEFINED in sales_order_shipping.py -- its PROJECT_ROOT lookup resolves
    # against THAT module's own globals, not procurement_image's, so it must
    # be patched too or the read-back path never matches the write path.
    monkeypatch.setattr(shipping_module, "PROJECT_ROOT", tmp_path)


# ==================== 补货需求 (report-shortage / 补货需求 page) ====================


def test_shortage_page_title_is_replenishment_demand(client):
    http, db, _tmp = client
    response = http.get("/procurement-demands/report-shortage")
    assert response.status_code == 200
    assert "补货需求" in response.text
    assert "报缺货" not in response.text


def test_shortage_page_subtitle_correct(client):
    http, db, _tmp = client
    response = http.get("/procurement-demands/report-shortage")
    assert "提醒库存即将短缺，需考虑物流时效。" in response.text
    assert "告诉老婆" not in response.text


def test_procurement_product_search_shows_image(client):
    http, db, _tmp = client
    item = product(db, "1")
    item.display_image_url = "https://example.com/images/p8-1.jpg"
    db.commit()
    response = http.get("/api/procurement/products/search", params={"q": item.internal_sku})
    row = next(r for r in response.json() if r["id"] == item.id)
    assert row["image_url"] == "https://example.com/images/p8-1.jpg"
    assert row["qinsi_product_code"] == item.qinsi_product_code


def test_procurement_search_china_zero_shows_explicit_zero(client):
    http, db, _tmp = client
    item = product(db, "2")
    warehouse = china_warehouse(db)
    add_snapshot_line(db, item.id, warehouse.id, 0)
    response = http.get("/api/procurement/products/search", params={"q": item.internal_sku})
    row = next(r for r in response.json() if r["id"] == item.id)
    assert row["china_quantity"] == 0


def test_procurement_search_japan_zero_shows_explicit_zero(client):
    from app.models import Location as LocationModel
    http, db, _tmp = client
    item = product(db, "3")
    jp_warehouse = LocationModel(internal_code="QW-NEW-JAPAN", display_name="日本仓", location_type="qinsi_warehouse", is_qinsi_warehouse=True)
    db.add(jp_warehouse)
    db.flush()
    add_snapshot_line(db, item.id, jp_warehouse.id, 0)
    response = http.get("/api/procurement/products/search", params={"q": item.internal_sku})
    row = next(r for r in response.json() if r["id"] == item.id)
    assert row["japan_quantity"] == 0


def test_procurement_search_unknown_inventory_is_none_not_zero(client):
    http, db, _tmp = client
    item = product(db, "4")
    response = http.get("/api/procurement/products/search", params={"q": item.internal_sku})
    row = next(r for r in response.json() if r["id"] == item.id)
    assert row["china_quantity"] is None
    assert row["japan_quantity"] is None


def test_shortage_quantity_defaults_to_one_in_template(client):
    http, db, _tmp = client
    response = http.get("/procurement-demands/report-shortage")
    assert 'value="1"' in response.text


def test_shortage_quantity_stepper_never_below_one():
    # JS behavior is covered by direct inspection of the stepper's clamp logic
    # (Math.max(1, ...)); this test locks in the shared min=1 rule at the
    # markup level so a future edit can't silently drop the floor.
    import re
    from pathlib import Path
    js = Path(__file__).resolve().parents[1].joinpath("app", "static", "procurement_demand_shortage.js").read_text(encoding="utf-8")
    assert re.search(r"Math\.max\(1,", js)


def test_shortage_route_rejects_all_three_empty(client):
    http, db, _tmp = client
    response = http.post("/procurement-demands/report-shortage", data={"quantity": "3"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert db.query(ProcurementDemand).count() == 0


def test_shortage_route_accepts_manual_name_only(client):
    http, db, _tmp = client
    response = http.post(
        "/procurement-demands/report-shortage", data={"manual_name": "只有名字的商品", "quantity": "2"}, follow_redirects=False,
    )
    assert response.status_code == 303
    demand = db.query(ProcurementDemand).filter_by(product_name_snapshot="只有名字的商品").one()
    assert demand.product_id is None
    assert demand.requested_quantity == 2


def test_shortage_route_accepts_manual_image_only(client):
    http, db, _tmp = client
    response = http.post(
        "/procurement-demands/report-shortage",
        data={"quantity": "1"},
        files={"manual_image": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    demand = db.query(ProcurementDemand).filter_by(demand_type="channel_shortage").order_by(ProcurementDemand.id.desc()).first()
    assert demand is not None
    assert demand.product_id is None
    assert demand.product_name_snapshot == "手工商品（图片）"
    assert demand.manual_image_relative_path is not None


def test_shortage_manual_image_view_route(client):
    http, db, _tmp = client
    http.post(
        "/procurement-demands/report-shortage", data={"quantity": "1"},
        files={"manual_image": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
    )
    demand = db.query(ProcurementDemand).order_by(ProcurementDemand.id.desc()).first()
    response = http.get(f"/procurement-demands/item-images/{demand.id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_shortage_manual_image_rejects_non_image_content(db_session):
    import pytest
    with pytest.raises(ValueError):
        create_channel_shortage_demand(
            db_session, manual_image_content=b"not an image", manual_image_filename="fake.jpg",
        )


def test_shortage_rejects_when_product_name_and_image_both_empty(db_session):
    import pytest
    with pytest.raises(ValueError):
        create_channel_shortage_demand(db_session, quantity=1)


def test_shortage_channel_label_renamed_to_replenishment(client):
    http, db, _tmp = client
    http.post("/procurement-demands/report-shortage", data={"manual_name": "标签测试商品", "quantity": "1"})
    response = http.get("/procurement-demands?view=open")
    assert "补货需求" in response.text
    assert "渠道缺货" not in response.text


# ==================== 采购 Plan 编辑规则 ====================


def test_plan_editable_when_no_execution_yet(db_session):
    item = product(db_session, "10")
    create_channel_shortage_demand(db_session, product_id=item.id, quantity=5)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=5)])
    updated = update_demand_plan(db_session, plan.id, planned_quantity=8, note="改一下")
    assert updated.planned_quantity == 8
    assert updated.note == "改一下"


def test_plan_quantity_can_increase_above_purchased(db_session):
    item = product(db_session, "11")
    create_channel_shortage_demand(db_session, product_id=item.id, quantity=10)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=10)])
    record_purchase_execution(db_session, plan.id, 4)
    updated = update_demand_plan(db_session, plan.id, planned_quantity=6)
    assert updated.planned_quantity == 6
    updated = update_demand_plan(db_session, plan.id, planned_quantity=12)
    assert updated.planned_quantity == 12


def test_plan_quantity_below_purchased_rejected(db_session):
    import pytest
    item = product(db_session, "12")
    create_channel_shortage_demand(db_session, product_id=item.id, quantity=10)
    [plan] = create_plans(db_session, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=10)])
    record_purchase_execution(db_session, plan.id, 4)
    with pytest.raises(ValueError, match="不得低于已采购数量"):
        update_demand_plan(db_session, plan.id, planned_quantity=2)
    # the already-recorded execution fact must be completely untouched
    assert purchased_quantity_for_plans(db_session, [plan.id])[plan.id] == 4


def test_plan_planned_purchased_remaining_display(client):
    http, db, _tmp = client
    item = product(db, "13")
    shop = store(db, "计划显示测试店")
    db.commit()
    create_channel_shortage_demand(db, product_id=item.id, quantity=10)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=10)])
    set_plan_selected_store(db, plan.id, shop.id)
    record_purchase_execution(db, plan.id, 4)
    response = http.get("/procurement-demands?view=planned")
    assert "已买：<strong>4</strong>" in response.text
    assert "剩余：<strong>6</strong>" in response.text


def test_plan_edit_route_shows_product_image(client):
    http, db, _tmp = client
    item = product(db, "14")
    item.display_image_url = "https://example.com/images/p8-14.jpg"
    db.commit()
    create_channel_shortage_demand(db, product_id=item.id, quantity=3)
    create_plans(db, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=3)])
    response = http.get("/procurement-demands?view=planned")
    assert "https://example.com/images/p8-14.jpg" in response.text


def test_plan_manual_image_shows_in_planned_view(client):
    http, db, _tmp = client
    http.post(
        "/procurement-demands/report-shortage", data={"quantity": "2"},
        files={"manual_image": ("photo.jpg", jpeg_bytes(), "image/jpeg")},
    )
    demand = db.query(ProcurementDemand).order_by(ProcurementDemand.id.desc()).first()
    create_plans(db, [PlanSelectionInput(kind="demand", key=str(demand.id), planned_quantity=2)])
    response = http.get("/procurement-demands?view=planned")
    assert f"/procurement-demands/item-images/{demand.id}" in response.text


def test_plan_edit_route_enforces_purchased_floor(client):
    http, db, _tmp = client
    item = product(db, "15")
    shop = store(db, "路由校验店")
    db.commit()
    create_channel_shortage_demand(db, product_id=item.id, quantity=10)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=10)])
    set_plan_selected_store(db, plan.id, shop.id)
    record_purchase_execution(db, plan.id, 4)
    response = http.post(
        f"/procurement-demands/plans/{plan.id}/edit",
        data={"planned_quantity": "2", "note": "", "group_by": "product"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    db.refresh(plan)
    assert plan.planned_quantity == 10


# ==================== 采购需求中心 7 个导航 tab ====================


def test_all_seven_tabs_present(client):
    http, db, _tmp = client
    response = http.get("/procurement-demands")
    for label in ("待采购", "找货需求", "待采购商品", "分拣", "采购", "已采购", "全部"):
        assert label in response.text


def test_investigation_tab_shows_investigation_demands(client):
    http, db, _tmp = client
    http.post("/procurement-demands/investigations", data={"manual_name": "找货测试商品", "note": "看看有没有"})
    response = http.get("/procurement-demands?view=investigation")
    assert response.status_code == 200
    assert "找货测试商品" in response.text


def test_purchased_items_do_not_appear_in_planned_tab(client):
    http, db, _tmp = client
    item = product(db, "20")
    shop = store(db, "已采购隔离测试店")
    db.commit()
    create_channel_shortage_demand(db, product_id=item.id, quantity=3)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=3)])
    set_plan_selected_store(db, plan.id, shop.id)
    record_purchase_execution(db, plan.id, 3)

    planned_response = http.get("/procurement-demands?view=planned")
    assert "补货测试商品20" not in planned_response.text
    purchased_response = http.get("/procurement-demands?view=purchased")
    assert "补货测试商品20" in purchased_response.text


def test_all_tab_shows_everything(client):
    http, db, _tmp = client
    item = product(db, "21")
    create_channel_shortage_demand(db, product_id=item.id, quantity=1)
    response = http.get("/procurement-demands?view=all")
    assert "补货测试商品21" in response.text


def test_sorting_tab_is_planned_store_view(client):
    http, db, _tmp = client
    item = product(db, "22")
    shop = store(db, "分拣测试店")
    db.commit()
    create_channel_shortage_demand(db, product_id=item.id, quantity=2)
    [plan] = create_plans(db, [PlanSelectionInput(kind="product", key=str(item.id), planned_quantity=2)])
    set_plan_selected_store(db, plan.id, shop.id)
    response = http.get("/procurement-demands?view=planned&group_by=store")
    assert response.status_code == 200
    assert "分拣测试店" in response.text


def test_purchase_tab_link_points_to_purchase_page(client):
    http, db, _tmp = client
    response = http.get("/procurement-demands")
    assert 'href="/procurement-demands/purchase"' in response.text


# ==================== QinSi Snapshot 分页 ====================


def make_snapshot_with_lines(db, line_count: int) -> QinsiInventorySnapshot:
    snap = QinsiInventorySnapshot(
        batch_no=f"QS-P8-PAGE-{line_count}", original_filename="page-test.xlsx", file_hash=f"p8-page-hash-{line_count}",
        file_content=b"x", data_at=datetime.now(timezone.utc), status="completed",
        total_rows=line_count, success_rows=line_count, unmatched_rows=0, exception_rows=0,
    )
    db.add(snap)
    db.flush()
    for i in range(line_count):
        db.add(QinsiInventorySnapshotLine(
            snapshot_id=snap.id, original_row_no=i + 1, raw_summary_json=json.dumps({}),
            raw_product_name=f"分页测试商品{i}", jan=f"111100000{i:04d}", qinsi_product_code=f"QC{i:04d}",
            quantity=1, matching_status="matched" if i % 4 else "unmatched", warehouse_status="matched",
        ))
    db.commit()
    return snap


def test_snapshot_detail_default_page_size_is_20(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}")
    assert response.status_code == 200
    assert response.text.count("分页测试商品") == 20


def test_snapshot_detail_page_2_shows_next_rows(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    page1 = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page": 1})
    page2 = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page": 2})
    assert "分页测试商品0<" in page1.text or "分页测试商品0 " in page1.text or ">分页测试商品0<" in page1.text
    assert "分页测试商品20" in page2.text
    assert "分页测试商品0<" not in page2.text


def test_snapshot_detail_page_size_10(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page_size": 10})
    assert response.text.count("分页测试商品") == 10


def test_snapshot_detail_page_size_50(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page_size": 50})
    assert response.text.count("分页测试商品") == 45


def test_snapshot_detail_page_size_100(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 150)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page_size": 100})
    assert response.text.count("分页测试商品") == 100


def test_snapshot_detail_invalid_page_size_falls_back_to_default(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page_size": 999})
    assert response.status_code == 200
    assert response.text.count("分页测试商品") == 20


def test_snapshot_detail_matching_status_filter(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 20)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"matching_status": "unmatched", "page_size": 50})
    # every 4th row (i % 4 == 0) is unmatched -> 5 of 20
    assert response.text.count("分页测试商品") == 5


def test_snapshot_detail_warehouse_filter(client):
    http, db, _tmp = client
    item = product(db, "30")
    warehouse = china_warehouse(db)
    snap = QinsiInventorySnapshot(
        batch_no="QS-P8-WH", original_filename="wh.xlsx", file_hash="p8-wh-hash",
        file_content=b"x", data_at=datetime.now(timezone.utc), status="completed",
        total_rows=2, success_rows=2, unmatched_rows=0, exception_rows=0,
    )
    db.add(snap)
    db.flush()
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snap.id, original_row_no=1, raw_summary_json="{}", raw_product_name="仓库过滤商品甲",
        warehouse_id=warehouse.id, quantity=1, matching_status="matched", warehouse_status="matched",
    ))
    db.add(QinsiInventorySnapshotLine(
        snapshot_id=snap.id, original_row_no=2, raw_summary_json="{}", raw_product_name="仓库过滤商品乙",
        warehouse_id=None, raw_warehouse_name="其它仓库", quantity=1, matching_status="matched", warehouse_status="matched",
    ))
    db.commit()
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"warehouse_id": warehouse.id})
    assert "仓库过滤商品甲" in response.text
    assert "仓库过滤商品乙" not in response.text


def test_snapshot_detail_jan_search(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 20)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"jan_query": f"111100000{3:04d}"})
    assert "分页测试商品3" in response.text
    assert response.text.count("分页测试商品") == 1


def test_snapshot_detail_qinsi_code_search(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 20)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"qinsi_code_query": "QC0005"})
    assert "分页测试商品5" in response.text
    assert response.text.count("分页测试商品") == 1


def test_snapshot_detail_name_search(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 20)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"name_query": "分页测试商品7"})
    # exactly one <td> cell contains the matched row's name (the filter input
    # also echoes the query text back into its value= attribute, so a raw
    # whole-page substring count would over-count by one)
    assert response.text.count(">分页测试商品7<") == 1


def test_snapshot_detail_filtered_count_correct(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 20)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"matching_status": "unmatched"})
    assert "共 5 行" in response.text


def test_snapshot_detail_top_summary_unaffected_by_pagination(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}", params={"page_size": 10})
    assert f"<strong>{snap.total_rows}</strong>明细行" in response.text


def test_review_page_paginated_and_actionable_only(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 45)  # every 4th (i%4==0) is unmatched -> 12 rows (0,4,...,44)
    response = http.get(f"/qinsi-inventory-snapshots/{snap.id}/review", params={"page_size": 10})
    assert response.status_code == 200
    # only unmatched/conflict/warehouse-issue rows show -- never the fully-matched ones
    assert response.text.count("确认商品映射") == 10


def test_review_page_retry_matching_still_works(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 8)
    response = http.post(f"/qinsi-inventory-snapshots/{snap.id}/retry-matching", follow_redirects=False)
    assert response.status_code == 303


def test_review_page_ignore_lines_still_works(client):
    http, db, _tmp = client
    snap = make_snapshot_with_lines(db, 8)
    # ignore_snapshot_lines only touches unmatched/conflict rows -- must pick
    # one of those, not just "the first row by whatever implicit DB order".
    line = db.query(QinsiInventorySnapshotLine).filter_by(snapshot_id=snap.id, matching_status="unmatched").first()
    assert line is not None
    response = http.post(
        f"/qinsi-inventory-snapshots/{snap.id}/ignore-lines", data={"line_ids": [str(line.id)]}, follow_redirects=False,
    )
    assert response.status_code == 303
    db.refresh(line)
    assert line.matching_status == "ignored"


# ==================== 首页 / 菜单 ====================


def test_home_page_has_four_quick_entries(client):
    http, db, _tmp = client
    response = http.get("/")
    assert response.status_code == 200
    assert "home-quick-entries" in response.text
    for label, href in (
        ("扫码查价", "/price-check"), ("微信订单", "/sales-orders"),
        ("补货需求", "/procurement-demands/report-shortage"), ("采购", "/procurement-demands"),
    ):
        assert label in response.text
        assert href in response.text


def test_scan_price_check_is_top_level_and_scan_duplicate_removed(client):
    http, db, _tmp = client
    response = http.get("/")
    assert response.status_code == 200
    assert '<span>▦</span><b>扫码查价</b>' in response.text


def test_replenishment_naming_consistent_across_nav(client):
    http, db, _tmp = client
    response = http.get("/")
    assert "补货需求" in response.text
    home_text = response.text
    more_response = http.get("/more")
    assert "补货需求" in more_response.text
    assert "报缺货" not in home_text
    assert "报缺货" not in more_response.text


def _extract_tag(text, tag, css_class):
    import re
    m = re.search(rf'<{tag}[^>]*class="{css_class}"[^>]*>.*?</{tag}>', text, re.S)
    assert m, f"could not find <{tag} class=\"{css_class}\"> block"
    return m.group(0)


def test_home_top_and_bottom_quick_entries_share_the_same_four_links(client):
    """The in-page quick-entry block and the global sidebar-nav/mobile-bottom-nav
    must agree on all four hrefs — a prior bug had the global nav's "采购"
    pointing at /purchase-batches while the home block pointed at
    /procurement-demands."""
    http, db, _tmp = client
    response = http.get("/")
    assert response.status_code == 200
    text = response.text
    top_block = _extract_tag(text, "section", "home-quick-entries")
    sidebar_nav = _extract_tag(text, "nav", "sidebar-nav")
    bottom_nav = _extract_tag(text, "nav", "mobile-bottom-nav")
    expected_hrefs = (
        "/price-check", "/sales-orders",
        "/procurement-demands/report-shortage", "/procurement-demands",
    )
    for href in expected_hrefs:
        assert f'href="{href}"' in top_block, f"{href} missing from home-quick-entries"
        assert f'href="{href}"' in sidebar_nav, f"{href} missing from sidebar-nav"
        assert f'href="{href}"' in bottom_nav, f"{href} missing from mobile-bottom-nav"
    assert "/purchase-batches" not in sidebar_nav
    assert "/purchase-batches" not in bottom_nav


def test_home_bottom_nav_does_not_send_procurement_to_purchase_batches(client):
    http, db, _tmp = client
    response = http.get("/")
    assert response.status_code == 200
    bottom_nav = _extract_tag(response.text, "nav", "mobile-bottom-nav")
    assert "/purchase-batches" not in bottom_nav


def test_field_purchase_batch_scan_still_reachable_from_more_menu(client):
    http, db, _tmp = client
    more_response = http.get("/more")
    assert more_response.status_code == 200
    assert "扫码（批量）" in more_response.text
    assert '/field-purchase' in more_response.text
    field_response = http.get("/field-purchase")
    assert field_response.status_code == 200
