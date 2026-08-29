from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.field_purchase as field_purchase
from app.config import rakuten_http_referer
from app.field_purchase import (
    assign_field_item_jan,
    confirm_field_item,
    create_field_batch,
    create_new_product_draft,
    process_durable_job,
    product_lookup_payload,
    record_ambiguous_scan_for_review,
    record_existing_scan,
    recover_stale_jobs,
    save_field_product_image,
    update_field_batch_store,
    update_field_item,
)
from app.analytics_service import analytics_dashboard, resolve_date_range
from app.local_product import (
    QINSI_DERIVED_BARCODE_SOURCE,
    derive_jan_from_qinsi_sku,
    ensure_qinsi_derived_barcode,
    resolve_local_product_by_jan,
)
from app.models import (
    DurableBackgroundJob,
    EnrichmentAuditLog,
    FieldPurchaseItem,
    FieldPurchaseBatch,
    FieldPurchaseSyncRequest,
    PlatformProviderState,
    Product,
    ProductAlias,
    ProductBarcode,
    Store,
    TagEvidence,
)
from app.price_providers import (
    AmazonCreatorsPriceProvider,
    PriceCandidate,
    PriceProvider,
    ProviderResponse,
    RakutenPriceProvider,
    YahooShoppingPriceProvider,
)
from app.product_matching import validate_jan
from app.provider_config import provider_status_rows, test_provider_connection as run_provider_test


VALID_JAN = "4901234567894"


def gtin(body: str) -> str:
    full_length = len(body) + 1
    weighted = sum(
        int(digit) * (3 if (full_length - index) % 2 == 0 else 1)
        for index, digit in enumerate(body)
    )
    return f"{body}{(10 - weighted % 10) % 10}"


LEADING_ZERO_JAN = gtin("012345678901")


def make_store(db_session) -> Store:
    store = Store(name="现场测试店", name_cn="现场测试店", name_ja="テスト店")
    db_session.add(store)
    db_session.commit()
    return store


def make_batch(db_session):
    store = make_store(db_session)
    return create_field_batch(
        db_session,
        store_id=store.id,
        operator_name="采购员A",
        client_request_id="batch-request-1",
    )


def test_jan_validation_preserves_leading_zero_and_rejects_empty():
    assert LEADING_ZERO_JAN.startswith("0")
    assert validate_jan(LEADING_ZERO_JAN)
    assert not validate_jan("")
    assert not validate_jan(None)
    assert LEADING_ZERO_JAN != LEADING_ZERO_JAN.lstrip("0")
    assert len(LEADING_ZERO_JAN) == 13


def test_unified_jan_lookup_returns_not_found_and_invalid(db_session):
    assert product_lookup_payload(db_session, VALID_JAN)["status"] == "NOT_FOUND"
    assert product_lookup_payload(db_session, "12345678")["status"] == "INVALID"
    assert resolve_local_product_by_jan(db_session, "123456789012").status == "NOT_FOUND"
    assert resolve_local_product_by_jan(db_session, "123456789013").status == "INVALID"


def test_field_lookup_is_local_only_and_qinsi_code_does_not_fake_jan(db_session, monkeypatch):
    import app.price_service as price_service

    monkeypatch.setattr(price_service, "query_prices", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("online provider called")))
    product = Product(name_cn="秦丝货号不是JAN", qinsi_product_code=VALID_JAN)
    db_session.add(product)
    db_session.commit()

    payload = product_lookup_payload(db_session, VALID_JAN)

    assert payload["status"] == "NOT_FOUND"


def test_formal_jan_wins_over_qinsi_code_for_known_duplicate_shape(db_session):
    imported = Product(name_cn="秦丝旧货号", qinsi_product_code="4550624157131", status="qinsi_product_imported")
    formal = Product(jan="4550624157131", name_cn="正式JAN商品", name_ja="正式JAN", status="new_pending_review")
    db_session.add_all([imported, formal])
    db_session.commit()

    resolution = resolve_local_product_by_jan(db_session, "4550624157131")
    payload = product_lookup_payload(db_session, "4550624157131")

    assert resolution.status == "UNIQUE"
    assert resolution.product.id == formal.id
    assert resolution.match_method == "product_jan"
    assert payload["status"] == "UNIQUE"
    assert payload["product"]["id"] == formal.id


def test_existing_product_barcode_lookup_repeat_scan_and_idempotency(db_session):
    batch = make_batch(db_session)
    product = Product(name_cn="条码商品", name_ja="バーコード商品")
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductBarcode(
            product_id=product.id,
            barcode=LEADING_ZERO_JAN,
            source_system="qinsi",
            is_primary=True,
        )
    )
    db_session.commit()

    lookup = product_lookup_payload(db_session, LEADING_ZERO_JAN, batch.id)
    assert lookup["status"] == "UNIQUE"
    assert lookup["product"]["id"] == product.id
    assert lookup["batch_quantity"] == 0

    first = record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=LEADING_ZERO_JAN,
        client_request_id="scan-1",
    )
    replay = record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=LEADING_ZERO_JAN,
        client_request_id="scan-1",
    )
    second = record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=LEADING_ZERO_JAN,
        client_request_id="scan-2",
    )

    assert first["quantity"] == 1
    assert replay["replayed"] is True
    assert second["quantity"] == 2
    assert db_session.scalar(select(func.count()).select_from(FieldPurchaseItem)) == 1


def test_confirmed_jan_alias_lookup_is_shared_by_price_and_field_purchase(db_session):
    product = Product(name_cn="别名JAN商品")
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductAlias(product_id=product.id, alias=VALID_JAN, normalized_alias=VALID_JAN, confirmed=True))
    db_session.commit()

    resolution = resolve_local_product_by_jan(db_session, VALID_JAN)
    payload = product_lookup_payload(db_session, VALID_JAN)

    assert resolution.status == "UNIQUE"
    assert resolution.match_method == "product_alias_jan"
    assert payload["status"] == "UNIQUE"
    assert payload["product"]["id"] == product.id
    assert payload["match_source"] == "product_alias_jan"


def test_qinsi_sku_derived_jan_backfill_is_not_used_for_plain_scan(db_session):
    reproduction_jan = "4550726010198"
    assert validate_jan(reproduction_jan)
    product = Product(
        name_cn="秦丝派生条码商品",
        name_ja="秦丝派生商品",
        qinsi_product_code=f"/{reproduction_jan}",
    )
    db_session.add(product)
    db_session.commit()
    batch = create_field_batch(
        db_session,
        store_id=None,
        operator_name="采购员A",
        client_request_id="derived-qinsi-batch",
    )

    resolution = resolve_local_product_by_jan(db_session, reproduction_jan)
    assert resolution.status == "NOT_FOUND"
    assert product_lookup_payload(db_session, reproduction_jan, batch.id)["status"] == "NOT_FOUND"
    with pytest.raises(LookupError):
        record_existing_scan(
            db_session,
            batch_id=batch.id,
            jan=reproduction_jan,
            client_request_id="derived-qinsi-scan",
        )
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1

    ensure_qinsi_derived_barcode(db_session, product)
    ensure_qinsi_derived_barcode(db_session, product)
    db_session.commit()
    aliases = list(
        db_session.scalars(
            select(ProductBarcode).where(ProductBarcode.barcode == reproduction_jan)
        )
    )
    assert len(aliases) == 1
    assert aliases[0].source_system == QINSI_DERIVED_BARCODE_SOURCE
    assert aliases[0].is_primary is False
    assert resolve_local_product_by_jan(db_session, reproduction_jan).status == "NOT_FOUND"


