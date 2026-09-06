from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
import pytest
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
import app.price_service as price_service
from app.price_service import _search_provider_coalesced, build_lookup_view, online_reference_price_from_offers, query_prices, update_store_price
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
    # Neither side may fabricate a "中文名待补"/"日文名待补" placeholder --
    # a missing name means the other name (or nothing) comes back, never a
    # sentinel string leaking into UI/search text.
    assert format_product_display_name(None, "日本語") == "日本語"
    assert format_product_display_name("中文", None) == "中文"
    assert format_product_display_name(None, None) is None
    assert format_product_display_name("", "") is None
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
    # No deepseek_client is injected here, so DeepSeek stays unconfigured in this
    # hermetic test run and the offer title never becomes a chosen_cn; the product
    # still needs a human-completed Chinese name, hence new_pending_completion.
    assert view.product is not None and view.product.status == "new_pending_completion"
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


def test_build_engine_configures_wal_and_generous_busy_timeout(tmp_path):
    from sqlalchemy import text

    from app.db import build_engine

    url = f"sqlite:///{(tmp_path / 'pragma_check.sqlite3').as_posix()}"
    engine = build_engine(url)
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar()
    assert journal_mode == "wal"
    assert busy_timeout >= 15000


def test_sqlite_busy_timeout_avoids_locked_error_under_concurrent_writers(tmp_path):
    # Reproduces the real in-store 500: the background enrichment task for a
    # brand-new JAN opens its own engine/session (process_price_lookup_enrichment)
    # and can still be mid-transaction when the next scan's foreground request
    # tries to write to the same sqlite file. Without WAL + busy_timeout this
    # raised "database is locked" immediately instead of waiting briefly.
    from sqlalchemy.orm import Session as OrmSession

    from app.db import Base, build_engine

    db_path = tmp_path / "concurrent.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    setup_engine = build_engine(url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    writer_engine = build_engine(url)
    other_engine = build_engine(url)
    errors: list[Exception] = []

    def hold_write_lock():
        with OrmSession(writer_engine) as session:
            session.add(Marketplace(code="holder", name="Holder", active=True))
            session.flush()
            time.sleep(0.5)
            session.commit()

    def concurrent_write():
        try:
            time.sleep(0.1)
            with OrmSession(other_engine) as session:
                session.add(Marketplace(code="second_writer", name="Second", active=True))
                session.commit()
        except Exception as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_write_lock)
    other = threading.Thread(target=concurrent_write)
    holder.start()
    other.start()
    holder.join(timeout=5)
    other.join(timeout=5)

    assert not holder.is_alive() and not other.is_alive()
    assert not errors, f"concurrent writer should wait via busy_timeout instead of failing: {errors}"
    with OrmSession(writer_engine) as session:
        assert session.scalar(select(func.count()).select_from(Marketplace)) == 2


def test_zero_busy_timeout_reproduces_the_original_locked_error(tmp_path):
    # Contrast case: with no busy handler at all (the pre-fix equivalent),
    # the same contention pattern above genuinely raises "database is locked"
    # instead of waiting — this proves build_engine's busy_timeout is what
    # actually prevents the 500, not some incidental timing.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from app.db import Base, build_engine

    db_path = tmp_path / "no_busy_timeout.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    setup_engine = build_engine(url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    zero_timeout_engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 0})
    other_engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 0})
    errors: list[Exception] = []

    def hold_write_lock():
        with OrmSession(zero_timeout_engine) as session:
            session.add(Marketplace(code="holder2", name="Holder2", active=True))
            session.flush()
            time.sleep(0.3)
            session.commit()

    def concurrent_write():
        try:
            time.sleep(0.05)
            with OrmSession(other_engine) as session:
                session.add(Marketplace(code="second_writer2", name="Second2", active=True))
                session.commit()
        except Exception as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_write_lock)
    other = threading.Thread(target=concurrent_write)
    holder.start()
    other.start()
    holder.join(timeout=5)
    other.join(timeout=5)

    assert errors, "expected a locked-database error with no busy timeout configured"
    assert "locked" in str(errors[0]).lower()


@dataclass
class DelayedProvider(PriceProvider):
    code: str
    delay_seconds: float
    response: ProviderResponse | None = None
    display_name: str = "Delayed"
    base_url: str | None = "https://example.test/"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        time.sleep(self.delay_seconds)
        return self.response or ProviderResponse("empty")


