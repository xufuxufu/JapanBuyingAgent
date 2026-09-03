from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


# ==================== 21-23. entry points exist in the three pages ====================

def test_sales_order_new_page_has_image_search_entry():
    template = _read("app/templates/sales_order_new.html")
    assert "item-image-search-mount" in template
    assert "/static/image_product_search.js" in template


def test_procurement_demand_shortage_page_has_image_search_entry():
    template = _read("app/templates/procurement_demand_shortage_new.html")
    assert "shortageImageSearchMount" in template
    assert "/static/image_product_search.js" in template


def test_products_page_has_image_search_entry_and_rebuild_button():
    template = _read("app/templates/products.html")
    assert "productsImageSearchMount" in template
    assert "rebuildImageSearchIndexBtn" in template
    assert "/static/image_product_search.js" in template


# ==================== 24-25. click image zooms only, click card selects ====================

def test_widget_result_thumbnail_uses_shared_zoomable_mechanism():
    js = _read("app/static/image_product_search.js")
    # thumbnail must be a plain "zoomable" image (global image_zoom.js's capture-phase
    # listener intercepts the click and stops propagation before the card's own
    # "select" handler runs) -- the widget itself must NOT attach a click handler
    # to the image, only to the card as a whole.
    assert 'img.className = "product-thumb zoomable"' in js
    assert "card.addEventListener(\"click\"" in js
    assert 'img.addEventListener("click"' not in js


def test_widget_card_click_invokes_on_select_callback():
    js = _read("app/static/image_product_search.js")
    assert "onSelect({" in js
    assert "id: item.product_id" in js


# ==================== 26-27. query preview + loading state ====================

def test_widget_shows_query_preview_and_loading_state():
    js = _read("app/static/image_product_search.js")
    assert "preview.src = objectUrl" in js
    assert "搜索中" in js


def test_widget_two_capture_entry_points():
    js = _read("app/static/image_product_search.js")
    assert 'capture="environment"' in js
    assert "拍照搜索" in js
    assert "从相册选择" in js


# ==================== 28. empty / error states ====================

def test_widget_handles_no_index_and_error_and_empty_results():
    js = _read("app/static/image_product_search.js")
    assert '"no_index"' in js
    assert '"error"' in js
    assert "没有找到相似商品" in js


def test_widget_never_auto_selects_top1():
    js = _read("app/static/image_product_search.js")
    # every result becomes a clickable card the user must act on; there is no
    # code path that calls onSelect without a click.
    assert js.count("onSelect(") == 1
    assert "results[0]" not in js


# ==================== no_similar_results (similarity-threshold filtering) ====================

def test_widget_handles_no_similar_results_status():
    js = _read("app/static/image_product_search.js")
    assert '"no_similar_results"' in js
    assert "没有找到足够相似的商品" in js


def test_widget_has_no_dead_low_similarity_hint():
    """The 0.5 client-side 'low similarity' hint became unreachable once the
    backend enforces a 0.82 floor on everything it returns -- must not
    linger as dead/misleading code."""
    js = _read("app/static/image_product_search.js")
    assert "similarity_score < 0.5" not in js
    assert "结果相似度较低" not in js