def test_qinsi_sku_derived_jan_preserves_leading_zero_and_rejects_unsafe_forms(db_session):
    leading = Product(qinsi_product_code=f"/{LEADING_ZERO_JAN}", name_cn="前导零")
    invalid = Product(qinsi_product_code="/4550726010199", name_cn="错误校验位")
    ordinary = Product(qinsi_product_code="SKU/4550726010198", name_cn="普通斜杠SKU")
    db_session.add_all([leading, invalid, ordinary])
    db_session.commit()

    assert derive_jan_from_qinsi_sku(leading.qinsi_product_code) == LEADING_ZERO_JAN
    assert resolve_local_product_by_jan(db_session, LEADING_ZERO_JAN).product is None
    assert derive_jan_from_qinsi_sku(invalid.qinsi_product_code) is None
    assert derive_jan_from_qinsi_sku(ordinary.qinsi_product_code) is None
    assert resolve_local_product_by_jan(db_session, "4550726010198").product is None


def test_local_jan_conflict_never_auto_matches(db_session):
    jan = "4550726010198"
    barcode_product = Product(name_cn="条码商品")
    derived_product = Product(name_cn="派生商品", qinsi_product_code=f"/{jan}")
    db_session.add_all([barcode_product, derived_product])
    db_session.flush()
    db_session.add(ProductBarcode(
        product_id=barcode_product.id,
        barcode=jan,
        source_system="qinsi",
        is_primary=True,
    ))
    db_session.commit()

    resolution = resolve_local_product_by_jan(db_session, jan)
    assert resolution.is_unique
    assert resolution.product.id == barcode_product.id
    payload = product_lookup_payload(db_session, jan)
    assert payload["status"] == "UNIQUE"
    assert payload["product"]["id"] == barcode_product.id
    batch = create_field_batch(
        db_session,
        store_id=None,
        operator_name="采购员A",
        client_request_id="ambiguous-review-batch",
    )
    selected = record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=jan,
        client_request_id="ambiguous-selected",
        selected_product_id=barcode_product.id,
    )
    assert selected["product"]["id"] == barcode_product.id
    with pytest.raises(ValueError):
        record_ambiguous_scan_for_review(
            db_session,
            batch_id=batch.id,
            jan=jan,
            client_request_id="ambiguous-review",
        )
    try:
        ensure_qinsi_derived_barcode(db_session, derived_product)
    except ValueError as exc:
        assert "其他商品" in str(exc) or "多个商品" in str(exc)
    else:
        raise AssertionError("已被普通条码占用的派生 JAN 不应创建秦丝派生条码别名")
    assert db_session.scalar(
        select(func.count())
        .select_from(ProductBarcode)
        .where(
            ProductBarcode.barcode == jan,
            ProductBarcode.source_system == QINSI_DERIVED_BARCODE_SOURCE,
        )
    ) == 0


def test_same_jan_products_are_rejected_and_unique_product_scans_directly(db_session):
    jan = "4571609352419"
    red = Product(
        jan=jan,
        qinsi_product_code="Q-RED",
        qinsi_name="红色款",
        name_cn="红色款",
        specification="红",
        status="qinsi_product_imported",
    )
    blue = Product(
        jan=jan,
        qinsi_product_code="Q-BLUE",
        qinsi_name="蓝色款",
        name_cn="蓝色款",
        specification="蓝",
        status="qinsi_product_imported",
    )
    db_session.add_all([red, blue])
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(red)
    db_session.commit()

    resolution = resolve_local_product_by_jan(db_session, jan)
    assert resolution.product.id == red.id
    assert not resolution.is_conflict
    payload = product_lookup_payload(db_session, jan)
    assert payload["status"] == "UNIQUE"
    assert payload["product"]["id"] == red.id

    batch = create_field_batch(
        db_session,
        store_id=None,
        operator_name="采购员A",
        client_request_id="same-jan-candidate-batch",
    )
    scanned = record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=jan,
        client_request_id="same-jan-unique",
    )
    assert scanned["product"]["id"] == red.id
    assert db_session.scalar(select(FieldPurchaseItem.product_id)) == red.id