def test_providers_are_queried_in_parallel_not_summed_sequentially(db_session):
    # This is the concrete fix for "JAN 查询速度不稳定，有时快有时明显慢": providers
    # used to run one after another, so total latency was the SUM of every
    # provider's response time. Three providers each sleeping 0.3s must now
    # complete in well under their sum (0.9s), proving they run concurrently.
    providers = [
        DelayedProvider("slow_a", 0.3, ProviderResponse("success", (offer(500),))),
        DelayedProvider("slow_b", 0.3, ProviderResponse("success", (offer(600),))),
        DelayedProvider("slow_c", 0.3, ProviderResponse("success", (offer(700),))),
    ]
    started = time.monotonic()
    # trigger_enrichment=False matches how the real HTTP routes call this --
    # they defer enrichment to a BackgroundTask instead of running it inline,
    # so this isolates the provider-querying phase itself.
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers, trigger_enrichment=False)
    elapsed = time.monotonic() - started

    assert elapsed < 0.65, f"providers ran sequentially instead of in parallel: {elapsed:.2f}s"
    statuses = {item.provider_code: item.status for item in view.attempts}
    assert statuses == {"slow_a": "success", "slow_b": "success", "slow_c": "success"}
    assert view.online_min_price == 500


def test_repeated_same_jan_force_refresh_query_does_not_500(db_session):
    provider = FakeProvider("repeat_provider", ProviderResponse("success", (offer(500),)))
    for _ in range(5):
        view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN, force_refresh=True), [provider])
        assert view.online_min_price == 500
    assert db_session.scalar(select(func.count()).select_from(PriceSearchRun)) == 5
    assert db_session.scalar(select(func.count()).select_from(PriceLookupHistory)) == 5
    assert db_session.scalar(select(func.count()).select_from(Marketplace).where(Marketplace.code == "repeat_provider")) == 1


def test_repeated_same_jan_query_over_http_does_not_500(client):
    test_client, _db_session, _ = client
    for _ in range(4):
        response = test_client.post(
            "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
        )
        assert response.status_code == 303
        result = test_client.get(response.headers["location"])
        assert result.status_code == 200


def test_provider_processing_failure_is_isolated_and_does_not_500(db_session, monkeypatch):
    original_record_state = price_service._record_provider_state

    def failing_record_state(session, provider, response, *, tested_at):
        if provider.code == "boom_provider":
            raise RuntimeError("boom during processing")
        return original_record_state(session, provider, response, tested_at=tested_at)

    monkeypatch.setattr(price_service, "_record_provider_state", failing_record_state)
    providers = [
        FakeProvider("boom_provider", ProviderResponse("success", (offer(500),))),
        FakeProvider("ok_provider", ProviderResponse("success", (offer(600),))),
    ]

    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers)

    statuses = {item.provider_code: item.status for item in view.attempts}
    assert statuses["boom_provider"] == "error"
    assert statuses["ok_provider"] == "success"
    assert view.providers_partial_failed is True
    assert view.providers_all_failed is False
    assert view.online_min_price == 600


def test_rakuten_offer_still_participates_when_another_provider_fails(db_session):
    # Rakuten stays a price source even though its images are deprioritized
    # elsewhere (product_enrichment's image ranking) -- those are separate
    # concerns, and a query must not drop or special-case Rakuten's price data.
    providers = [
        FakeProvider("rakuten", ProviderResponse("success", (offer(800),))),
        FakeProvider("yahoo_shopping", error=RuntimeError("yahoo down")),
    ]
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers)
    statuses = {item.provider_code: item.status for item in view.attempts}
    assert statuses == {"rakuten": "success", "yahoo_shopping": "error"}
    assert view.providers_partial_failed is True
    assert any(offer_.marketplace.code == "rakuten" for offer_ in view.trusted_offers)
    assert view.online_min_price == 800


def test_each_configured_provider_is_called_exactly_once_per_query(db_session):
    providers = [
        FakeProvider("prov_a", ProviderResponse("success", (offer(500),))),
        FakeProvider("prov_b", ProviderResponse("empty")),
        FakeProvider("prov_c", error=RuntimeError("boom")),
    ]
    query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers)
    assert [provider.calls for provider in providers] == [1, 1, 1]


def test_refresh_product_online_price_requires_jan(db_session):
    from app.price_service import refresh_product_online_price

    product = Product(jan=None, name_cn="无条码商品")
    db_session.add(product)
    db_session.commit()
    with pytest.raises(ValueError, match="JAN"):
        refresh_product_online_price(db_session, product)


