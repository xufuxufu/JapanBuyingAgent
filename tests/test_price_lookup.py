from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Marketplace, PriceLookupHistory, PriceProviderAttempt, PriceSearchRun, Product, ProductBarcode, ProductOffer, ProductOperationLog
from app.price_providers import (
    LocalQinsiPriceProvider,
    PriceCandidate,
    PriceProvider,
    ProviderResponse,
    RakutenPriceProvider,
    WebFallbackPriceProvider,
    YahooShoppingPriceProvider,
)
from app.price_service import _search_provider_coalesced, build_lookup_view, online_reference_price_from_offers, query_prices
from app.product_identity import format_product_display_name, normalize_product_name
from app.schemas import PriceLookupInput


VALID_JAN = "4901234567894"
OTHER_JAN = "4901234567887"


@dataclass
class FakeProvider(PriceProvider):
    code: str
    response: ProviderResponse | None = None
    error: Exception | None = None
    calls: int = 0
    display_name: str = "Fake"
    base_url: str | None = "https://example.test/"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return self.response or ProviderResponse("empty")


class SlowProvider(PriceProvider):
    code = "slow_coalesced"
    display_name = "Slow Coalesced"
    base_url = "https://example.test"

    def __init__(self):
        self.calls = 0

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        self.calls += 1
        time.sleep(0.08)
        return ProviderResponse("empty", message=jan, error_code="NOT_FOUND")


class WebFallbackClient:
    def __init__(self, page_text: str):
        self.page_text = page_text
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            html = '<a class="result__a" href="https://hands.net/goods/abc">HANDS</a>'
            return httpx.Response(200, text=html, request=httpx.Request("GET", str(url)))
        return httpx.Response(
            200,
            text=self.page_text,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", str(url)),
        )


class OfficialFallbackClient:
    def __init__(self, page_text: str, url: str):
        self.page_text = page_text
        self.url = url
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            html = f'<a class="result__a" href="{self.url}">公式商品</a>'
            return httpx.Response(200, text=html, request=httpx.Request("GET", str(url)))
        return httpx.Response(
            200,
            text=self.page_text,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", self.url),
        )


def offer(price: int, *, title: str = "测试商品 100ml", jan: str | None = VALID_JAN, shipping: int = 0, **kwargs):
    stock_status = kwargs.pop("stock_status", "in_stock")
    return PriceCandidate(
        title=title, url=f"https://example.test/{price}", image_url=f"https://img.test/{price}.jpg",
        seller="测试店", item_price=price, shipping_price=shipping, jan=jan, stock_status=stock_status,
        jan_verified=kwargs.pop("jan_verified", jan == VALID_JAN), **kwargs,
    )


def test_provider_inflight_same_jan_is_coalesced():
    provider = SlowProvider()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(_search_provider_coalesced(provider, VALID_JAN, 1)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert provider.calls == 1
    assert [result.status for result in results] == ["empty", "empty"]


def test_jan_validation_and_product_name_rules():
    assert PriceLookupInput(jan=VALID_JAN).jan == VALID_JAN
    for invalid in ("", "123", "4901234567895", "49012ABC67894"):
        try:
            PriceLookupInput(jan=invalid)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"invalid JAN accepted: {invalid}")
    assert format_product_display_name("中文", "日本語") == "中文|日本語"
    try:
        normalize_product_name("名" * 129, "中文名")
    except ValueError as exc:
        assert "128" in str(exc)
    else:
        raise AssertionError("overlong product name accepted")


def test_existing_product_match_totals_history_and_comparison(db_session):
    product = Product(jan=VALID_JAN, name_cn="测试商品", name_ja="テスト商品", specification="100ml", purchase_price=1400)
    db_session.add(product)
    db_session.commit()
    provider = FakeProvider("success", ProviderResponse("success", (offer(1000, shipping=200),)))

    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN, current_store_price=1100), [provider])

    assert view.product.id == product.id
    assert view.product_display_name == "测试商品|テスト商品"
    # 商品主数据采购价不是已确认小票，不能冒充上一次采购价。
    assert view.recent_purchase_price is None
    assert view.online_min_price == 1000
    assert view.difference == 100 and view.comparison_status == "online_cheaper"
    saved = db_session.scalar(select(ProductOffer))
    assert (saved.item_price, saved.shipping_price, saved.total_price) == (1000, 200, 1200)
    assert saved.image_url == "https://img.test/1000.jpg"
    assert db_session.scalar(select(func.count()).select_from(PriceLookupHistory)) == 1


