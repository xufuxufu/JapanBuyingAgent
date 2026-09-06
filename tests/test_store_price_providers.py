from __future__ import annotations

import json

import httpx
from sqlalchemy import select

from app.models import Product
from app.price_providers import AnpanmanStorePriceProvider, NishimatsuyaPriceProvider
from app.price_service import query_prices
from app.schemas import PriceLookupInput


VALID_JAN = "4901234567894"


class SequencedClient:
    """Returns one canned response per call, in order; extra calls repeat the last one."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def html_response(url: str, body: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": content_type}, request=httpx.Request("GET", url))


def json_response(url: str, payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, text=json.dumps(payload), headers={"content-type": "application/json"},
        request=httpx.Request("GET", url),
    )


# ---------------------------------------------------------------------------
# Anpanman official store (Shopify storefront)
# ---------------------------------------------------------------------------

ANPANMAN_SEARCH_HTML = """
<html><body>
<product-item>
  <a href="/products/a54x0009?_pos=1&_sid=abc&_ss=r">アンパンマンはじめてハウスドール</a>
</product-item>
</body></html>
"""

ANPANMAN_PRODUCT_JSON = {
    "product": {
        "title": "アンパンマンはじめてハウスドール アンパンマンごうで出発！アンパンマンセット",
        "vendor": "バンダイ",
        "handle": "a54x0009",
        "variants": [{
            "price": "2310", "compare_at_price": "", "barcode": VALID_JAN, "sku": "a54x0009s001",
        }],
        "images": [{"src": "https://cdn.shopify.com/s/files/1/0000/0000/product.jpg"}],
    }
}


def test_anpanman_search_success_parses_title_price_url_image_brand():
    client = SequencedClient([
        html_response("https://store.anpanman.jp/search", ANPANMAN_SEARCH_HTML),
        json_response("https://store.anpanman.jp/products/a54x0009.json", ANPANMAN_PRODUCT_JSON),
    ])
    provider = AnpanmanStorePriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "success"
    assert len(response.offers) == 1
    offer = response.offers[0]
    assert offer.title == "アンパンマンはじめてハウスドール アンパンマンごうで出発！アンパンマンセット"
    assert offer.item_price == 2310
    assert offer.url == "https://store.anpanman.jp/products/a54x0009"
    assert offer.image_url == "https://cdn.shopify.com/s/files/1/0000/0000/product.jpg"
    assert offer.brand == "バンダイ"
    assert offer.jan == VALID_JAN and offer.jan_verified is True
    assert offer.match_type == "EXACT_JAN"


def test_anpanman_distinguishes_current_price_from_compare_at_price():
    payload = json.loads(json.dumps(ANPANMAN_PRODUCT_JSON))
    payload["product"]["variants"][0]["price"] = "1980"
    payload["product"]["variants"][0]["compare_at_price"] = "2500"
    client = SequencedClient([
        html_response("https://store.anpanman.jp/search", ANPANMAN_SEARCH_HTML),
        json_response("https://store.anpanman.jp/products/a54x0009.json", payload),
    ])
    provider = AnpanmanStorePriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    offer = response.offers[0]
    assert offer.item_price == 1980
    assert offer.raw_data["compare_at_price"] == 2500


def test_anpanman_no_search_results_returns_empty_not_error():
    client = SequencedClient([html_response("https://store.anpanman.jp/search", "<html><body>0件</body></html>")])
    provider = AnpanmanStorePriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "empty"
    assert response.offers == ()
    assert response.search_url.startswith("https://store.anpanman.jp/search?q=")


def test_anpanman_malformed_product_json_is_skipped_not_fatal():
    client = SequencedClient([
        html_response("https://store.anpanman.jp/search", ANPANMAN_SEARCH_HTML),
        json_response("https://store.anpanman.jp/products/a54x0009.json", {"product": {"title": "无变体商品", "variants": []}}),
    ])
    provider = AnpanmanStorePriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "empty"
    assert response.offers == ()


def test_anpanman_page_fetch_exception_returns_error_response():
    class RaisingClient:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    provider = AnpanmanStorePriceProvider(client=RaisingClient())

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "error"
    assert response.error_code == "SEARCH_FAILED"


# ---------------------------------------------------------------------------
# Nishimatsuya (site-scoped web fallback -- no reachable site to talk to
# directly, see NishimatsuyaPriceProvider's docstring)
# ---------------------------------------------------------------------------

def test_nishimatsuya_search_success_via_json_ld():
    jan = "4960919412621"
    search_html = '<a class="result__a" href="https://www.24028-net.jp/shop/g/g12345/">西松屋 商品</a>'
    product_page = f"""
    <html><head>
      <meta property="og:title" content="西松屋">
      <script type="application/ld+json">{{
        "@context":"https://schema.org","@type":"Product",
        "name":"西松屋 ベビー服 セット",
        "gtin13":"{jan}",
        "offers":{{"@type":"Offer","price":"1980","priceCurrency":"JPY"}}
      }}</script>
    </head><body><h1>西松屋 ベビー服 セット</h1>JANコード {jan}</body></html>
    """
    client = SequencedClient([
        html_response("https://duckduckgo.com/html/", search_html),
        html_response("https://www.24028-net.jp/shop/g/g12345/", product_page),
    ])
    provider = NishimatsuyaPriceProvider(client=client)

    response = provider.search(jan, 4.0)

    assert response.status == "success"
    offer = response.offers[0]
    assert offer.title == "西松屋 ベビー服 セット"
    assert offer.item_price == 1980
    assert offer.jan_verified is True
    assert offer.url == "https://www.24028-net.jp/shop/g/g12345/"


def test_nishimatsuya_prefers_sale_price_over_struck_through_original_price():
    # Regression for "不要把划线原价当当前价格" -- when a page has no JSON-LD
    # and mentions the pre-discount reference price before the actual sale
    # price, the shared text-price extractor must not latch onto the label
    # "通常価格" and return the higher, no-longer-current amount.
    jan = "4960919412621"
    search_html = '<a class="result__a" href="https://www.24028-net.jp/shop/g/g99999/">西松屋 セール商品</a>'
    product_page = f"""
    <html><body><h1>西松屋 セール商品</h1>
    JANコード {jan} 通常価格 ¥2,500 セール価格 ¥1,980（税込）
    </body></html>
    """
    client = SequencedClient([
        html_response("https://duckduckgo.com/html/", search_html),
        html_response("https://www.24028-net.jp/shop/g/g99999/", product_page),
    ])
    provider = NishimatsuyaPriceProvider(client=client)

    response = provider.search(jan, 4.0)

    assert response.offers[0].item_price == 1980


def test_nishimatsuya_no_matching_domain_results_returns_empty():
    search_html = '<a class="result__a" href="https://example.com/unrelated">unrelated</a>'
    client = SequencedClient([html_response("https://duckduckgo.com/html/", search_html)])
    provider = NishimatsuyaPriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "empty"
    assert response.offers == ()


def test_nishimatsuya_page_fetch_error_is_skipped_not_fatal():
    search_html = '<a class="result__a" href="https://www.24028-net.jp/shop/g/gdead/">dead link</a>'
    client = SequencedClient([
        html_response("https://duckduckgo.com/html/", search_html),
        html_response("https://www.24028-net.jp/shop/g/gdead/", "not found", status=500),
    ])
    provider = NishimatsuyaPriceProvider(client=client)

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "empty"
    assert response.offers == ()


def test_nishimatsuya_search_request_exception_returns_error_response():
    class RaisingClient:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    provider = NishimatsuyaPriceProvider(client=RaisingClient())

    response = provider.search(VALID_JAN, 4.0)

    assert response.status == "error"
    assert response.error_code == "SEARCH_FAILED"


# ---------------------------------------------------------------------------
# Unified pipeline: both new providers plug into query_prices() untouched
# ---------------------------------------------------------------------------

def test_both_new_providers_are_called_and_one_failing_does_not_affect_the_other(db_session):
    anpanman_client = SequencedClient([
        html_response("https://store.anpanman.jp/search", ANPANMAN_SEARCH_HTML),
        json_response("https://store.anpanman.jp/products/a54x0009.json", ANPANMAN_PRODUCT_JSON),
    ])

    class BrokenNishimatsuyaClient:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("cloudfront blocked", request=httpx.Request("GET", url))

    anpanman = AnpanmanStorePriceProvider(client=anpanman_client)
    nishimatsuya = NishimatsuyaPriceProvider(client=BrokenNishimatsuyaClient())

    view = query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [nishimatsuya, anpanman])

    statuses = {item.provider_code: item.status for item in view.attempts}
    assert statuses == {"nishimatsuya": "error", "anpanman_store": "success"}
    assert view.providers_partial_failed is True
    assert any(offer.marketplace.code == "anpanman_store" for offer in view.trusted_offers)
    # each provider's own search() ran exactly once for this single lookup
    assert len(anpanman_client.calls) == 2  # search page + one product.json
    product = db_session.scalar(select(Product).where(Product.jan == VALID_JAN))
    assert product is not None


def test_no_duplicate_calls_when_both_new_providers_configured(db_session):
    anpanman_client = SequencedClient([
        html_response("https://store.anpanman.jp/search", "<html><body>0件</body></html>"),
    ])
    nishimatsuya_client = SequencedClient([
        html_response("https://duckduckgo.com/html/", "<html><body>no results</body></html>"),
    ])
    anpanman = AnpanmanStorePriceProvider(client=anpanman_client)
    nishimatsuya = NishimatsuyaPriceProvider(client=nishimatsuya_client)

    query_prices(db_session, PriceLookupInput(jan=VALID_JAN), [nishimatsuya, anpanman])

    assert len(anpanman_client.calls) == 1
    assert len(nishimatsuya_client.calls) == 1