def test_optional_field_store_allows_scanning_then_later_update_and_analytics(client):
    http, db_session, _ = client
    response = http.post(
        "/field-purchase/batches",
        data={
            "store_id": "",
            "operator_name": "无门店采购员",
            "client_request_id": "optional-store-batch",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    batch = db_session.scalar(
        select(FieldPurchaseBatch).where(
            FieldPurchaseBatch.client_request_id == "optional-store-batch"
        )
    )
    assert batch.store_id is None

    product = Product(jan=VALID_JAN, name_cn="连续扫码商品")
    db_session.add(product)
    db_session.commit()
    record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=VALID_JAN,
        client_request_id="optional-store-scan-1",
    )
    record_existing_scan(
        db_session,
        batch_id=batch.id,
        jan=VALID_JAN,
        client_request_id="optional-store-scan-2",
    )
    db_session.refresh(batch)
    started_at = batch.started_at
    if started_at.tzinfo is None:  # SQLite returns UTC timestamps without tzinfo.
        started_at = started_at.replace(tzinfo=timezone.utc)
    local_day = started_at.astimezone(timezone(timedelta(hours=9))).date()
    period = resolve_date_range("custom", local_day, local_day, today=local_day)
    before = analytics_dashboard(db_session, period)
    assert before["field_purchase_stores"][0]["label"] == "未填写门店"
    assert before["field_purchase_stores"][0]["quantity"] == 2

    page = http.get(f"/field-purchase?batch_id={batch.id}")
    assert page.status_code == 200
    default_field_page = http.get("/field-purchase").text
    assert batch.batch_no in default_field_page
    assert "门店稍后补充" in default_field_page
    assert "未填写门店" in page.text

    store = make_store(db_session)
    updated = http.post(
        f"/field-purchase/batches/{batch.id}/store",
        data={"store_id": str(store.id)},
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db_session.expire_all()
    assert db_session.get(FieldPurchaseBatch, batch.id).store_id == store.id
    after = analytics_dashboard(db_session, period)
    assert after["field_purchase_stores"][0]["store"].id == store.id
    assert after["field_purchase_stores"][0]["quantity"] == 2

    update_field_batch_store(db_session, batch.id, store_id=None)
    assert db_session.get(FieldPurchaseBatch, batch.id).store_id is None


def test_new_draft_requires_tag_photo_is_retryable_and_no_jan_uses_temp_id(
    db_session,
    tmp_path,
    monkeypatch,
    jpeg_bytes,
):
    tag_dir = tmp_path / "tags"
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tag_dir)
    batch = make_batch(db_session)

    try:
        create_new_product_draft(
            db_session,
            batch_id=batch.id,
            client_request_id="draft-retry",
            photo_content=b"",
            photo_content_type="image/jpeg",
            photo_filename="tag.jpg",
            jan=None,
            temporary_id="TMP-OFFLINE-1",
        )
    except ValueError as exc:
        assert "不能为空" in str(exc)
    else:
        raise AssertionError("empty tag photo accepted")
    assert db_session.scalar(select(func.count()).select_from(FieldPurchaseSyncRequest)) == 0

    payload, job = create_new_product_draft(
        db_session,
        batch_id=batch.id,
        client_request_id="draft-retry",
        photo_content=jpeg_bytes,
        photo_content_type="image/jpeg",
        photo_filename="tag.jpg",
        jan=None,
        temporary_id="TMP-OFFLINE-1",
    )
    replay, replay_job = create_new_product_draft(
        db_session,
        batch_id=batch.id,
        client_request_id="draft-retry",
        photo_content=jpeg_bytes,
        photo_content_type="image/jpeg",
        photo_filename="tag.jpg",
        jan=None,
        temporary_id="TMP-OFFLINE-1",
    )
    second_scan, _ = create_new_product_draft(
        db_session,
        batch_id=batch.id,
        client_request_id="draft-second-scan",
        photo_content=jpeg_bytes,
        photo_content_type="image/jpeg",
        photo_filename="tag.jpg",
        jan=None,
        temporary_id="TMP-OFFLINE-1",
    )
    item = db_session.get(FieldPurchaseItem, payload["item_id"])
    evidence = db_session.scalar(select(TagEvidence).where(TagEvidence.field_purchase_item_id == item.id))

    assert item.jan is None
    assert item.temporary_id == "TMP-OFFLINE-1"
    assert item.status == "ENRICHMENT_PENDING"
    assert evidence and (tmp_path / evidence.file_path).read_bytes() == jpeg_bytes
    assert replay["replayed"] is True
    assert replay_job.id == job.id
    assert second_scan["item_id"] == item.id and item.quantity == 2
    assert db_session.scalar(select(func.count()).select_from(TagEvidence)) == 1


def test_later_jan_merges_draft_into_existing_batch_item_without_new_product(
    db_session, tmp_path, monkeypatch, jpeg_bytes,
):
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tmp_path / "tags")
    batch = make_batch(db_session)
    product = Product(jan=VALID_JAN, name_cn="已有商品")
    db_session.add(product)
    db_session.commit()
    existing = record_existing_scan(
        db_session, batch_id=batch.id, jan=VALID_JAN, client_request_id="merge-existing",
    )
    payload, _ = create_new_product_draft(
        db_session, batch_id=batch.id, client_request_id="merge-draft",
        photo_content=jpeg_bytes, photo_content_type="image/jpeg", photo_filename="tag.jpg",
        jan=None, temporary_id="TMP-MERGE-1",
    )
    merged = assign_field_item_jan(db_session, payload["item_id"], VALID_JAN, actor="审核员")
    assert merged.id == existing["item_id"] and merged.quantity == 2 and merged.product_id == product.id
    assert db_session.get(FieldPurchaseItem, payload["item_id"]) is None
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1
    assert db_session.scalar(select(func.count()).select_from(TagEvidence).where(TagEvidence.field_purchase_item_id == merged.id)) == 1


def test_stale_running_job_recovers_after_restart_and_moves_to_review(
    db_session,
    tmp_path,
    monkeypatch,
    jpeg_bytes,
):
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tmp_path / "tags")
    monkeypatch.setenv("JBA_FIELD_JOB_STALE_MINUTES", "5")
    batch = make_batch(db_session)
    payload, job = create_new_product_draft(
        db_session,
        batch_id=batch.id,
        client_request_id="restart-draft",
        photo_content=jpeg_bytes,
        photo_content_type="image/jpeg",
        photo_filename="tag.jpg",
        jan=None,
        temporary_id="TMP-RESTART-1",
    )
    job.status = "RUNNING"
    job.attempts = 1
    job.locked_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    db_session.commit()

    assert recover_stale_jobs(db_session) == 1
    assert db_session.get(DurableBackgroundJob, job.id).status == "PENDING"
    process_durable_job(job.id, db_session.get_bind())
    db_session.expire_all()

    item = db_session.get(FieldPurchaseItem, payload["item_id"])
    evidence = db_session.scalar(select(TagEvidence).where(TagEvidence.field_purchase_item_id == item.id))
    recovered = db_session.get(DurableBackgroundJob, job.id)
    assert recovered.status == "COMPLETED"
    assert item.status == "NEEDS_REVIEW"
    assert evidence.ocr_status == "UNCONFIGURED"


def test_manual_review_blocks_no_jan_product_without_fabricating_barcode(
    db_session,
    tmp_path,
    monkeypatch,
    jpeg_bytes,
):
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tmp_path / "tags")
    batch = make_batch(db_session)
    payload, _ = create_new_product_draft(
        db_session,
        batch_id=batch.id,
        client_request_id="manual-no-jan",
        photo_content=jpeg_bytes,
        photo_content_type="image/jpeg",
        photo_filename="tag.jpg",
        jan=None,
        temporary_id="TMP-MANUAL-1",
    )
    update_field_item(
        db_session,
        payload["item_id"],
        actor="审核员",
        name_cn="无条码商品",
        name_ja="バーコードなし",
        unit_price=980,
        brand="测试品牌",
        category="测试分类",
        unit_name="个",
    )
    try:
        confirm_field_item(db_session, payload["item_id"], actor="审核员")
    except ValueError as exc:
        assert "合法 JAN" in str(exc)
    else:
        raise AssertionError("无 JAN 草稿不应自动创建 Product")
    assert db_session.scalar(select(func.count()).select_from(Product)) == 0


def test_photo_completion_updates_existing_same_jan_and_does_not_guess_jan(
    db_session,
    tmp_path,
    monkeypatch,
    jpeg_bytes,
):
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tmp_path / "tags")
    monkeypatch.setattr(field_purchase, "PRODUCT_IMAGE_DIR", tmp_path / "products")
    batch = make_batch(db_session)
    existing = Product(jan=VALID_JAN, status="new_pending_completion", name_ja="缺商品")
    db_session.add(existing)
    db_session.flush()
    item = FieldPurchaseItem(
        batch_id=batch.id,
        jan=VALID_JAN,
        quantity=1,
        status="NEEDS_REVIEW",
        name_ja="写真候補商品 140g",
        captured_by="采购员A",
    )
    db_session.add(item)
    db_session.commit()
    save_field_product_image(
        db_session,
        item.id,
        actor="审核员",
        content=jpeg_bytes,
        content_type="image/jpeg",
        original_filename="photo.jpg",
    )
    evidence = db_session.scalar(select(TagEvidence).where(TagEvidence.field_purchase_item_id == item.id))
    evidence.ocr_status = "COMPLETED"
    evidence.ocr_text = "JAN 4570110290418 W60×H60×D80mm 140g"
    db_session.commit()

    product = confirm_field_item(db_session, item.id, actor="审核员")

    assert product.id == existing.id
    assert product.jan == VALID_JAN
    assert product.name_ja == "写真候補商品 140g"
    assert product.source == "photo"
    assert product.name_source == "photo"
    assert product.needs_review is True
    assert product.main_image_path and (tmp_path / product.main_image_path).is_file()
    assert product.net_weight_g == 140
    assert product.width_mm == 60 and product.height_mm == 60 and product.depth_mm == 80
    assert db_session.scalar(select(func.count()).select_from(Product).where(Product.jan == VALID_JAN)) == 1
    assert db_session.scalar(select(Product).where(Product.jan == "4570110290418")) is None


