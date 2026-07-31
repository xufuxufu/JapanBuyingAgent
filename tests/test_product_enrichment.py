from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from PIL import Image
from sqlalchemy import func, select

import app.product_enrichment as enrichment
import app.product_translation_service as translation_service
from app.config import get_deepseek_config
from app.provider_config import DeepSeekDiagnosticProvider
from app.location_service import initialize_default_locations
from app.models import (
    Product, ProductEnrichmentCandidate, ProductEnrichmentTask, PurchaseBatch,
    Receipt, ReceiptBatch, ReceiptItem,
)
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse
from app.product_enrichment import (
    accept_task, download_main_image, ensure_enrichment_task, ensure_receipt_item_tasks,
    process_enrichment_task, translate_candidate,
)
from app.product_identity import format_product_display_name
from app.product_translation_service import (
    MISSING_CN_PLACEHOLDER,
    reset_translation_attempt_cache,
    translate_missing_chinese_names,
    translate_product_chinese_name,
)


VALID_JAN = "4901234567894"
OTHER_JAN = "4570110290418"


@dataclass
class FakeProvider(PriceProvider):
    code: str
    offers: tuple[PriceCandidate, ...]
    display_name: str = "Fake Provider"
    base_url: str | None = "https://example.test"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse("success", self.offers)


def offer(title="测试商品 500ml 型号 AB-123", *, url="https://example.test/item", image_url="https://img.test/main.jpg"):
    return PriceCandidate(
        title=title, url=url, image_url=image_url, seller="测试店", item_price=1000,
        shipping_price=0, shipping_known=True, jan=VALID_JAN, stock_status="in_stock",
        condition="new", listing_type="single", jan_verified=True, match_type="EXACT_JAN",
        raw_data={"brandName": "测试品牌", "manufacturer": "测试厂商", "categoryName": "日用品"},
    )


class JsonResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, json=self.payload, request=httpx.Request("POST", "https://deepseek.test"))
            raise httpx.HTTPStatusError("status", request=response.request, response=response)
        return None

    def json(self):
        return self.payload


class DeepSeekClient:
    def __init__(self, result=None):
        self.result = result or "测试品牌清洁用品500ml AB-123|テスト商品"
        self.calls = 0
        self.last_payload = None

    def post(self, url, headers, json, timeout):
        self.calls += 1
        self.last_payload = json
        content = self.result if isinstance(self.result, str) else __import__("json").dumps(self.result, ensure_ascii=False)
        return JsonResponse({"choices": [{"message": {"content": content}}]})


class RateLimitedDeepSeekClient:
    def post(self, url, headers, json, timeout):
        return JsonResponse({"error": {"message": "rate limited"}}, status_code=429)


class ImageClient:
    def __init__(self, content: bytes | None = None, fail: bool = False):
        self.content = content or image_bytes()
        self.fail = fail

    def get(self, url, timeout, follow_redirects=True):
        request = httpx.Request("GET", url)
        if self.fail:
            raise httpx.TimeoutException("timeout", request=request)
        return httpx.Response(200, content=self.content, headers={"content-type": "image/jpeg"}, request=request)


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 320), "white").save(output, "JPEG")
    return output.getvalue()


def make_item(db, *, jan=VALID_JAN, confirmed=False):
    batch = ReceiptBatch(
        batch_no=f"ENRICH-{id(db)}-{jan}", status="confirmed" if confirmed else "review",
        image_status="ready", gpt_status="reviewed" if confirmed else "json_imported",
    )
    receipt = Receipt(
        batch=batch, raw_store_name="测试店", confirmation_status="confirmed" if confirmed else "pending",
        review_status="reviewed" if confirmed else "pending",
        confirmed_at=datetime.now(timezone.utc) if confirmed else None,
    )
    item = ReceiptItem(
        receipt=receipt, line_no=1, raw_name="テスト商品", recognized_name="测试商品",
        jan_candidate=jan, quantity=1, unit_price=900, discount_amount=0,
        line_total=900, confidence=1, review_status="confirmed" if confirmed else "pending",
        match_status="new_product" if confirmed else "unmatched",
    )
    db.add(batch)
    db.commit()
    return item