def test_new_jan_creates_pending_product(db_session):
    provider = FakeProvider("new_candidate", ProviderResponse("success", (offer(900),)))
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    assert view.product is not None and view.product.status == "new_pending_review"
    assert view.product.purchase_price == 900 and view.product.sale_price == 900
    assert view.run.is_new_candidate is False
    assert db_session.scalar(select(func.count()).select_from(Product)) == 1
    assert view.trusted_offers[0].product_id == view.product.id


def product_offer(price: int, *, item_id: int, shipping: int = 0, trusted: bool = True):
    return ProductOffer(
        id=item_id,
        marketplace=Marketplace(code=f"m{item_id}", name=f"M{item_id}"),
        item_price=price,
        shipping_price=shipping,
        shipping_known=True,
        total_price=price + shipping,
        url=f"https://example.test/{item_id}",
        is_trusted=trusted,
        stock_status="in_stock",
    )


def test_online_reference_price_average_uses_lowest_valid_one_two_three():
    one, offers = online_reference_price_from_offers((product_offer(1000, item_id=1),))
    assert one == 1000 and len(offers) == 1
    two, offers = online_reference_price_from_offers((
        product_offer(1000, item_id=1),
        product_offer(1200, item_id=2, shipping=300),
    ))
    assert two == 1250 and len(offers) == 2
    three, offers = online_reference_price_from_offers((
        product_offer(1000, item_id=1),
        product_offer(1200, item_id=2, shipping=300),
        product_offer(2000, item_id=3),
        product_offer(100, item_id=4, trusted=False),
        product_offer(9000, item_id=5),
    ))
    assert three == 1500 and [offer.id for offer in offers] == [1, 2, 3]


def test_new_product_reference_price_logs_lowest_three_average(db_session):
    provider = FakeProvider("avg_provider", ProviderResponse("success", (
        offer(1000),
        offer(1200, shipping=300),
        offer(2000),
        offer(9000),
    )))

    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    log = db_session.scalar(select(ProductOperationLog).where(ProductOperationLog.product_id == view.product.id))

    assert view.product.purchase_price == 1500 and view.product.sale_price == 1500
    assert log is not None
    assert "online_lowest_3_average" in (log.after_json or "")
    assert "offer_count\": 3" in (log.after_json or "")


def test_provider_empty_timeout_error_and_unconfigured_are_saved(db_session, monkeypatch):
    monkeypatch.delenv("JBA_RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("JBA_RAKUTEN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("JBA_YAHOO_CLIENT_ID", raising=False)
    providers = [
        FakeProvider("empty_provider", ProviderResponse("empty", message="空结果")),
        FakeProvider("timeout_provider", error=TimeoutError()),
        FakeProvider("error_provider", error=RuntimeError("boom")),
        RakutenPriceProvider(), YahooShoppingPriceProvider(),
    ]
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers)
    statuses = {item.provider_code: item.status for item in view.attempts}
    assert statuses == {
        "empty_provider": "empty", "timeout_provider": "timeout", "error_provider": "error",
        "rakuten": "unconfigured", "yahoo_shopping": "unconfigured",
    }


def test_web_fallback_runs_after_api_empty_and_enriches_name_image_price(db_session, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("JBA_PRODUCT_IMAGE_DOWNLOAD_ENABLED", "false")
    page = f"""
    <html><head>
      <meta property="og:title" content="HANDS Web 商品 300ml">
      <meta property="og:image" content="/images/item.jpg">
    </head><body>JANコード {VALID_JAN} 税込価格 ¥1,234 在庫あり</body></html>
    """
    web = WebFallbackPriceProvider(client=WebFallbackClient(page))
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [
        FakeProvider("api_empty", ProviderResponse("empty", message="no result")),
        web,
    ])

    web_offer = next(item for item in view.result_offers if item.marketplace.code == "web_fallback")
    product = db_session.scalar(select(Product).where(Product.jan == VALID_JAN))

    assert web_offer.title == "HANDS Web 商品 300ml"
    assert web_offer.image_url == "https://hands.net/images/item.jpg"
    assert web_offer.item_price == 1234
    assert web_offer.jan_match_status == "exact"
    assert product is not None and product.name_ja == "HANDS Web 商品 300ml"
    assert product.main_image_source_url == "https://hands.net/images/item.jpg"