def test_refresh_product_online_price_reuses_query_prices(db_session, monkeypatch):
    # Product-detail and watched-products re-query must go through the exact
    # same provider pipeline as scanning -- not a separate hardcoded search.
    import app.price_service as price_service_module
    from app.price_service import refresh_product_online_price

    product = Product(jan=VALID_JAN, name_cn="重新查价商品")
    db_session.add(product)
    db_session.commit()
    fake = FakeProvider("rakuten", ProviderResponse("success", (offer(1234),)))
    monkeypatch.setattr(price_service_module, "get_default_price_providers", lambda: [fake])

    view = refresh_product_online_price(db_session, product)

    assert fake.calls == 1
    assert view.online_min_price == 1234
    assert view.product.id == product.id


def test_product_detail_refresh_price_route_calls_unified_service(client, monkeypatch):
    import app.price_service as price_service_module

    http, db, _ = client
    product = Product(jan=VALID_JAN, name_cn="详情页重新查价")
    db.add(product)
    db.commit()
    fake = FakeProvider("rakuten", ProviderResponse("success", (offer(2000),)))
    monkeypatch.setattr(price_service_module, "get_default_price_providers", lambda: [fake])

    response = http.post(f"/products/{product.id}/refresh-price", follow_redirects=True)

    assert response.status_code == 200
    assert fake.calls == 1
    saved = db.scalar(select(ProductOffer).where(ProductOffer.product_id == product.id))
    assert saved is not None and saved.item_price == 2000


def test_watched_products_refresh_price_route_calls_unified_service(client, monkeypatch):
    import app.price_service as price_service_module
    from app.watch_service import add_watch

    http, db, _ = client
    product = Product(jan=VALID_JAN, name_cn="关注商品重新查价", purchase_price=100)
    db.add(product)
    db.commit()
    add_watch(db, product.id)
    fake = FakeProvider("rakuten", ProviderResponse("success", (offer(3000),)))
    monkeypatch.setattr(price_service_module, "get_default_price_providers", lambda: [fake])

    response = http.post(f"/watched-products/{product.id}/refresh-price", follow_redirects=True)

    assert response.status_code == 200
    assert fake.calls == 1
    saved = db.scalar(select(ProductOffer).where(ProductOffer.product_id == product.id))
    assert saved is not None and saved.item_price == 3000


def test_all_providers_failing_returns_view_with_retry_flag_not_500(db_session):
    providers = [
        FakeProvider("fail_one", error=RuntimeError("boom")),
        FakeProvider("fail_two", error=TimeoutError()),
    ]
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), providers)
    assert view.providers_all_failed is True
    assert view.providers_partial_failed is False
    assert view.result_offers == ()


def test_marketplace_lookup_recovers_from_concurrent_insert_race(db_session, monkeypatch):
    provider = FakeProvider("race_provider", ProviderResponse("success", (offer(700),)))
    real_marketplace = price_service._marketplace
    calls = {"count": 0}

    def racy_marketplace(session, provider_arg):
        calls["count"] += 1
        if calls["count"] == 1:
            session.add(Marketplace(code=provider_arg.code, name=provider_arg.display_name, active=True))
            session.flush()
        return real_marketplace(session, provider_arg)

    monkeypatch.setattr(price_service, "_marketplace", racy_marketplace)
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])
    assert view.online_min_price == 700
    assert db_session.scalar(select(func.count()).select_from(Marketplace).where(Marketplace.code == "race_provider")) == 1


def test_store_price_can_be_saved_reopened_and_does_not_overwrite_online_price(db_session):
    provider = FakeProvider("store_price_provider", ProviderResponse("success", (offer(500),)))
    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [provider])

    updated = update_store_price(db_session, view.history.id, 2000)
    assert updated.current_store_price == 2000

    reopened = build_lookup_view(db_session, view.history.id)
    assert reopened.current_store_price == 2000
    assert reopened.online_min_price == 500

    cleared = update_store_price(db_session, view.history.id, None)
    assert cleared.current_store_price is None


def _make_valid_jan(body12: str) -> str:
    digits = [int(c) for c in body12]
    weighted = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - weighted % 10) % 10
    return body12 + str(check)


