from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

import app.product_translation_service as translation_service
import app.product_admin as product_admin
from app.db import get_db
from app.main import app
from app.models import Product, ProductOperationLog, QinsiExportJob, Receipt, ReceiptBatch, ReceiptItem
from app.product_identity import normalize_product_name_whitespace


def test_duplicate_jans_are_rejected_and_qinsi_product_code_stays_unique(db_session):
    db_session.add_all([
        Product(jan=None, name_cn="A"),
        Product(jan=None, name_cn="B"),
        Product(jan="0490000000001", qinsi_product_code="QINSI-A", name_cn="C"),
    ])
    db_session.commit()
    assert len(list(db_session.scalars(select(Product).where(Product.jan.is_(None))))) == 2
    db_session.add(Product(jan="0490000000001", qinsi_product_code="QINSI-B", name_cn="D"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    db_session.add(Product(jan="0490000000002", qinsi_product_code="QINSI-A", name_cn="E"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_api_creates_products_with_and_without_jan_and_generates_unique_skus(client):
    http, db, _ = client
    with_jan = http.post("/api/products", json={"name_cn": "有JAN商品", "jan": "00123457"})
    without_jan = http.post("/api/products", json={"name_cn": "无JAN商品", "jan": None})
    assert with_jan.status_code == without_jan.status_code == 201
    first, second = with_jan.json(), without_jan.json()
    assert first["jan"] == "00123457" and second["jan"] is None
    assert re.fullmatch(r"NJ-\d{8}-\d{6}", first["internal_sku"])
    assert first["internal_sku"] != second["internal_sku"]
    assert len(list(db.scalars(select(Product)))) == 2


def test_later_jan_entry_keeps_internal_sku_and_duplicate_jan_is_rejected(client):
    http, _, _ = client
    first = http.post("/api/products", json={"name_cn": "先无JAN"}).json()
    second = http.post("/api/products", json={"name_cn": "已有JAN", "jan": "12345670"}).json()
    updated = http.patch(f"/api/products/{first['id']}", json={"jan": "00012345600012"})
    assert updated.status_code == 200
    assert updated.json()["internal_sku"] == first["internal_sku"]
    assert updated.json()["jan"] == "00012345600012"
    duplicate = http.patch(f"/api/products/{first['id']}", json={"jan": second["jan"]})
    assert duplicate.status_code == 409
    assert "已被商品" in duplicate.text


def test_qinsi_product_code_never_populates_jan(client):
    http, _, _ = client
    response = http.post("/api/products", json={"name_cn": "仅秦丝编码", "qinsi_product_code": "4901872097296"})
    assert response.status_code == 201
    assert response.json()["qinsi_product_code"] == "4901872097296"
    assert response.json()["jan"] is None


def test_product_name_whitespace_normalization_variants():
    assert normalize_product_name_whitespace("A  B   C") == "A B C"
    assert normalize_product_name_whitespace("クロレッツXPボトルRオリジナル　　140G") == "クロレッツXPボトルRオリジナル 140G"
    assert normalize_product_name_whitespace("  A\t　B\n C  ") == "A B C"


def test_products_page_has_mobile_cards_sticky_desktop_action_and_local_font_mode(client):
    http, db, _ = client
    product = Product(
        jan="00123457",
        name_cn="手机卡片商品",
        purchase_price=880,
        internal_sku="NJ-TEST-CARD-LONG-LONG-LONG",
        qinsi_product_code="QINSI-CODE-LONG-LONG-LONG",
        main_image_source_url="https://img.example.test/product.jpg",
    )
    db.add(product)
    db.add(QinsiExportJob(status="exported", export_filename="recent-qinsi-products.xlsx"))
    db.commit()
    response = http.get("/products")
    assert response.status_code == 200
    assert "mobile-product-card" in response.text and "sticky-action" in response.text
    assert "秦丝库存快照" in response.text and "内部SKU" in response.text
    assert "jba-font-mode" in response.text and "标准" in response.text and "大字" in response.text
    assert "product-image-slot" in response.text
    assert "sku-cell" in response.text and "qinsi-code-cell" in response.text
    assert "/price-check?jan=00123457&refresh=1" in response.text
    assert response.text.index("手机卡片商品") < response.text.index("缺中文名商品")
    assert response.text.index("手机卡片商品") < response.text.index("未登录秦丝的新商品")
    assert response.text.index("手机卡片商品") < response.text.index("最近新商品导出")


def test_products_new_qinsi_block_outputs_remote_image_and_fallback(client):
    http, db, _ = client
    product = Product(
        jan="4547894155004",
        name_ja="画像商品",
        status="new_pending_review",
        main_image_source_url="https://img.example.test/product.jpg",
    )
    db.add(product)
    db.commit()
    response = http.get("/products")
    assert response.status_code == 200
    assert '<img class="product-thumb" src="https://img.example.test/product.jpg"' in response.text
    assert "product-image-slot" in response.text
    assert "打开图片" in response.text
    assert "onerror=" in response.text
    assert "暂无图片" in response.text


def test_products_css_keeps_image_placeholder_hidden_and_long_columns_wrapped():
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")
    assert ".product-image-slot .product-thumb-link+.field-image-empty" in css
    assert ".product-image-slot .product-thumb-link+.product-image-empty{display:none}" in css
    assert "[hidden]{display:none!important}" in css
    assert ".desktop-products table{table-layout:fixed;min-width:1320px}" in css
    assert ".sku-cell" in css and ".qinsi-code-cell" in css and "overflow-wrap:anywhere" in css


def test_product_detail_uses_one_main_image_area_without_duplicate_placeholder(client):
    http, db, _ = client
    product = Product(
        jan="4547894155004",
        name_ja="画像商品",
        status="new_pending_review",
        main_image_source_url="https://img.example.test/product.jpg",
    )
    db.add(product)
    db.commit()
    response = http.get(f"/products/{product.id}")
    assert response.status_code == 200
    assert response.text.count("product-main-image") == 1
    assert "https://img.example.test/product.jpg" in response.text
    assert "暂无图片" not in response.text


def test_product_detail_with_deepseek_key_does_not_show_unconfigured(client, monkeypatch):
    http, db, _ = client
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-only-key")
    product = Product(jan="00123457", name_ja="日本語商品", status="new_pending_completion")
    db.add(product)
    db.commit()
    response = http.get(f"/products/{product.id}")
    assert response.status_code == 200
    assert "重新生成中文名" in response.text
    assert "DeepSeek：" not in response.text


def test_generate_chinese_name_route_saves_result(client, monkeypatch):
    http, db, _ = client
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-only-key")

    def fake_translate(name_ja, *, client=None, config=None, timeout_seconds=10):
        from app.deepseek_service import DeepSeekTranslation

        return DeepSeekTranslation(name_cn="路由中文", name_ja=name_ja, raw_content=f"路由中文|{name_ja}")

    monkeypatch.setattr(translation_service, "translate_name_with_deepseek", fake_translate)
    product = Product(name_ja="ルート商品", status="new_pending_completion")
    db.add(product)
    db.commit()
    response = http.post(
        f"/products/{product.id}/generate-chinese-name",
        data={"return_to": f"/products/{product.id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.refresh(product)
    assert product.name_cn == "路由中文"
    assert product.name_ja == "ルート商品"
    assert product.display_name == "路由中文|ルート商品"


def test_product_edit_updates_name_image_price_status_and_display_name(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="旧中文", name_ja="Old", purchase_price=880, status="active")
    db.add(product)
    db.commit()
    before = product.updated_at

    response = http.post(
        f"/products/{product.id}",
        data={
            "name_cn": "新中文\t  名",
            "name_ja": "新日文　　名",
            "jan": "00001234",
            "main_image_source_url": "https://img.example.test/product.jpg",
            "purchase_price": "123.45",
            "status": "new_pending_review",
            "actor": "测试员",
            "reason": "修正资料",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.name_cn == "新中文 名"
    assert product.name_ja == "新日文 名"
    assert product.display_name == "新中文 名|新日文 名"
    assert len(product.display_name) <= 128
    assert product.jan == "00001234"
    assert product.main_image_source_url == "https://img.example.test/product.jpg"
    assert str(product.purchase_price) == "123.45"
    assert product.status == "new_pending_review"
    assert product.updated_at != before
    log = db.scalar(select(ProductOperationLog).where(ProductOperationLog.product_id == product.id))
    assert log.action == "edit" and log.actor == "测试员" and log.reason == "修正资料"


def test_product_photo_completion_endpoint_saves_image_specs_without_chinese_name(client, monkeypatch, jpeg_bytes):
    http, db, root = client
    image_dir = root / "products" / "main"
    monkeypatch.setattr(product_admin, "PROJECT_ROOT", root)
    monkeypatch.setattr(product_admin, "PRODUCT_IMAGE_DIR", image_dir)
    product = Product(jan="4901234567894", name_ja=None, status="new_pending_completion")
    db.add(product)
    db.commit()

    response = http.post(
        f"/products/{product.id}/photo",
        files={"product_image": ("candidate.jpg", jpeg_bytes, "image/jpeg")},
        data={"name_ja": "Photo Candidate", "spec_text": "W60xH60xD80mm", "actor": "tester"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.jan == "4901234567894"
    assert product.name_cn is None and product.name_ja == "Photo Candidate"
    assert product.main_image_path and (root / product.main_image_path).is_file()
    assert product.display_image_url == f"/product-images/{product.id}"
    assert product.width_mm == 60 and product.height_mm == 60 and product.depth_mm == 80
    assert product.status == "new_pending_review"


def test_restore_auto_image_clears_manual_lock_and_returns_to_remote_source(client, monkeypatch, jpeg_bytes):
    http, db, root = client
    image_dir = root / "products" / "main"
    monkeypatch.setattr(product_admin, "PROJECT_ROOT", root)
    monkeypatch.setattr(product_admin, "PRODUCT_IMAGE_DIR", image_dir)
    product = Product(
        jan="4901234567894",
        name_cn="图片商品",
        main_image_source_url="https://img.example.test/auto.jpg",
    )
    db.add(product)
    db.commit()
    uploaded = http.post(
        f"/products/{product.id}/photo",
        files={"product_image": ("candidate.jpg", jpeg_bytes, "image/jpeg")},
        data={"actor": "tester"},
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    db.refresh(product)
    assert product.main_image_path and product.main_image_locked is True

    restored = http.post(f"/products/{product.id}/restore-auto-image", follow_redirects=False)

    assert restored.status_code == 303
    db.refresh(product)
    assert product.main_image_locked is False
    assert product.main_image_path is None
    assert product.main_image_source_url == "https://img.example.test/auto.jpg"


def test_product_display_name_is_generated_and_truncated_to_128(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="旧", name_ja="Old")
    db.add(product)
    db.commit()

    response = http.post(
        f"/products/{product.id}",
        data={
            "name_cn": "中" * 100,
            "name_ja": "日" * 100,
            "jan": product.jan,
            "purchase_price": "",
            "status": "active",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert "|" in product.display_name
    assert len(product.display_name) <= 128


def test_product_edit_rejects_duplicate_jan(client):
    http, db, _ = client
    first = Product(jan="00123457", name_cn="甲")
    second = Product(jan="76543210", name_cn="乙")
    db.add_all([first, second])
    db.commit()

    response = http.post(
        f"/products/{first.id}",
        data={"name_cn": "甲", "name_ja": "", "jan": second.jan, "purchase_price": "", "status": "active"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    db.refresh(first)
    assert first.jan == "00123457"


def test_unlinked_new_product_can_be_physically_deleted(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="可删除", status="new_pending_review")
    db.add(product)
    db.commit()
    product_id = product.id

    response = http.post(
        f"/products/{product_id}/delete",
        data={"confirm_delete": "1", "actor": "测试员", "reason": "草稿误建"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert db.get(Product, product_id) is None
    log = db.scalar(select(ProductOperationLog).where(ProductOperationLog.internal_sku == product.internal_sku))
    assert log.action == "delete" and log.product_id is None and log.reason == "草稿误建"


def _add_receipt_item(db, product: Product) -> ReceiptItem:
    batch = ReceiptBatch(batch_no=f"PRODUCT-MANAGE-{product.id}", status="confirmed", image_status="ready", gpt_status="reviewed")
    receipt = Receipt(batch=batch, raw_store_name="测试店", confirmation_status="confirmed", review_status="reviewed")
    db.add(receipt)
    db.flush()
    item = ReceiptItem(
        receipt_id=receipt.id,
        line_no=1,
        raw_name=product.name_cn or "商品",
        product_id=product.id,
        match_status="matched_existing",
        quantity=1,
        unit_price=100,
        discount_amount=0,
        line_total=100,
        confidence=1,
        review_status="confirmed",
    )
    db.add(item)
    db.commit()
    return item


def test_receipt_linked_product_cannot_delete_but_can_archive_and_history_stays_visible(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="有关联", status="active")
    db.add(product)
    db.commit()
    _add_receipt_item(db, product)

    deleted = http.post(
        f"/products/{product.id}/delete",
        data={"confirm_delete": "1", "reason": "尝试删除"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    db.refresh(product)
    assert product.status == "active"

    archived = http.post(
        f"/products/{product.id}/archive",
        data={"reason": "有历史记录停用"},
        follow_redirects=False,
    )
    assert archived.status_code == 303
    db.refresh(product)
    assert product.status == "archived"
    listing = http.get("/products")
    assert listing.status_code == 200 and "有关联" not in listing.text
    detail = http.get(f"/products/{product.id}")
    assert detail.status_code == 200 and "有关联" in detail.text and "测试店" in detail.text


def test_qinsi_imported_product_cannot_be_physically_deleted(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="已导入", status="qinsi_product_imported")
    db.add(product)
    db.commit()

    response = http.post(
        f"/products/{product.id}/delete",
        data={"confirm_delete": "1", "reason": "尝试删除"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.status == "qinsi_product_imported"


def test_archived_product_can_be_restored(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="恢复商品", status="archived")
    db.add(product)
    db.commit()

    response = http.post(
        f"/products/{product.id}/restore",
        data={"reason": "重新上架"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db.refresh(product)
    assert product.status == "active"


def test_products_and_product_detail_pages_are_http_200(client):
    http, db, _ = client
    product = Product(jan="00123457", name_cn="页面商品", status="active")
    db.add(product)
    db.commit()
    assert http.get("/products").status_code == 200
    detail = http.get(f"/products/{product.id}")
    assert detail.status_code == 200
    for expected in ("编辑", "停用", "商品状态"):
        assert expected in detail.text


def test_health_reports_database_unavailable():
    class BrokenSession:
        def execute(self, _statement):
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))

    def broken_db():
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_db
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_excel_mapping_document_has_locked_identifier_mappings():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "QINSI_FIELD_MAPPING.md").read_text(encoding="utf-8")
    assert re.search(r"条码\s*→\s*jan", text)
    assert re.search(r"货号\s*→\s*qinsi_product_code", text)