def configure_deepseek(monkeypatch, *, auto_create=False):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-only-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-mock")
    monkeypatch.setenv("JBA_AUTO_CREATE_PRODUCT_ENABLED", "true" if auto_create else "false")


def test_new_jan_creates_one_task_and_duplicate_sources_do_not_duplicate(db_session):
    item = make_item(db_session)
    first = ensure_receipt_item_tasks(db_session, [item], "gpt_receipt_json")[0]
    second = ensure_receipt_item_tasks(db_session, [item], "receipt_matching")[0]
    assert first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(ProductEnrichmentTask)) == 1
    assert len(second.sources) == 1


def test_existing_product_does_not_create_task(db_session):
    db_session.add(Product(jan=VALID_JAN, name_cn="人工商品", product_data_confirmed=True, name_locked=True))
    db_session.commit()
    assert ensure_enrichment_task(db_session, VALID_JAN, "manual_jan") is None
    assert db_session.scalar(select(func.count()).select_from(ProductEnrichmentTask)) == 0


def test_disabled_enrichment_still_saves_missing_product_and_manual_names_are_validated(db_session, monkeypatch):
    monkeypatch.setenv("JBA_PRODUCT_ENRICHMENT_ENABLED", "false")
    task = ensure_enrichment_task(db_session, VALID_JAN, "price_lookup")
    process_enrichment_task(db_session, task)
    product = db_session.scalar(select(Product).where(Product.jan == VALID_JAN))
    assert product is not None
    assert product.display_name == f"{VALID_JAN}|缺商品"
    assert product.status == "new_pending_completion"

    product = accept_task(
        db_session, task.id,
        name_cn=("中文|名称" * 30), name_ja=("日本語|商品" * 30),
    )
    assert product.status == "new_pending_review"
    assert product.display_name.count("|") == 1
    assert "·" in product.name_cn and "·" in product.name_ja
    assert len(f"{product.name_cn}|{product.name_ja}") <= 128


def test_deepseek_failure_keeps_japanese_name_without_placeholder_cn(db_session, monkeypatch):
    configure_deepseek(monkeypatch)
    task = ensure_enrichment_task(db_session, VALID_JAN, "price_lookup")
    process_enrichment_task(
        db_session,
        task,
        providers=[FakeProvider("ja", (offer("テスト日本語商品 500ml"),))],
        deepseek_client=DeepSeekClient(result="broken response without pipe"),
        image_client=ImageClient(fail=True),
    )
    product = db_session.scalar(select(Product).where(Product.jan == VALID_JAN))
    assert product.name_ja == "テスト日本語商品 500ml"
    assert product.name_cn is None
    assert product.status == "new_pending_completion"
    assert MISSING_CN_PLACEHOLDER not in (product.display_name or "")