def test_field_purchase_api_and_navigation_permissions(client, monkeypatch, jpeg_bytes):
    test_client, db_session, _ = client
    store = make_store(db_session)
    batch = create_field_batch(
        db_session,
        store_id=store.id,
        operator_name="手机采购员",
        client_request_id="api-batch",
    )
    product = Product(jan=VALID_JAN, name_cn="已有商品", name_ja="既存商品")
    db_session.add(product)
    db_session.commit()

    page = test_client.get(f"/field-purchase?batch_id={batch.id}")
    assert page.status_code == 200
    assert "fieldCameraSelect" in page.text
    assert "拍商品照片" in page.text
    default_page = test_client.get("/field-purchase")
    assert batch.batch_no in default_page.text
    assert "当前批次暂无商品" in default_page.text
    assert "尚未建立采购批次" not in default_page.text
    response = test_client.post(
        "/api/field-purchase/scans",
        json={
            "batch_id": batch.id,
            "jan": VALID_JAN,
            "quantity": 1,
            "client_request_id": "api-scan-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["product"]["id"] == product.id
    draft = test_client.post(
        "/api/field-purchase/drafts",
        data={
            "batch_id": str(batch.id),
            "client_request_id": "api-draft-1",
            "jan": "",
            "temporary_id": "TMP-API-1",
            "name": "",
            "unit_price": "",
            "quantity": "1",
        },
        files={"tag_photo": ("tag.jpg", jpeg_bytes, "image/jpeg")},
    )
    replay = test_client.post(
        "/api/field-purchase/drafts",
        data={
            "batch_id": str(batch.id),
            "client_request_id": "api-draft-1",
            "jan": "",
            "temporary_id": "TMP-API-1",
            "quantity": "1",
        },
        files={"tag_photo": ("tag.jpg", jpeg_bytes, "image/jpeg")},
    )
    assert draft.status_code == 201
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    db_session.expire_all()
    assert db_session.get(FieldPurchaseItem, draft.json()["item_id"]).status == "NEEDS_REVIEW"

    monkeypatch.setenv("JBA_DEFAULT_ROLE", "buyer")
    buyer_more = test_client.get("/more")
    assert "现场作业" in buyer_more.text
    assert "秦丝数据" not in buyer_more.text
    assert "/tasks" in buyer_more.text
    assert "/receipts" in buyer_more.text and "小票记录" in buyer_more.text
    buyer_tasks = test_client.get("/tasks")
    assert "小票批次 / 小票记录" in buyer_tasks.text
    monkeypatch.setenv("JBA_DEFAULT_ROLE", "admin")
    admin_more = test_client.get("/more")
    assert "秦丝数据" in admin_more.text


def test_camera_contract_uses_moderate_resolution_roi_and_safe_capability_fallback():
    root = Path(__file__).resolve().parents[1]
    script = (root / "app" / "static" / "field_purchase.js").read_text(encoding="utf-8")
    adapter = (root / "app" / "static" / "camera_adapter.js").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")
    field_template = (root / "app" / "templates" / "field_purchase.html").read_text(encoding="utf-8")
    price_template = (root / "app" / "templates" / "price_check.html").read_text(encoding="utf-8")

    for contract in (
        "width: {ideal: 1280}",
        "height: {ideal: 720}",
        "frameRate: {ideal: 30, max: 30}",
        'constraints.resizeMode = {ideal: "none"}',
        "constraints.zoom = {ideal: 1}",
        "enumerateDevices",
        "getCapabilities",
        'advanced.focusMode = "continuous"',
        "isVideoFrameReady",
        "detector.detect(video)",
        "stream.getTracks().forEach((track) => track.stop())",
        'objectFit = "contain"',
        'video: {facingMode: {ideal: "environment"}}',
        "START_TIMEOUT_MS = 10000",
        "cameraErrorDetails",
    ):
        assert contract in adapter
    assert "new window.JBACamera.UnifiedJanScanner" in script
    assert "field-iphone-p0-20260726-2" in script and "field-iphone-p0-20260726-2" in field_template
    assert "function deriveNextItemState()" in script
    assert "currentFlowGeneration" in script and "localLookupController?.abort()" in script
    assert "unlockScanAudio" in script and "AudioContext" in script
    assert "✓ 已识别" in script and "SCAN_SUCCESS_PAUSE_MS = 950" in script
    assert "pauseAfterSuccess" in adapter and "full_frame_fallback" in adapter
    assert "localLookup" in script and "requestId" in script and "elapsedMs" in script and "matchSource" in script
    for diagnostic in (
        "fileSelected",
        "fileName",
        "fileType",
        "fileSize",
        "fileReadStarted",
        "fileReadFinished",
        "previewReady",
        "indexedDbWriteStarted",
        "indexedDbWriteSucceeded",
        "localPhotoId",
        "syncStatus",
        "lastPhotoError",
        "local_query_failed_retryable",
    ):
        assert diagnostic in script
    assert "tagPhoto.value = \"\";" in script
    assert "已保存到本机；服务器待同步" in script
    assert "本地商品查询失败，请重试。" in script
    assert "finally" in script
    assert "UnifiedJanScanner" in adapter
    assert "decode loop running" in adapter
    assert "framesTotal" in script
    assert "videoWidth" in script and "videoHeight" in script
    assert "UnifiedJanScanner" in price_template
    assert "camera 0, facing back" in adapter
    assert 'facingMode = {ideal: "environment"}' in adapter
    assert "capabilities.torch === true" in adapter
    assert "applyConstraints({advanced: [{torch: Boolean(enabled)}]})" in adapter
    assert "切换镜头 / 高级设置" in field_template and "切换镜头 / 高级设置" in price_template
    assert "playsinline muted autoplay" in field_template and "playsinline muted autoplay" in price_template
    assert ".field-camera-stage video" in css and "object-fit:contain" in css
    assert ".field-scan-guide" in css and "width:88%" in css
    assert "position:fixed" in css and "z-index:10000" in css
    assert "jba-field-shell-v5" in (root / "app" / "static" / "service-worker.js").read_text(encoding="utf-8")


def test_camera_adapter_android_ios_and_torch_mocks():
    node = shutil.which("node")
    assert node, "CameraAdapter Mock 测试需要 Node.js"
    root = Path(__file__).resolve().parents[1]
    adapter_path = json.dumps(str(root / "app" / "static" / "camera_adapter.js"))
    program = f"""
const assert = require("assert");
const api = require({adapter_path});
function storage(initial = {{}}) {{
  const values = new Map(Object.entries(initial));
  return {{
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    value: (key) => values.get(key),
  }};
}}
function makeTrack(deviceId, capabilities, options = {{}}) {{
  return {{
    stopped: false,
    calls: [],
    getSettings: () => ({{deviceId, width: 1920, height: 1080, zoom: 1}}),
    getCapabilities: () => capabilities,
    stop() {{ this.stopped = true; }},
    async applyConstraints(value) {{
      this.calls.push(value);
      if (value.advanced && value.advanced[0] && value.advanced[0].torch && options.failTorch) {{
        const error = new Error("webkit torch");
        error.name = options.failTorch;
        throw error;
      }}
    }},
  }};
}}
function makeStream(track) {{
  return {{getVideoTracks: () => [track], getTracks: () => [track]}};
}}
(async () => {{
  const androidDevices = [
    {{kind: "videoinput", deviceId: "wide", label: "camera 2, facing back"}},
    {{kind: "videoinput", deviceId: "main", label: "camera 0, facing back"}},
  ];
  const androidTrack = makeTrack("main", {{torch: true, zoom: {{min: 1, max: 4, step: 0.5}}, focusMode: ["continuous"]}});
  const androidCalls = [];
  const androidStorage = storage();
  const android = new api.CameraAdapter({{
    mediaDevices: {{
      enumerateDevices: async () => androidDevices,
      getUserMedia: async (constraints) => {{ androidCalls.push(constraints); return makeStream(androidTrack); }},
    }},
    storage: androidStorage,
    userAgent: "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36",
    secureContext: true,
  }});
  const androidState = await android.start();
  assert.deepEqual(androidCalls[0], api.MINIMAL_CONSTRAINTS);
  assert.equal(androidStorage.value(api.DEFAULT_DEVICE_KEY), "main");
  assert.equal(androidState.torchSupported, true);
  assert.equal((await android.setTorch(true)).enabled, true);
  assert.deepEqual(androidTrack.calls[androidTrack.calls.length - 1], {{advanced: [{{torch: true}}]}});

  const rememberedStorage = storage();
  rememberedStorage.setItem(api.DEFAULT_DEVICE_KEY, "wide");
  const rememberedWideTrack = makeTrack("wide", {{}});
  const rememberedMainTrack = makeTrack("main", {{}});
  const rememberedCalls = [];
  const remembered = new api.CameraAdapter({{
    mediaDevices: {{
      enumerateDevices: async () => androidDevices,
      getUserMedia: async (constraints) => {{
        rememberedCalls.push(constraints);
        const requested = constraints.video.deviceId && constraints.video.deviceId.exact;
        return makeStream(requested === "wide" ? rememberedWideTrack : rememberedMainTrack);
      }},
    }},
    storage: rememberedStorage,
    userAgent: "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36",
    secureContext: true,
  }});
  await remembered.start();
  assert.deepEqual(rememberedCalls[0], api.MINIMAL_CONSTRAINTS);
  assert.equal(rememberedCalls[1].video.deviceId.exact, "wide");
  await remembered.start("main");
  assert.equal(rememberedWideTrack.stopped, true);
  assert.equal(rememberedStorage.value(api.DEFAULT_DEVICE_KEY), "main");

  const iosTrack = makeTrack("ios-back", {{}});
  const iosCalls = [];
  const iosStorage = storage();
  const iosEvents = [];
  const ios = new api.CameraAdapter({{
    mediaDevices: {{
      enumerateDevices: async () => {{ iosEvents.push("enumerate"); return androidDevices; }},
      getUserMedia: async (constraints) => {{ iosEvents.push("getUserMedia"); iosCalls.push(constraints); return makeStream(iosTrack); }},
    }},
    storage: iosStorage,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    platform: "iPhone",
    secureContext: true,
  }});
  await ios.start("", {{onStream: async () => iosEvents.push("play")}});
  assert.deepEqual(iosEvents.slice(0, 3), ["getUserMedia", "play", "enumerate"]);
  assert.deepEqual(iosCalls[0].video.facingMode, {{ideal: "environment"}});
  assert.equal(iosCalls[0].video.deviceId, undefined);
  assert.equal(iosCalls[0].video.zoom, undefined);
  assert.equal(iosStorage.value(api.DEFAULT_DEVICE_KEY), "ios-back");

  const failingTrack = makeTrack("torch-fail", {{torch: true}}, {{failTorch: "AbortError"}});
  const failing = new api.CameraAdapter({{
    mediaDevices: {{
      enumerateDevices: async () => [{{kind: "videoinput", deviceId: "torch-fail", label: "back"}}],
      getUserMedia: async () => makeStream(failingTrack),
    }},
    storage: storage(),
    userAgent: "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36",
    secureContext: true,
  }});
  await failing.start("torch-fail");
  const firstFailure = await failing.setTorch(true);
  const repeatedFailure = await failing.setTorch(true);
  assert.equal(firstFailure.message, api.TORCH_UNSUPPORTED_MESSAGE);
  assert.equal(firstFailure.shouldNotify, true);
  assert.equal(repeatedFailure.shouldNotify, false);
  assert.equal(repeatedFailure.supported, false);

  const fallbackCalls = [];
  const environmentTrack = makeTrack("environment", {{}});
  const fallbackTrack = makeTrack("fallback", {{}});
  const invalidSaved = new api.CameraAdapter({{
    mediaDevices: {{
      enumerateDevices: async () => [{{kind:"videoinput",deviceId:"gone",label:"camera 0, facing back"}}],
      getUserMedia: async (constraints) => {{
        fallbackCalls.push(constraints);
        if (fallbackCalls.length === 1) return makeStream(environmentTrack);
        if (constraints.video === true) return makeStream(fallbackTrack);
        const error = new Error("stale device"); error.name = "OverconstrainedError"; throw error;
      }},
    }},
    storage: storage({{[api.DEFAULT_DEVICE_KEY]: "gone"}}),
    userAgent: "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36",
    secureContext: true,
  }});
  const fallbackState = await invalidSaved.start();
  assert.deepEqual(fallbackCalls[0], api.MINIMAL_CONSTRAINTS);
  assert.equal(fallbackCalls[1].video.deviceId.exact, "gone");
  assert.deepEqual(fallbackCalls[2], api.MINIMAL_CONSTRAINTS);
  assert.equal(fallbackCalls[3].video, true);
  assert.equal(fallbackState.settings.deviceId, "fallback");

  const timeoutAdapter = new api.CameraAdapter({{
    mediaDevices: {{enumerateDevices: async()=>[], getUserMedia: () => new Promise(() => {{}})}},
    storage: storage(), secureContext: true, startTimeoutMs: 20,
  }});
  let timeoutName = "";
  try {{ await timeoutAdapter.start(); }} catch (error) {{ timeoutName = error.name; }}
  assert.equal(timeoutName, "TimeoutError");

  const playTrack = makeTrack("play", {{}});
  const playAdapter = new api.CameraAdapter({{
    mediaDevices: {{enumerateDevices: async()=>[], getUserMedia: async()=>makeStream(playTrack)}},
    storage: storage(), secureContext: true,
  }});
  let playName = "";
  try {{ await playAdapter.start("", {{onStream: async()=>{{throw new Error("play rejected")}}}}); }}
  catch (error) {{ playName = error.name; }}
  assert.equal(playName, "PlaybackError");
  const busy = new Error("camera busy"); busy.name = "NotReadableError";
  assert(api.cameraErrorDetails(busy, {{secureContext:true,platform:{{kind:"ios-webkit"}},mediaDevices:{{getUserMedia(){{}}}}}}).message.includes("占用"));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        [node, "-e", program],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_unified_jan_scanner_skips_decode_until_video_ready_and_disposes_on_restart():
    node = shutil.which("node")
    assert node, "UnifiedJanScanner Mock 测试需要 Node.js"
    root = Path(__file__).resolve().parents[1]
    adapter_path = json.dumps(str(root / "app" / "static" / "camera_adapter.js"))
    program = f"""
const assert = require("assert");
const api = require({adapter_path});

let decodeCalls = 0;
let readerInstances = [];
class FakeReader {{
  constructor() {{ this.resetCalls = 0; readerInstances.push(this); }}
  decode(video) {{
    decodeCalls += 1;
    const error = new Error("not found");
    error.name = "NotFoundException";
    throw error;
  }}
  reset() {{ this.resetCalls += 1; }}
}}
globalThis.ZXingBrowser = {{
  BrowserMultiFormatReader: FakeReader,
  BarcodeFormat: {{EAN_13: 1, EAN_8: 2, UPC_A: 3}},
}};

const listeners = {{}};
const video = {{
  readyState: 0, videoWidth: 0, videoHeight: 0, srcObject: null,
  classList: {{add() {{}}, remove() {{}}}},
  addEventListener(type, fn) {{ (listeners[type] = listeners[type] || []).push(fn); }},
  removeEventListener(type, fn) {{
    if (!listeners[type]) return;
    listeners[type] = listeners[type].filter((item) => item !== fn);
  }},
  play: async () => {{}},
}};
function fireVideoEvent(type) {{ (listeners[type] || []).slice().forEach((fn) => fn()); }}

const fakeTrack = {{getSettings: () => ({{}})}};
const fakeStream = {{getVideoTracks: () => [fakeTrack]}};
const fakeCameraAdapter = {{
  async start(deviceId, opts) {{
    await opts.onStream(fakeStream);
    return {{
      stream: fakeStream, track: fakeTrack, devices: [], capabilities: {{}},
      settings: {{}}, torchSupported: false, warnings: [],
    }};
  }},
  stop() {{}},
}};

(async () => {{
  const scanner = new api.UnifiedJanScanner({{video, cameraAdapter: fakeCameraAdapter, onCode: () => {{}}}});
  const startPromise = scanner.start();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(decodeCalls, 0, "decode must not run before readyState/videoWidth/videoHeight are ready");

  video.readyState = 2; video.videoWidth = 640; video.videoHeight = 480;
  fireVideoEvent("loadedmetadata");
  await startPromise;
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert(decodeCalls >= 1, "decode should start running once the video frame is ready");
  assert.equal(readerInstances.length, 1);

  const firstReader = readerInstances[0];
  const generationAfterFirstStart = scanner.loopGeneration;

  // Simulate "继续扫码": video briefly reports not-ready again mid-restart,
  // then becomes ready — the scanner must fully dispose the old reader/stream
  // before starting a fresh one, and must not decode against the stale frame.
  video.readyState = 0; video.videoWidth = 0; video.videoHeight = 0;
  const restartPromise = scanner.start();
  assert.equal(firstReader.resetCalls, 1, "old zxing reader must be reset() during restart's stop()");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(readerInstances.length, 1, "no new reader should be created until the restarted frame is ready");

  video.readyState = 2; video.videoWidth = 640; video.videoHeight = 480;
  fireVideoEvent("loadedmetadata");
  await restartPromise;
  await new Promise((resolve) => setTimeout(resolve, 60));

  assert.equal(readerInstances.length, 2, "restart must create exactly one new reader, not stack loops");
  assert(scanner.loopGeneration > generationAfterFirstStart, "loop generation must advance so the old decode closure self-cancels");

  scanner.stop("test_complete");
  process.exit(0);
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        [node, "-e", program],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_zxing_settle_delay_applies_only_to_zxing_not_barcode_detector():
    node = shutil.which("node")
    assert node, "UnifiedJanScanner Mock 测试需要 Node.js"
    root = Path(__file__).resolve().parents[1]
    adapter_path = json.dumps(str(root / "app" / "static" / "camera_adapter.js"))
    program = f"""
const assert = require("assert");
const api = require({adapter_path});

function makeVideo() {{
  const listeners = {{}};
  return {{
    readyState: 2, videoWidth: 640, videoHeight: 480, srcObject: null,
    classList: {{add() {{}}, remove() {{}}}},
    addEventListener(type, fn) {{ (listeners[type] = listeners[type] || []).push(fn); }},
    removeEventListener() {{}},
    play: async () => {{}},
  }};
}}

function makeFakeCameraAdapter() {{
  const fakeTrack = {{getSettings: () => ({{}})}};
  const fakeStream = {{getVideoTracks: () => [fakeTrack]}};
  return {{
    async start(deviceId, opts) {{
      await opts.onStream(fakeStream);
      return {{
        stream: fakeStream, track: fakeTrack, devices: [], capabilities: {{}},
        settings: {{}}, torchSupported: false, warnings: [],
      }};
    }},
    stop() {{}},
  }};
}}

(async () => {{
  // ZXing path: video is ready from the very first tick (readyState=2 already),
  // so the only thing that can delay the first decode() call is zxingSettleDelayMs.
  let zxingDecodeCalls = 0;
  class FakeReader {{
    decode() {{ zxingDecodeCalls += 1; const e = new Error("nf"); e.name = "NotFoundException"; throw e; }}
    reset() {{}}
  }}
  delete globalThis.BarcodeDetector;
  globalThis.ZXingBrowser = {{
    BrowserMultiFormatReader: FakeReader,
    BarcodeFormat: {{EAN_13: 1, EAN_8: 2, UPC_A: 3}},
  }};
  const zxingScanner = new api.UnifiedJanScanner({{
    video: makeVideo(), cameraAdapter: makeFakeCameraAdapter(), onCode: () => {{}},
    zxingSettleDelayMs: 200,
  }});
  const zxingStart = zxingScanner.start();
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(zxingDecodeCalls, 0, "ZXing must wait out zxingSettleDelayMs before its first decode() call");
  await zxingStart;
  assert(zxingDecodeCalls >= 1, "ZXing should have decoded at least once once start() resolves");
  zxingScanner.stop("done");

  // Android/Chrome path: BarcodeDetector is natively available, so the settle
  // delay (a ZXing-only workaround for iOS Safari's software decoder) must
  // not slow this path down at all -- Android should stay exactly as fast.
  let detectorCalls = 0;
  globalThis.BarcodeDetector = class {{
    static async getSupportedFormats() {{ return ["ean_13", "ean_8", "upc_a", "upc_e"]; }}
    async detect() {{ detectorCalls += 1; return []; }}
  }};
  const detectorScanner = new api.UnifiedJanScanner({{
    video: makeVideo(), cameraAdapter: makeFakeCameraAdapter(), onCode: () => {{}},
    zxingSettleDelayMs: 200,
  }});
  const detectorStart = detectorScanner.start();
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert(detectorCalls >= 1, "BarcodeDetector (Android/Chrome) must not be held back by the ZXing-only settle delay");
  await detectorStart;
  detectorScanner.stop("done");

  process.exit(0);
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        [node, "-e", program],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_render_diagnostics_does_not_force_debug_panel_visible():
    # renderDiagnostics() used to set `this.debug.hidden = false` on every
    # single decode attempt (many times per second while scanning), which
    # meant the raw diagnostics JSON popped open for every price-check user
    # as soon as they started scanning, regardless of the page's own
    # hidden-by-default intent (?debug=1 only). It must now only refresh the
    # content, leaving visibility entirely up to the caller.
    node = shutil.which("node")
    assert node, "UnifiedJanScanner Mock 测试需要 Node.js"
    root = Path(__file__).resolve().parents[1]
    adapter_path = json.dumps(str(root / "app" / "static" / "camera_adapter.js"))
    program = f"""
const assert = require("assert");
const api = require({adapter_path});

const debugEl = {{hidden: true, textContent: ""}};
const video = {{
  readyState: 2, videoWidth: 640, videoHeight: 480, srcObject: null,
  classList: {{add() {{}}, remove() {{}}}},
  addEventListener() {{}}, removeEventListener() {{}},
  play: async () => {{}},
}};
const fakeTrack = {{getSettings: () => ({{}})}};
const fakeStream = {{getVideoTracks: () => [fakeTrack]}};
const fakeCameraAdapter = {{
  async start(deviceId, opts) {{
    await opts.onStream(fakeStream);
    return {{stream: fakeStream, track: fakeTrack, devices: [], capabilities: {{}}, settings: {{}}, torchSupported: false, warnings: []}};
  }},
  stop() {{}},
}};
globalThis.BarcodeDetector = class {{
  static async getSupportedFormats() {{ return ["ean_13"]; }}
  async detect() {{ return []; }}
}};

(async () => {{
  const scanner = new api.UnifiedJanScanner({{video, cameraAdapter: fakeCameraAdapter, debug: debugEl, onCode: () => {{}}}});
  await scanner.start();
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(debugEl.hidden, true, "renderDiagnostics() must not force the debug panel visible on its own");
  assert(debugEl.textContent.length > 0, "renderDiagnostics() must still refresh the content for callers that DO show it");
  scanner.stop("done");
  process.exit(0);
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        [node, "-e", program],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


class MockSuccessProvider(PriceProvider):
    code = "mock_success"
    display_name = "Mock Success"
    base_url = "https://example.test"

    def is_configured(self) -> bool:
        return True

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse(
            "success",
            (
                PriceCandidate(
                    title="Mock 商品",
                    url="https://example.test/item",
                    item_price=1000,
                    jan=jan,
                    jan_verified=True,
                    match_type="EXACT_JAN",
                    confidence=1.0,
                ),
            ),
        )


def test_provider_unconfigured_degradation_unified_result_and_mock_success(
    db_session,
    monkeypatch,
):
    for name in (
        "JBA_RAKUTEN_APPLICATION_ID",
        "JBA_RAKUTEN_ACCESS_KEY",
        "JBA_YAHOO_CLIENT_ID",
        "JBA_AMAZON_CREATORS_PUBLIC_KEY",
        "JBA_AMAZON_CREATORS_PRIVATE_KEY",
        "JBA_AMAZON_JP_PARTNER_TAG",
        "JBA_AMAZON_JP_MARKETPLACE",
    ):
        monkeypatch.delenv(name, raising=False)
    for provider in (
        RakutenPriceProvider(),
        YahooShoppingPriceProvider(),
        AmazonCreatorsPriceProvider(),
    ):
        response = provider.search(VALID_JAN, 0.1)
        assert response.status == "unconfigured"
        assert response.error_code == "UNCONFIGURED"
        assert "API未配置" in response.message
        assert response.search_url

    views = provider_status_rows(db_session)
    assert next(row for row in views if row.code == "rakuten").configured is False
    response = run_provider_test(db_session, "mock_success", provider=MockSuccessProvider())
    state = db_session.scalar(
        select(PlatformProviderState).where(
            PlatformProviderState.provider_code == "mock_success"
        )
    )
    unified = response.offers[0].unified("mock_success")
    assert response.status == "success"
    assert state.credentials_valid is True and state.request_count == 1
    assert unified["jan_verified"] is True
    assert unified["link_type"] == "product"
    assert unified["total_price"] == 1000


def test_platform_config_page_never_echoes_full_key(client, monkeypatch):
    test_client, _, _ = client
    secret = "full-secret-never-show"
    monkeypatch.setenv("JBA_RAKUTEN_APPLICATION_ID", "configured-app")
    monkeypatch.setenv("JBA_RAKUTEN_ACCESS_KEY", secret)
    page = test_client.get("/platform-config")
    assert page.status_code == 200
    assert "已配置" in page.text
    assert "applicationIdPresent=True / length=14" in page.text
    assert "accessKeyPresent=True / length=22" in page.text
    assert "测试 Rakuten Item Search" in page.text
    assert secret not in page.text


class RecordingProviderClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.payload[len(self.calls) - 1] if isinstance(self.payload, list) else self.payload
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url, headers=kwargs.get("headers")))


def test_rakuten_referer_config_is_stripped_and_headers_are_shared(monkeypatch):
    monkeypatch.setenv("JBA_RAKUTEN_HTTP_REFERER", "  https://xufu-cp.taile96adb.ts.net:8020  ")
    provider = RakutenPriceProvider()

    assert rakuten_http_referer() == "https://xufu-cp.taile96adb.ts.net:8020/"
    assert provider._rakuten_headers() == {
        "Referer": "https://xufu-cp.taile96adb.ts.net:8020/",
        "User-Agent": "JapanBuyingAgent/1.0",
    }
    assert provider._rakuten_headers({"accessKey": "secret"})["Referer"] == "https://xufu-cp.taile96adb.ts.net:8020/"

    monkeypatch.setenv("JBA_RAKUTEN_HTTP_REFERER", "ftp://example.test/")
    try:
        rakuten_http_referer()
    except ValueError:
        pass
    else:
        raise AssertionError("invalid Rakuten Referer URL accepted")


def test_rakuten_and_yahoo_use_exact_jan_and_parse_required_fields(monkeypatch):
    monkeypatch.setenv("JBA_RAKUTEN_APPLICATION_ID", "configured")
    monkeypatch.setenv("JBA_RAKUTEN_ACCESS_KEY", "header-only-secret")
    monkeypatch.setenv("JBA_RAKUTEN_HTTP_REFERER", "https://xufu-cp.taile96adb.ts.net:8020/")
    monkeypatch.delenv("JBA_RAKUTEN_PRODUCT_SEARCH_ENABLED", raising=False)
    rakuten_client = RecordingProviderClient({"Items": [{"Item": {
            "itemName": f"商品 {VALID_JAN}", "itemUrl": "https://example.test/i", "itemPrice": 990,
            "mediumImageUrls": [{"imageUrl": "https://example.test/i.jpg"}], "availability": 1,
            "postageFlag": 1, "shopName": "楽天店",
    }}]})
    rakuten = RakutenPriceProvider(client=rakuten_client).search(VALID_JAN, 1)
    assert len(rakuten_client.calls) == 1
    item_url, item_call = rakuten_client.calls[0]
    assert "IchibaItem/Search" in item_url
    assert "Product/Search" not in item_url
    assert item_call["params"]["keyword"] == VALID_JAN
    assert item_call["params"]["accessKey"] == "header-only-secret"
    assert item_call["params"]["applicationId"] == "configured"
    assert item_call["headers"]["Referer"] == "https://xufu-cp.taile96adb.ts.net:8020/"
    assert item_call["headers"]["User-Agent"] == "JapanBuyingAgent/1.0"
    assert rakuten.status == "success" and rakuten.http_status == 200
    assert rakuten.diagnostics["endpoint_strategy"] == "item_search_only"
    assert rakuten.diagnostics["rakuten_item_self_check"]["apiVersion"] == "20260701"
    assert rakuten.diagnostics["product_search"]["enabled"] is False
    assert rakuten.diagnostics["item_search"]["result_count"] == 1
    assert (rakuten.offers[0].title, rakuten.offers[0].item_price, rakuten.offers[0].stock_status) == (f"商品 {VALID_JAN}", 990, "in_stock")
    assert rakuten.offers[0].image_url and rakuten.offers[0].url and rakuten.offers[0].jan == VALID_JAN
    assert rakuten.offers[0].match_type == "UNVERIFIED"

    monkeypatch.setenv("JBA_RAKUTEN_PRODUCT_SEARCH_ENABLED", "true")
    product_client = RecordingProviderClient([
        {"products": [{"product": {
            "productName": "商品", "productCode": VALID_JAN, "productUrlPC": "https://example.test/r",
            "mediumImageUrl": "https://example.test/r.jpg", "salesMinPrice": 980, "salesItemCount": 2,
        }}]},
        {"Items": []},
    ])
    with_product = RakutenPriceProvider(client=product_client).search(VALID_JAN, 1)
    assert len(product_client.calls) == 2
    assert "Product/Search" in product_client.calls[0][0]
    assert product_client.calls[0][1]["headers"]["Referer"] == "https://xufu-cp.taile96adb.ts.net:8020/"
    assert product_client.calls[1][1]["headers"]["Referer"] == "https://xufu-cp.taile96adb.ts.net:8020/"
    assert with_product.offers[0].match_type == "EXACT_JAN"

    monkeypatch.setenv("JBA_YAHOO_CLIENT_ID", "configured")
    yahoo_client = RecordingProviderClient({"hits": [{
        "name": "商品", "url": "https://example.test/y", "price": 990, "janCode": VALID_JAN,
        "inStock": True, "exImage": {"url": "https://example.test/y.jpg"},
    }]})
    yahoo = YahooShoppingPriceProvider(client=yahoo_client).search(VALID_JAN, 1)
    _, yahoo_call = yahoo_client.calls[0]
    assert yahoo_call["params"]["jan_code"] == VALID_JAN and yahoo_call["params"]["results"] == 20
    assert yahoo.status == "success" and yahoo.http_status == 200
    assert (yahoo.offers[0].title, yahoo.offers[0].item_price, yahoo.offers[0].stock_status) == ("商品", 990, "in_stock")
    assert yahoo.offers[0].image_url and yahoo.offers[0].url and yahoo.offers[0].jan == VALID_JAN


def test_rakuten_maps_rate_limit_auth_and_empty(monkeypatch):
    monkeypatch.setenv("JBA_RAKUTEN_APPLICATION_ID", "configured")
    monkeypatch.setenv("JBA_RAKUTEN_ACCESS_KEY", "secret")

    class StatusClient:
        def __init__(self, status, payload=None, headers=None):
            self.status = status
            self.payload = payload or {}
            self.headers = headers or {}
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return httpx.Response(
                self.status,
                json=self.payload,
                headers=self.headers,
                request=httpx.Request("GET", url, headers=kwargs.get("headers")),
            )

    rate = RakutenPriceProvider(client=StatusClient(429, headers={"Retry-After": "7"})).search(VALID_JAN, 1)
    assert rate.status == "error" and rate.error_code == "RATE_LIMITED"
    assert rate.diagnostics["cooldown_decision"]["cooldown_seconds"] == 7

    auth = RakutenPriceProvider(client=StatusClient(403)).search(VALID_JAN, 1)
    assert auth.status == "error" and auth.error_code == "AUTH_FAILED"

    empty = RakutenPriceProvider(client=StatusClient(200, {"Items": []})).search(VALID_JAN, 1)
    assert empty.status == "empty" and empty.error_code == "NOT_FOUND"


class ConnectionResultProvider(PriceProvider):
    code = "connection_result"
    display_name = "连接测试"
    base_url = None

    def __init__(self, result):
        self.result = result

    def is_configured(self):
        return True

    def search(self, jan, timeout_seconds):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_provider_connection_distinguishes_empty_auth_and_rate_limit(db_session):
    empty = run_provider_test(db_session, "connection_result", provider=ConnectionResultProvider(
        ProviderResponse("empty", message="无结果", error_code="NOT_FOUND", http_status=200)
    ))
    state = db_session.scalar(select(PlatformProviderState).where(PlatformProviderState.provider_code == "connection_result"))
    assert empty.status == "empty" and state.credentials_valid is True and state.last_success_at is not None

    def status_error(code):
        response = httpx.Response(code, request=httpx.Request("GET", "https://example.test/status"))
        return httpx.HTTPStatusError("status", request=response.request, response=response)

    auth = run_provider_test(db_session, "connection_result", provider=ConnectionResultProvider(status_error(401)))
    assert auth.error_code == "AUTH_FAILED" and auth.http_status == 401 and state.credentials_valid is False
    limited = run_provider_test(db_session, "connection_result", provider=ConnectionResultProvider(status_error(429)))
    assert limited.error_code == "RATE_LIMITED" and limited.http_status == 429