def test_20x_same_jan_and_20x_different_jan_with_concurrent_background_writer_no_500(tmp_path):
    # P0 stress repro requested on-site: same-JAN repeats, different-JAN
    # repeats, and a background writer standing in for
    # process_price_lookup_enrichment, all hitting the same sqlite file
    # concurrently. At least 20 iterations of each pattern; zero exceptions
    # allowed anywhere.
    import app.models  # noqa: F401
    from sqlalchemy.orm import Session as OrmSession

    from app.db import Base, build_engine

    db_path = tmp_path / "stress20.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    setup_engine = build_engine(url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    stop_flag = threading.Event()
    background_errors: list[Exception] = []

    def background_writer():
        engine = build_engine(url)
        counter = 0
        while not stop_flag.is_set():
            counter += 1
            try:
                with OrmSession(engine) as session:
                    session.add(Marketplace(code=f"bg_writer_{counter}", name=f"BG{counter}", active=True))
                    session.commit()
            except Exception as exc:  # pragma: no cover - failure path asserted below
                background_errors.append(exc)
            time.sleep(0.01)
        engine.dispose()

    bg_thread = threading.Thread(target=background_writer, daemon=True)
    bg_thread.start()

    foreground_errors: list[Exception] = []

    def run_query(jan: str):
        engine = build_engine(url)
        try:
            with OrmSession(engine) as session:
                provider = FakeProvider(f"stress_{jan}", ProviderResponse("success", (offer(500, jan=jan),)))
                query_prices(session, PriceLookupInput(jan=jan, force_refresh=True), [provider])
        except Exception as exc:
            foreground_errors.append(exc)
        finally:
            engine.dispose()

    try:
        for _ in range(20):
            run_query(VALID_JAN)
        different_jans = [_make_valid_jan(f"4901{i:08d}") for i in range(20)]
        for jan in different_jans:
            run_query(jan)
    finally:
        stop_flag.set()
        bg_thread.join(timeout=5)

    assert not bg_thread.is_alive()
    assert not foreground_errors, f"foreground query raised: {foreground_errors}"
    assert not background_errors, f"background writer raised: {background_errors}"

    verify_engine = build_engine(url)
    with OrmSession(verify_engine) as session:
        assert session.scalar(select(func.count()).select_from(PriceSearchRun)) == 40


def test_camera_diagnostics_endpoint_accepts_payload_and_never_500s(client):
    test_client, _db_session, _ = client
    response = test_client.post("/api/camera-diagnostics", json={
        "context": "price_check", "userAgent": "iPhone test UA", "mode": "first_decode",
        "roiMode": "full_frame", "readyState": 4, "videoWidth": 1280, "videoHeight": 720,
        "settingsResolution": "1280x720", "deviceLabel": "Back Camera",
        "barcodeDetectorSupported": False, "zxingLoaded": True, "decodesPerSecond": 5,
        "firstDecodeMs": 812, "lastException": "none",
    })
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    minimal = test_client.post("/api/camera-diagnostics", json={})
    assert minimal.status_code == 200


def test_store_price_full_redirect_chain_saves_and_shows_value(client):
    # Exact real-device chain: open the result page, POST the store price with
    # follow_redirects=True (the way a real phone/browser actually behaves),
    # and confirm the final response is 200 on the same JAN's result page,
    # showing the saved value -- not a 404, not a different JAN, not a 500.
    test_client, db_session, _ = client
    scan = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    assert scan.status_code == 303
    result_url = scan.headers["location"]

    opened = test_client.get(result_url)
    assert opened.status_code == 200
    history_id = int(result_url.rstrip("/").rsplit("/", 1)[-1])
    form_action = f"/price-check/results/{history_id}/store-price"
    assert f'action="{form_action}"' in opened.text

    final = test_client.post(
        form_action, data={"current_store_price": "1540"}, follow_redirects=True,
    )
    assert final.status_code == 200
    assert len(final.history) == 1 and final.history[0].status_code == 303
    assert f"JAN {VALID_JAN}" in final.text
    assert 'value="1540"' in final.text
    assert "已保存" in final.text

    db_session.expire_all()
    saved = db_session.get(PriceLookupHistory, history_id)
    assert saved.current_store_price == 1540