def test_missing_chinese_name_product_generates_name_and_review_status(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    product = Product(
        jan=VALID_JAN, name_cn=None, name_ja="テスト商品　　500ml", status="new_pending_completion",
        display_name="テスト商品　　500ml", source="product_enrichment",
    )
    db_session.add(product)
    db_session.commit()
    result = translate_product_chinese_name(db_session, product, client=DeepSeekClient("测试商品　　500ml|テスト商品 500ml"), force=True)
    db_session.commit()
    assert result.status == "success"
    assert product.name_cn == "测试商品 500ml"
    assert product.display_name == "测试商品 500ml|テスト商品 500ml"
    assert product.status == "new_pending_review"
    assert len(product.display_name) <= 128


def test_deepseek_config_is_shared_by_self_check_and_translation(monkeypatch):
    monkeypatch.delenv("JBA_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", ' "mock-only-key" ')
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.test/v1")
    config = get_deepseek_config()
    settings = enrichment.get_enrichment_settings()
    assert config.api_key == "mock-only-key"
    assert config.api_key_variable == "DEEPSEEK_API_KEY"
    assert settings.deepseek_api_key == config.api_key
    assert settings.deepseek_enabled
    assert DeepSeekDiagnosticProvider().is_configured()


def test_deepseek_failure_still_saves_product_and_does_not_repeat(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    product = Product(name_ja="失敗商品", status="new_pending_completion", display_name="失敗商品")
    db_session.add(product)
    db_session.commit()
    client = DeepSeekClient("invalid")
    first = translate_product_chinese_name(db_session, product, client=client)
    second = translate_product_chinese_name(db_session, product, client=client)
    assert first.status == "failed"
    assert second.status == "skipped"
    assert product.name_cn is None and product.name_ja == "失敗商品"


def test_deepseek_failure_does_not_clear_existing_name(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    product = Product(name_cn="旧中文", name_ja="失敗商品", status="new_pending_review", display_name="旧中文|失敗商品")
    db_session.add(product)
    db_session.commit()
    result = translate_product_chinese_name(
        db_session, product, client=DeepSeekClient("invalid"), force=True, overwrite_existing=True,
    )
    assert result.status == "failed"
    assert product.name_cn == "旧中文"
    assert product.name_ja == "失敗商品"
    assert product.display_name == "旧中文|失敗商品"


def test_existing_chinese_name_is_not_translated_again(db_session, monkeypatch):
    configure_deepseek(monkeypatch)
    product = Product(name_cn="已有中文", name_ja="既存日本語", status="new_pending_review")
    db_session.add(product)
    db_session.commit()
    client = DeepSeekClient("新中文|既存日本語")
    result = translate_product_chinese_name(db_session, product, client=client, force=True)
    assert result.status == "skipped"
    assert client.calls == 0
    assert product.name_cn == "已有中文"


def test_placeholder_chinese_name_can_be_retranslated(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    product = Product(name_cn="中文名待补全", name_ja="再翻訳商品", status="new_pending_completion")
    db_session.add(product)
    db_session.commit()
    result = translate_product_chinese_name(db_session, product, client=DeepSeekClient("重翻中文|再翻訳商品"), force=True)
    assert result.status == "success"
    assert product.name_cn == "重翻中文"
    assert product.name_ja == "再翻訳商品"


def test_batch_translation_success_and_imported_products_are_skipped(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    pending = Product(name_ja="未翻訳商品", status="new_pending_completion", display_name="未翻訳商品")
    imported = Product(name_ja="已导入商品", status="qinsi_product_imported", display_name="已导入商品")
    db_session.add_all([pending, imported])
    db_session.commit()
    result = translate_missing_chinese_names(db_session, client=DeepSeekClient("批量中文|未翻訳商品"), force=True)
    db_session.refresh(pending)
    db_session.refresh(imported)
    assert result.candidate_count == 1
    assert result.success_count == 1
    assert pending.name_cn == "批量中文"
    assert pending.status == "new_pending_review"
    assert imported.name_cn is None


def test_batch_translation_stops_on_deepseek_rate_limit(db_session, monkeypatch):
    reset_translation_attempt_cache()
    configure_deepseek(monkeypatch)
    db_session.add_all([
        Product(name_ja="限流商品A", status="new_pending_completion", display_name="限流商品A"),
        Product(name_ja="限流商品B", status="new_pending_completion", display_name="限流商品B"),
    ])
    db_session.commit()
    result = translate_missing_chinese_names(db_session, client=RateLimitedDeepSeekClient(), force=True)
    assert result.failed_count == 1
    assert result.stopped_reason == "DeepSeek限流"


def test_provider_spec_conflict_never_auto_creates(db_session, monkeypatch, tmp_path):
    configure_deepseek(monkeypatch, auto_create=True)
    monkeypatch.setattr(enrichment, "PRODUCT_IMAGE_DIR", tmp_path / "products")
    item = make_item(db_session)
    task = ensure_receipt_item_tasks(db_session, [item], "receipt_matching")[0]
    provider = FakeProvider("conflict", (
        offer("测试商品 100ml", url="https://example.test/a"),
        offer("测试商品 200ml", url="https://example.test/b"),
    ))
    process_enrichment_task(
        db_session, task, providers=[provider], deepseek_client=DeepSeekClient(), image_client=ImageClient(),
    )
    assert task.status == "completed_with_warnings"
    assert "规格冲突" in (task.warnings_json or "")
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1


def test_deepseek_missing_is_safe_and_valid_json_builds_display_name(db_session, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    task = ProductEnrichmentTask(jan=VALID_JAN, trigger_source="test")
    db_session.add(task)
    db_session.flush()
    candidate = ProductEnrichmentCandidate(
        task_id=task.id, jan=VALID_JAN, name_ja="長い商品名 500ml AB-123", source_url="https://example.test",
        platform="fake", fetched_at=datetime.now(timezone.utc), model_number="AB-123", capacity="500ml",
    )
    db_session.add(candidate)
    db_session.commit()
    assert translate_candidate(db_session, task, candidate, client=DeepSeekClient()) is None
    assert task.deepseek_status == "pending_configuration"

    configure_deepseek(monkeypatch)
    result = translate_candidate(db_session, task, candidate, client=DeepSeekClient())
    assert result and "500ml" in result.name_cn and "AB-123" in result.name_cn
    display = format_product_display_name("品牌商品" * 30 + " 500ml AB-123", "日本商品" * 30 + " 500ml AB-123")
    assert len(display) <= 128 and "500ml" in display and "AB-123" in display and "|" in display


def test_same_jan_and_japanese_name_reuses_deepseek_cache(db_session, monkeypatch):
    configure_deepseek(monkeypatch)
    task = ProductEnrichmentTask(jan=VALID_JAN, trigger_source="test")
    db_session.add(task)
    db_session.flush()
    candidate = ProductEnrichmentCandidate(
        task_id=task.id, jan=VALID_JAN, name_ja="テスト商品 500ml", source_url="https://example.test/cache",
        platform="fake", fetched_at=datetime.now(timezone.utc), capacity="500ml",
    )
    db_session.add(candidate)
    db_session.commit()
    client = DeepSeekClient()
    assert translate_candidate(db_session, task, candidate, client=client)
    db_session.commit()
    task.deepseek_status = "pending"
    assert translate_candidate(db_session, task, candidate, client=client)
    assert client.calls == 1 and task.deepseek_status == "completed_cached"


def test_ai_json_requires_schema_version_and_rejects_unsourced_fields(db_session, monkeypatch):
    configure_deepseek(monkeypatch)
    task = ProductEnrichmentTask(jan=VALID_JAN, trigger_source="test")
    db_session.add(task)
    db_session.flush()
    candidate = ProductEnrichmentCandidate(
        task_id=task.id, jan=VALID_JAN, name_ja="テスト商品", source_url="https://example.test/item",
        platform="mock", fetched_at=datetime.now(timezone.utc), score=1, selected=True,
    )
    db_session.add(candidate)
    db_session.commit()
    invalid = DeepSeekClient(result={
        "schema_version": "1.0",
        "name_cn": "测试商品",
        "name_ja": "テスト商品",
        "brand_cn": "",
        "category_cn": "",
        "confidence": 1,
        "warnings": [],
        "price": 100,
    })
    assert translate_candidate(db_session, task, candidate, client=invalid) is None
    assert task.deepseek_status == "failed"


def test_main_image_download_success_and_failure_keep_remote_url(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(enrichment, "PRODUCT_IMAGE_DIR", tmp_path / "products")
    task = ProductEnrichmentTask(jan=VALID_JAN, trigger_source="test")
    db_session.add(task)
    db_session.flush()
    candidate = ProductEnrichmentCandidate(
        task_id=task.id, jan=VALID_JAN, name_ja="商品", image_url="https://img.test/main.jpg",
        source_url="https://example.test", platform="fake", fetched_at=datetime.now(timezone.utc),
    )
    db_session.add(candidate)
    db_session.commit()
    downloaded = download_main_image(task, candidate, client=ImageClient())
    assert downloaded and (tmp_path / "products" / (downloaded["path"].split("/")[-1])).is_file()
    assert len(downloaded["hash"]) == 64 and task.image_status == "completed"
    assert download_main_image(task, candidate, client=ImageClient(fail=True)) is None
    assert task.image_status == "failed_remote_available" and candidate.image_url


def test_high_confidence_auto_create_links_receipt_and_purchase(db_session, monkeypatch, tmp_path):
    initialize_default_locations(db_session)
    configure_deepseek(monkeypatch, auto_create=True)
    monkeypatch.setattr(enrichment, "PRODUCT_IMAGE_DIR", tmp_path / "products")
    item = make_item(db_session, confirmed=True)
    task = ensure_receipt_item_tasks(db_session, [item], "receipt_confirmation")[0]
    process_enrichment_task(
        db_session, task, providers=[FakeProvider("high", (offer(),))],
        deepseek_client=DeepSeekClient(), image_client=ImageClient(),
    )
    db_session.refresh(item)
    product = db_session.get(Product, task.product_id)
    assert task.status in {"completed", "completed_with_warnings"}
    assert product and product.jan == VALID_JAN and product.internal_sku and product.display_name == f"{product.name_cn}|{product.name_ja}"
    assert product.main_image_path and item.product_id == product.id and item.match_method == "auto_enrichment"
    assert db_session.scalar(select(func.count()).select_from(PurchaseBatch)) == 1


def test_new_product_auto_translation_uses_saved_japanese_name(db_session, monkeypatch, tmp_path):
    configure_deepseek(monkeypatch)
    monkeypatch.setattr(enrichment, "PRODUCT_IMAGE_DIR", tmp_path / "products")
    task = ensure_enrichment_task(db_session, VALID_JAN, "price_lookup")
    process_enrichment_task(
        db_session,
        task,
        providers=[FakeProvider("ja", (offer("保存済み日本語商品 300ml"),))],
        deepseek_client=DeepSeekClient("自动中文商品300ml|モデルが返した日文"),
        image_client=ImageClient(fail=True),
    )
    product = db_session.scalar(select(Product).where(Product.jan == VALID_JAN))
    assert product.name_cn == "自动中文商品300ml"
    assert product.name_ja == "保存済み日本語商品 300ml"
    assert product.display_name == "自动中文商品300ml|保存済み日本語商品 300ml"


def test_manual_confirmed_product_is_never_overwritten(db_session):
    item = make_item(db_session)
    task = ensure_receipt_item_tasks(db_session, [item], "manual_jan")[0]
    product = Product(
        jan=VALID_JAN, name_cn="人工中文名", name_ja="人工日本語", main_image_source_url="https://manual.test/a.jpg",
        product_data_confirmed=True, name_locked=True, main_image_locked=True,
    )
    db_session.add(product)
    db_session.commit()
    process_enrichment_task(db_session, task)
    db_session.refresh(product)
    assert (product.name_cn, product.name_ja, product.main_image_source_url) == (
        "人工中文名", "人工日本語", "https://manual.test/a.jpg",
    )
    assert task.product_id == product.id and item.product_id == product.id


def test_enrichment_list_detail_and_product_detail_return_200(client):
    http, db, _ = client
    item = make_item(db, jan=OTHER_JAN)
    task = ensure_receipt_item_tasks(db, [item], "manual_jan")[0]
    product = Product(jan=VALID_JAN, name_cn="中文", name_ja="日本語", display_name="中文|日本語")
    db.add(product)
    db.commit()
    assert http.get("/product-enrichment").status_code == 200
    assert http.get(f"/product-enrichment/{task.id}").status_code == 200
    assert http.get(f"/products/{product.id}").status_code == 200


def test_products_page_does_not_display_missing_cn_placeholder_as_name(client):
    http, db, _ = client
    product = Product(
        name_cn=MISSING_CN_PLACEHOLDER,
        name_ja="表示用日本語名",
        display_name=f"{MISSING_CN_PLACEHOLDER}|表示用日本語名",
        status="new_pending_completion",
    )
    db.add(product)
    db.commit()
    page = http.get("/products")
    assert page.status_code == 200
    assert "表示用日本語名" in page.text
    assert MISSING_CN_PLACEHOLDER not in page.text
    assert "缺中文名" in page.text
