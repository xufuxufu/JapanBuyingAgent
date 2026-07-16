from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import PriceLookupHistory, PriceProviderAttempt, PriceSearchRun, Product, ProductOffer
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse, RakutenPriceProvider, YahooShoppingPriceProvider
from app.price_service import build_lookup_view, query_prices
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


def offer(price: int, *, title: str = "测试商品 100ml", jan: str | None = VALID_JAN, shipping: int = 0, **kwargs):
    stock_status = kwargs.pop("stock_status", "in_stock")
    return PriceCandidate(
        title=title, url=f"https://example.test/{price}", image_url=f"https://img.test/{price}.jpg",
        seller="测试店", item_price=price, shipping_price=shipping, jan=jan, stock_status=stock_status, **kwargs,
    )


def test_jan_validation_and_product_name_rules():
    assert PriceLookupInput(jan=VALID_JAN).jan == VALID_JAN
    for invalid in ("", "123", "4901234567895", "49012ABC67894"):
        try:
            PriceLookupInput(jan=invalid)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"invalid JAN accepted: {invalid}")
    assert format_product_display_name("中文", "日本語") == "中文｜日本語"
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
    assert view.product_display_name == "测试商品｜テスト商品"
    assert view.recent_purchase_price == 1400
    assert view.online_min_price == 1200
    assert view.difference == -100 and view.comparison_status == "store_cheaper"
    saved = db_session.scalar(select(ProductOffer))
    assert (saved.item_price, saved.shipping_price, saved.total_price) == (1000, 200, 1200)
    assert saved.image_url == "https://img.test/1000.jpg"
    assert db_session.scalar(select(func.count()).select_from(PriceLookupHistory)) == 1


def test_new_jan_is_candidate_without_creating_product(db_session):
    provider = FakeProvider("new_candidate", ProviderResponse("success", (offer(900),)))
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    assert view.product is None and view.run.is_new_candidate is True
    assert db_session.scalar(select(func.count()).select_from(Product)) == 0
    assert view.trusted_offers[0].product_id is None


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


def test_sort_top_three_and_flag_untrusted_offers(db_session):
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
    assert [item.total_price for item in view.trusted_offers] == [600, 700, 800]
    reasons = "；".join(item.exclusion_reason or "" for item in view.flagged_offers)
    for text in ("JAN 不一致", "二手商品", "缺货", "数量或容量疑似不同", "定期购买价格", "运费未知"):
        assert text in reasons


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


def test_scan_and_result_pages_and_missing_config_do_not_500(client, monkeypatch):
    test_client, db_session, _ = client
    monkeypatch.delenv("JBA_RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("JBA_RAKUTEN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("JBA_YAHOO_CLIENT_ID", raising=False)
    scan = test_client.get("/price-check")
    assert scan.status_code == 200
    assert "BarcodeDetector" in scan.text and "扫码查价" in scan.text
    invalid = test_client.post("/price-check", data={"jan": "123"})
    assert invalid.status_code == 422 and "JAN" in invalid.text
    response = test_client.post("/price-check", data={"jan": VALID_JAN, "current_store_price": "1000"}, follow_redirects=False)
    assert response.status_code == 303
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    assert "新品候选" in result.text and "当前查询最低价" in result.text and "未配置" in result.text
    assert db_session.scalar(select(func.count()).select_from(PriceProviderAttempt)) == 3