def test_store_price_route_saves_and_persists_on_reopen(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    result_url = response.headers["location"]
    history_id = int(result_url.rstrip("/").rsplit("/", 1)[-1])

    save = test_client.post(
        f"/price-check/results/{history_id}/store-price",
        data={"current_store_price": "2500"}, follow_redirects=False,
    )
    assert save.status_code == 303
    assert "price_saved=1" in save.headers["location"]

    reopened = test_client.get(save.headers["location"])
    assert reopened.status_code == 200
    assert 'value="2500"' in reopened.text
    assert "已保存" in reopened.text

    reopened_again = test_client.get(f"/price-check/results/{history_id}")
    assert 'value="2500"' in reopened_again.text


def test_store_price_route_rejects_invalid_value(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    history_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[-1])

    save = test_client.post(
        f"/price-check/results/{history_id}/store-price",
        data={"current_store_price": "not-a-number"}, follow_redirects=False,
    )
    assert save.status_code == 303
    assert "price_error=" in save.headers["location"]


def test_result_page_orders_offers_before_local_product_card(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    body = result.text.split("</head>", 1)[1]
    offers_index = body.index("线上商品结果一览")
    local_card_index = body.index("local-product-card")
    assert offers_index < local_card_index
    assert body.count("JAN " + VALID_JAN) == 1


def test_result_page_compare_area_is_one_compact_card_with_tax_inclusive_label(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "current_store_price": "659", "force_refresh": "true"},
        follow_redirects=False,
    )
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    body = result.text

    assert "店内价" in body and "税込" in body
    # 店内价/网上最低/差额 must all live inside ONE .price-compare-card, not
    # three separate cards -- and 差额 specifically must not be its own card.
    assert body.count('class="card price-compare-card"') == 1
    assert "comparison-grid" not in body
    compare_section = body.split('class="card price-compare-card"', 1)[1].split("</section>", 1)[0]
    assert "店内价" in compare_section
    assert "网上最低" in compare_section
    assert "差额" in compare_section
    # 差额 must be a plain inline block within the same card, not `class="card ...`.
    diff_html = compare_section.split("price-compare-diff", 1)[1]
    assert '<div class="card' not in diff_html and '<section class="card' not in diff_html


def test_result_page_store_price_and_online_min_and_difference_correct(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "current_store_price": "659", "force_refresh": "true"},
        follow_redirects=False,
    )
    result = test_client.get(response.headers["location"])
    body = result.text
    assert 'value="659"' in body
    online_min_present = "暂无可信结果" in body or "¥" in body.split("网上最低", 1)[1][:200]
    assert online_min_present
    assert "差额" in body


def test_mobile_header_batch_badge_hidden_on_price_check_pages(client):
    test_client, _db_session, _ = client
    scan_page = test_client.get("/price-check")
    assert "price-check-page" in scan_page.text
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    result = test_client.get(response.headers["location"])
    assert "price-check-page" in result.text
    # field-purchase (batch-tracked flow) must be unaffected -- it should NOT
    # carry the price-check-only body class.
    field_page = test_client.get("/field-purchase")
    assert "price-check-page" not in field_page.text


def test_price_check_page_collapses_manual_entry_and_hides_debug_by_default(client):
    test_client, _db_session, _ = client
    response = test_client.get("/price-check")
    assert response.status_code == 200
    body = response.text
    # Manual JAN entry must be tucked inside a collapsed <details>, not a
    # top-level always-visible form block.
    assert '<summary>手动输入 JAN</summary>' in body
    manual_section = body.split('<summary>手动输入 JAN</summary>', 1)[1].split("</details>", 1)[0]
    assert 'id="priceLookupForm"' in manual_section
    assert 'id="janInput"' in manual_section
    # Debug JSON panel must default to hidden (no ?debug=1 given).
    assert 'id="priceCameraDebug"' in body
    debug_tag = body[body.index('id="priceCameraDebug"') - 5 : body.index('id="priceCameraDebug"') + 200]
    assert "hidden" in debug_tag


def test_price_check_page_debug_param_shows_debug_panel(client):
    test_client, _db_session, _ = client
    response = test_client.get("/price-check?debug=1")
    assert response.status_code == 200
    debug_tag = response.text[
        response.text.index('id="priceCameraDebug"') - 5 : response.text.index('id="priceCameraDebug"') + 60
    ]
    assert "hidden" not in debug_tag