def test_official_page_parser_prefers_product_name_over_store_name_and_keeps_source(db_session, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("JBA_PRODUCT_IMAGE_DOWNLOAD_ENABLED", "false")
    jan = "4960919412621"
    url = "https://store.toei-anim.co.jp/shop/g/gDIGI-412621/"
    page = f"""
    <html><head>
      <title>東映アニメーションオフィシャルストア</title>
      <meta property="og:title" content="東映アニメーションオフィシャルストア">
      <meta property="og:site_name" content="東映アニメーションオフィシャルストア">
      <meta property="og:image" content="https://store.toei-anim.co.jp/img/usr/img-ogp.jpg">
      <script type="application/ld+json">{{
        "@context":"https://schema.org","@type":"Product",
        "name":"【デジモンテイマーズ】デジデジおてだま（ジェンリャ）【Limited Base】",
        "gtin13":"{jan}",
        "brand":{{"name":"東映アニメーション"}},
        "offers":{{"@type":"Offer","price":"990","priceCurrency":"JPY"}}
      }}</script>
    </head><body>
      <h1>【デジモンテイマーズ】デジデジおてだま（ジェンリャ）【Limited Base】</h1>
      <img src="/img/goods/L/DIGI-412621-l.jpg">
      JANコード {jan} サイズ W約60mm × H約60mm × D約80mm
    </body></html>
    """
    view = query_prices(db_session, PriceLookupInput(jan=jan), [
        FakeProvider("api_empty_official", ProviderResponse("empty", message="no result")),
        WebFallbackPriceProvider(client=OfficialFallbackClient(page, url)),
    ])

    offer = next(item for item in view.result_offers if item.marketplace.code == "web_fallback")
    product = db_session.scalar(select(Product).where(Product.jan == jan))
    candidate = product.price_search_runs[-1].offers[0]

    assert offer.title == "【デジモンテイマーズ】デジデジおてだま（ジェンリャ）【Limited Base】"
    assert offer.item_price == 990
    assert offer.image_url == "https://store.toei-anim.co.jp/img/goods/L/DIGI-412621-l.jpg"
    assert product.name_ja == offer.title
    assert product.purchase_price == 990 and product.sale_price == 990
    assert product.width_mm == 60 and product.height_mm == 60 and product.depth_mm == 80
    assert "source_url" in (candidate.raw_data_json or "") and url in candidate.raw_data_json


def test_web_fallback_unknown_price_does_not_display_or_store_zero(db_session, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("JBA_PRODUCT_IMAGE_DOWNLOAD_ENABLED", "false")
    jan = "8800366242654"
    page = f"""
    <html><head><meta property="og:title" content="Loft 商品 100ml"><meta property="og:image" content="/p.jpg"></head>
    <body><h1>Loft 商品 100ml</h1>JAN {jan} サイズ 100ml 15,000円以上購入で送料無料</body></html>
    """
    view = query_prices(db_session, PriceLookupInput(jan=jan), [
        WebFallbackPriceProvider(client=OfficialFallbackClient(page, "https://www.loft.co.jp/store/g/g8800366242654/")),
    ])
    offer = next(item for item in view.result_offers if item.marketplace.code == "web_fallback")
    product = db_session.scalar(select(Product).where(Product.jan == jan))

    assert offer.item_price == 0
    assert offer.display_price is None and offer.display_price_text == ""
    assert product.purchase_price is None and product.sale_price is None


def test_web_fallback_rejects_page_without_matching_jan(db_session, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("JBA_PRODUCT_IMAGE_DOWNLOAD_ENABLED", "false")
    page = f"<html><head><title>別商品</title></head><body>JAN {OTHER_JAN} ¥999</body></html>"
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [
        FakeProvider("api_empty_conflict", ProviderResponse("empty", message="no result")),
        WebFallbackPriceProvider(client=WebFallbackClient(page)),
    ])

    assert all(item.marketplace.code != "web_fallback" for item in view.result_offers)
    assert view.run.provider_summary_json and "web_fallback" in view.run.provider_summary_json


def test_sort_by_item_price_and_flag_untrusted_offers(db_session):
    product = Product(jan=VALID_JAN, name_cn="乳液", name_ja="ローション", specification="100ml")
    db_session.add(product)
    db_session.commit()
    candidates = (
        offer(900), offer(700), offer(800), offer(600),
        offer(100, jan=OTHER_JAN),
        offer(110, condition="used"),
        offer(120, stock_status="out_of_stock"),
        offer(130, title="测试商品 200ml"),
        offer(140, title="定期購入 测试商品 100ml", listing_type="subscription"),
        offer(150, shipping_known=False),
    )
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [FakeProvider("quality", ProviderResponse("success", candidates))])
    assert [item.item_price for item in view.trusted_offers] == [150, 600, 700, 800, 900]
    assert view.incomplete_offers == ()
    assert view.trusted_offers[0].shipping_known is False
    reasons = "；".join(item.exclusion_reason or "" for item in view.flagged_offers)
    for text in ("JAN 不一致", "二手商品", "缺货", "数量或容量疑似不同", "定期购买价格"):
        assert text in reasons
    assert "运费未知" not in reasons


def test_result_sort_uses_reliable_pack_unit_price_without_shipping(db_session):
    candidates = (
        offer(2000, title="测试商品 100ml 10個"),
        offer(350, title="测试商品 100ml"),
        offer(2970, title="测试商品 100ml 10个装", raw_data={"pack_quantity": 10}),
        offer(310, title="测试商品 100ml", shipping=9999, shipping_known=False),
        offer(100, title="测试商品 100ml", stock_status="out_of_stock"),
    )
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [FakeProvider("pack_sort", ProviderResponse("success", candidates))])

    assert [item.item_price for item in view.result_offers] == [2970, 310, 350, 2000, 100]
    pack_offer = view.result_offers[0]
    assert pack_offer.pack_quantity == 10
    assert pack_offer.normalized_unit_price == 297
    assert pack_offer.display_price_text == "297"
    unreliable = next(item for item in view.result_offers if item.item_price == 2000)
    assert unreliable.pack_quantity is None
    assert unreliable.normalized_unit_price is None
    assert unreliable.display_price == 2000
    assert view.result_offers[-1].stock_label == "无货"


