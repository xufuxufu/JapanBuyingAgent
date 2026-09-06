from __future__ import annotations


def test_four_primary_home_entries_are_unchanged(client):
    http, _db, _ = client
    response = http.get("/")
    assert response.status_code == 200
    for label in ("扫码查价", "微信订单", "补货需求", "采购"):
        assert label in response.text


def test_upload_flow_is_labeled_recognize_receipt_everywhere(client):
    http, _db, _ = client
    more_page = http.get("/more")
    upload_page = http.get("/receipts/upload")

    assert "识别小票" in more_page.text
    assert "拍照识别" not in more_page.text
    assert upload_page.status_code == 200
    assert "识别小票" in upload_page.text
    assert "拍照识别" not in upload_page.text


def test_batch_scan_entry_hidden_from_more_and_sidebar_but_route_still_works(client):
    http, _db, _ = client
    more_page = http.get("/more")
    products_page = http.get("/products")  # any page renders the persistent desktop sidebar

    assert "扫码（批量）" not in more_page.text
    assert 'href="/field-purchase"' not in more_page.text
    assert 'href="/field-purchase"' not in products_page.text

    field_purchase_page = http.get("/field-purchase")
    assert field_purchase_page.status_code == 200


def test_more_page_groups_admin_pages_under_backend_management(client):
    http, _db, _ = client
    response = http.get("/more")
    assert response.status_code == 200
    text = response.text

    assert "常用功能" in text
    assert "后台管理" in text
    backend_start = text.index("后台管理")
    backend_section = text[backend_start:]

    assert "商品管理" in backend_section
    assert "门店管理" in backend_section
    assert "秦丝导入/同步/维护" in backend_section
    assert "系统与任务状态" in backend_section
    for expected_href in (
        "/locations", "/stores", "/products/qinsi-master-import", "/jan-governance",
        "/product-image-localization", "/gpt-jobs", "/monitor-status", "/platform-config",
    ):
        assert f'href="{expected_href}"' in backend_section, f"{expected_href} missing from 后台管理"


def test_frequently_used_section_keeps_daily_operational_pages(client):
    http, _db, _ = client
    response = http.get("/more")
    text = response.text
    frequent_section = text[text.index("常用功能"):text.index("后台管理")]

    for expected_href in (
        "/price-check", "/receipts/upload", "/purchase-batches", "/products",
        "/watched-products", "/restock-lists", "/receipts", "/notifications", "/purchase-analytics",
    ):
        assert f'href="{expected_href}"' in frequent_section, f"{expected_href} missing from 常用功能"


def test_domestic_logistics_removed_from_primary_and_bottom_nav(client):
    # 国内物流 moved out of the primary nav (home quick entries / mobile
    # bottom nav) into /more and a link on the 微信订单 list page -- the
    # route itself is unchanged and still fully functional (see below).
    http, _db, _ = client
    home = http.get("/")
    assert home.status_code == 200
    bottom_nav_start = home.text.index('class="mobile-bottom-nav"')
    bottom_nav = home.text[bottom_nav_start:bottom_nav_start + 800]
    assert "国内物流" not in bottom_nav
    sidebar_quick_start = home.text.index('class="sidebar-nav"')
    sidebar_quick_end = home.text.index("</nav>", sidebar_quick_start)
    sidebar_quick = home.text[sidebar_quick_start:sidebar_quick_end]
    assert "国内物流" not in sidebar_quick


def test_domestic_logistics_still_reachable_from_more_and_sales_orders_page(client):
    http, _db, _ = client
    more_page = http.get("/more")
    assert 'href="/domestic-logistics"' in more_page.text

    sales_orders_page = http.get("/sales-orders")
    assert sales_orders_page.status_code == 200
    assert 'href="/domestic-logistics"' in sales_orders_page.text

    # Route itself is untouched.
    route = http.get("/domestic-logistics")
    assert route.status_code == 200


def test_mobile_bottom_nav_is_a_single_row_of_four_plus_more(client):
    http, _db, _ = client
    home = http.get("/")
    bottom_nav_start = home.text.index('class="mobile-bottom-nav"')
    nav_end = home.text.index("</nav>", bottom_nav_start)
    bottom_nav = home.text[bottom_nav_start:nav_end]
    # Exactly the 4 HOME_QUICK_ENTRIES plus the always-present "更多" link --
    # matches the CSS's repeat(5,1fr) grid, keeping it a single row.
    assert bottom_nav.count("<a ") == 5
    for label in ("扫码查价", "微信订单", "补货需求", "采购", "更多"):
        assert label in bottom_nav