def test_price_check_page_autostart_param_triggers_camera_start(client):
    test_client, _db_session, _ = client
    no_autostart = test_client.get("/price-check")
    assert no_autostart.status_code == 200
    autostart = test_client.get("/price-check?autostart=1")
    assert autostart.status_code == 200
    body = autostart.text
    assert "autostart" in body and "startCamera()" in body
    # The autostart trigger must be conditional (reads the query param at
    # runtime), not an unconditional call baked into every page load --
    # otherwise a plain /price-check visit would also silently grab the
    # camera without the user tapping anything.
    assert "get('autostart') === '1'" in body


def test_result_page_continue_scan_links_include_autostart(client):
    test_client, _db_session, _ = client
    response = test_client.post(
        "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=False,
    )
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    assert result.text.count('href="/price-check?autostart=1"') == 2


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
    # The background enrichment task runs on its own engine/session (a separate
    # connection from db_session's), so db_session's identity map can still be
    # holding the pre-enrichment PriceSearchRun/PriceLookupHistory objects it
    # loaded during the POST above; expire_all() forces the next read to see
    # what the background task actually committed (this mirrors real usage,
    # where each request gets a brand new session with an empty identity map).
    db_session.expire_all()
    result = test_client.get(response.headers["location"])
    assert result.status_code == 200
    assert "本地已有商品" in result.text and "当前按商品价格排序，未计入配送费。" in result.text
    assert test_client.get("/health").status_code == 200
    assert db_session.scalar(select(func.count()).select_from(PriceProviderAttempt)) == 5


# ---------------- durability: repeated real scans over HTTP ----------------
# Reproduces the on-site complaint: scans 1-7 fine, degrading from ~8 onward,
# eventually an occasional 500. Every scan of an incomplete-status JAN
# schedules a background enrichment task via `background_tasks.add_task`,
# which TestClient (like real Starlette) runs synchronously before the
# request returns -- so these HTTP-level loops exercise the exact same
# engine-per-background-task code path a real continuous scanning session
# hits, not just the in-memory query_prices() function.


class FastSimProvider(PriceProvider):
    code = "sim_fast"
    display_name = "SimFast"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse("success", (offer(400, jan=jan),))


class SlowSimProvider(PriceProvider):
    code = "sim_slow"
    display_name = "SimSlow"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        time.sleep(0.15)
        return ProviderResponse("success", (offer(500, jan=jan),))


class TimeoutSimProvider(PriceProvider):
    code = "sim_timeout"
    display_name = "SimTimeout"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        raise TimeoutError("simulated provider timeout")


class ExceptionSimProvider(PriceProvider):
    code = "sim_exception"
    display_name = "SimException"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        raise RuntimeError("simulated provider crash")


def test_30x_same_jan_scan_over_http_no_500_and_no_runaway_slowdown(client):
    test_client, _db_session, _ = client
    durations = []
    for i in range(30):
        started = time.monotonic()
        response = test_client.post(
            "/price-check", data={"jan": VALID_JAN, "force_refresh": "true"}, follow_redirects=True,
        )
        durations.append(time.monotonic() - started)
        assert response.status_code == 200, f"scan {i + 1} failed with {response.status_code}"

    first_five_avg = sum(durations[:5]) / 5
    last_five_avg = sum(durations[-5:]) / 5
    assert last_five_avg < max(first_five_avg * 2, 0.2), (
        f"scans degraded across the run: first5_avg={first_five_avg:.3f}s last5_avg={last_five_avg:.3f}s "
        f"all={[round(d, 3) for d in durations]}"
    )


def test_30_different_jans_over_http_no_500(client):
    test_client, _db_session, _ = client
    for i in range(30):
        jan = _make_valid_jan(f"4906{i:08d}")
        response = test_client.post(
            "/price-check", data={"jan": jan, "force_refresh": "true"}, follow_redirects=True,
        )
        assert response.status_code == 200, f"jan #{i + 1} ({jan}) failed with {response.status_code}"


def test_background_enrichment_engine_is_disposed_every_time(client, monkeypatch):
    # This is the concrete regression guard for the engine leak: every scan of
    # a still-incomplete JAN used to open a fresh SQLAlchemy engine (and its
    # connection pool) in process_price_lookup_enrichment and never dispose
    # it, so N scans left N abandoned open sqlite connections behind.
    from sqlalchemy.engine import Engine

    dispose_count = {"n": 0}
    original_dispose = Engine.dispose

    def counting_dispose(self, *args, **kwargs):
        dispose_count["n"] += 1
        return original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "dispose", counting_dispose)

    test_client, _db_session, _ = client
    total = 10
    for i in range(total):
        jan = _make_valid_jan(f"4907{i:08d}")
        response = test_client.post(
            "/price-check", data={"jan": jan, "force_refresh": "true"}, follow_redirects=True,
        )
        assert response.status_code == 200

    assert dispose_count["n"] >= total, (
        f"expected at least {total} engine disposals from background enrichment, got {dispose_count['n']}"
    )