def test_cache_reuses_run_but_saves_each_lookup_history(db_session):
    provider = FakeProvider("cache", ProviderResponse("success", (offer(777),)))
    first = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    second = query_prices(db_session, PriceLookupInput(jan=VALID_JAN, current_store_price=800), [provider])
    assert provider.calls == 1
    assert first.run.id == second.run.id
    assert second.history.cache_hit is True
    assert db_session.scalar(select(func.count()).select_from(PriceSearchRun)) == 1
    assert db_session.scalar(select(func.count()).select_from(PriceLookupHistory)) == 2
    assert build_lookup_view(db_session, second.history.id).comparison_status == "online_cheaper"


def test_qinsi_derived_jan_rebinds_cached_lookup_and_local_provider(db_session):
    provider = FakeProvider("cache_before_qinsi", ProviderResponse("success", (offer(777),)))
    first = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    assert first.run.is_new_candidate is False
    created = first.product

    product = Product(
        qinsi_product_code=f"/{VALID_JAN}",
        name_cn="秦丝斜杠货号商品",
        purchase_price=650,
    )
    db_session.add(product)
    db_session.flush()
    db_session.delete(created)
    db_session.flush()
    db_session.add(ProductBarcode(product_id=product.id, barcode=VALID_JAN, source_system="manual"))
    db_session.commit()
    db_session.expire_all()

    second = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    local_response = LocalQinsiPriceProvider(db_session).search(VALID_JAN, 1)

    assert second.history.cache_hit is True
    assert second.product.id == product.id
    assert second.run.is_new_candidate is False
    assert provider.calls == 1
    assert local_response.status == "success"
    assert local_response.offers[0].url == f"/products/{product.id}"


def test_scan_and_result_pages_and_missing_config_do_not_500(client, monkeypatch):
    test_client, db_session, _ = client
    monkeypatch.delenv("JBA_RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("JBA_RAKUTEN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("JBA_YAHOO_CLIENT_ID", raising=False)
    scan = test_client.get("/price-check")
    assert scan.status_code == 200
    assert "UnifiedJanScanner" in scan.text and "扫码查价" in scan.text
    assert "Rakuten出口IP" not in scan.text
    assert 'name="force_refresh" type="checkbox" value="true" checked' in scan.text
    assert "停止扫码" in scan.text and 'id="stopScan"' not in scan.text
    invalid = test_client.post("/price-check", data={"jan": "123"})
    assert invalid.status_code == 422 and "JAN" in invalid.text
    response = test_client.post("/price-check", data={"jan": VALID_JAN, "current_store_price": "1000"}, follow_redirects=False)
    assert response.status_code == 303
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    assert "本地已有商品" in result.text and "当前按商品价格排序，未计入配送费。" in result.text
    assert test_client.get("/health").status_code == 200
    assert db_session.scalar(select(func.count()).select_from(PriceProviderAttempt)) == 5