def test_same_jan_repeated_enrichment_does_not_reprocess_within_cooldown(db_session, monkeypatch):
    import app.product_enrichment as enrichment

    process_calls = {"n": 0}
    original_process = enrichment.process_enrichment_task

    def counting_process(session, task, **kwargs):
        process_calls["n"] += 1
        return original_process(session, task, **kwargs)

    monkeypatch.setattr(enrichment, "process_enrichment_task", counting_process)

    db_url = db_session.get_bind().url.render_as_string(hide_password=False)
    provider = FastSimProvider()
    view = query_prices(db_session, PriceLookupInput(jan=OTHER_JAN, force_refresh=True), [provider], trigger_enrichment=False)
    db_session.commit()

    # First call actually runs the pipeline (each call opens its own engine
    # against the same file, exactly like the real background task does).
    enrichment.process_price_lookup_enrichment(db_url, OTHER_JAN, view.history.id)
    assert process_calls["n"] == 1

    # Repeat calls immediately after (same JAN, task now completed_with_warnings
    # because there's no real DeepSeek/image config in tests) must be skipped
    # by the cooldown guard instead of re-running the whole pipeline again.
    for _ in range(3):
        enrichment.process_price_lookup_enrichment(db_url, OTHER_JAN, view.history.id)
    assert process_calls["n"] == 1


def test_enrichment_global_concurrency_cap_defers_excess_tasks(tmp_path, monkeypatch):
    # Different JANs are NOT deduped against each other -- only a global cap
    # (ENRICHMENT_MAX_CONCURRENCY) protects the shared threadpool from being
    # starved when several different-JAN scans each schedule their own
    # background enrichment task at roughly the same time.
    import app.models  # noqa: F401
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session as OrmSession

    from app.db import Base, build_engine
    import app.product_enrichment as enrichment

    db_path = tmp_path / "concurrency_cap.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    setup_engine = build_engine(url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    provider = FastSimProvider()
    jobs = []
    seed_engine = build_engine(url)
    with OrmSession(seed_engine) as session:
        for i in range(5):
            jan = _make_valid_jan(f"4911{i:08d}")
            view = query_prices(session, PriceLookupInput(jan=jan, force_refresh=True), [provider], trigger_enrichment=False)
            jobs.append((jan, view.history.id))
    seed_engine.dispose()

    concurrent_now = {"n": 0, "max_seen": 0}
    lock = threading.Lock()

    def slow_process(session, task, **kwargs):
        with lock:
            concurrent_now["n"] += 1
            concurrent_now["max_seen"] = max(concurrent_now["max_seen"], concurrent_now["n"])
        time.sleep(0.5)
        with lock:
            concurrent_now["n"] -= 1
        task.status = "completed_with_warnings"
        task.completed_at = datetime.now(timezone.utc)
        session.commit()
        return task

    monkeypatch.setattr(enrichment, "process_enrichment_task", slow_process)

    threads = [
        threading.Thread(target=enrichment.process_price_lookup_enrichment, args=(url, jan, history_id))
        for jan, history_id in jobs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert enrichment.ENRICHMENT_MAX_CONCURRENCY == 2, "test assumes the documented default cap of 2"
    assert concurrent_now["max_seen"] <= 2, (
        f"more than 2 enrichment tasks ran at once (cap not enforced): {concurrent_now['max_seen']}"
    )
    snapshot = enrichment.enrichment_concurrency_snapshot()
    assert snapshot["active_count"] == 0, "semaphore slots must all be released after completion"


def test_provider_mix_fast_slow_timeout_exception_over_http_no_500(client, monkeypatch):
    test_client, _db_session, _ = client
    mix = [FastSimProvider(), SlowSimProvider(), TimeoutSimProvider(), ExceptionSimProvider()]
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: mix)

    for i in range(8):
        jan = _make_valid_jan(f"4908{i:08d}")
        response = test_client.post(
            "/price-check", data={"jan": jan, "force_refresh": "true"}, follow_redirects=True,
        )
        assert response.status_code == 200, f"jan #{i + 1} failed with {response.status_code}"
