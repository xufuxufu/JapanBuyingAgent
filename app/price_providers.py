from __future__ import annotations

import os
import json
import re
from html import unescape
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from app.config import clean_env_value, rakuten_http_referer
from app.local_product import resolve_local_product_by_jan
from app.product_image_localization import preferred_product_image_url
from app.product_specs import extract_spec_text, parse_product_specs
from app.product_translation_service import product_display_label
from app.rakuten_ip_monitor import rakuten_ip_warning_message, rakuten_public_ip_status


SUBSCRIPTION_PATTERN = re.compile(r"定期(?:購入|便)|サブスク|subscription", re.IGNORECASE)
JAN_PATTERN = re.compile(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)")
YEN_PRICE_PATTERNS = (
    # "通常価格"/"参考価格"/"旧価格"/"定価" label the pre-discount reference
    # price, not what a buyer actually pays -- deliberately not a trigger
    # label here (see the exclusion context below too), so a page showing
    # both a struck-through original price and a sale price doesn't return
    # the original one just because it happens to appear first in the text.
    # The bare "価格" alternative would otherwise also match as a substring of
    # "通常価格"/"参考価格"/"旧価格" (no word boundary between them in
    # Japanese), so those three are excluded via lookbehind right here rather
    # than relying only on the surrounding-context check below.
    re.compile(
        r"(?:セール価格|特価|会員価格|税込価格|販売価格|(?<!通常)(?<!参考)(?<!旧)価格|税込み?|税抜)"
        r"\D{0,24}(?:￥|¥)?\s*([1-9][0-9,]{1,8})\s*円?",
        re.IGNORECASE,
    ),
    re.compile(r"(?:￥|¥)\s*([1-9][0-9,]{1,8})"),
    re.compile(r"([1-9][0-9,]{1,8})\s*円\s*(?:\(?(?:税込|税抜)\)?)", re.IGNORECASE),
)
TRUSTED_WEB_DOMAINS = (
    "toei-anim.co.jp",
    "hands.net",
    "loft.co.jp",
    "0101.co.jp",
    "marui.co.jp",
    "yodobashi.com",
    "wowma.jp",
    "aupay.market",
    "cosme.net",
    "rakuten.co.jp",
    "yahoo.co.jp",
    "shopping.yahoo.co.jp",
    "amazon.co.jp",
)
MAX_WEB_FALLBACK_OFFERS = 3


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    title: str
    url: str
    item_price: int
    shipping_price: int = 0
    shipping_known: bool = True
    image_url: str | None = None
    seller: str | None = None
    jan: str | None = None
    stock_status: str = "unknown"
    condition: str = "new"
    listing_type: str = "single"
    brand: str | None = None
    currency: str = "JPY"
    link_type: str = "product"
    jan_verified: bool = False
    match_type: str = "UNVERIFIED"
    confidence: float = 0.0
    fetched_at: datetime | None = None
    error_code: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def unified(self, platform: str) -> dict[str, Any]:
        return {
            "platform": platform,
            "jan": self.jan,
            "title": self.title or None,
            "brand": self.brand,
            "price": self.item_price if self.item_price > 0 else None,
            "shipping_fee": self.shipping_price if self.shipping_known else None,
            "total_price": self.item_price + self.shipping_price if self.item_price > 0 and self.shipping_known else None,
            "currency": self.currency,
            "availability": self.stock_status,
            "seller": self.seller,
            "product_url": self.url or None,
            "image_url": self.image_url,
            "link_type": self.link_type,
            "jan_verified": self.jan_verified,
            "match_type": self.match_type,
            "confidence": self.confidence,
            "fetched_at": (self.fetched_at or datetime.now(timezone.utc)).isoformat(),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    status: str
    offers: tuple[PriceCandidate, ...] = ()
    message: str | None = None
    search_url: str | None = None
    error_code: str | None = None
    http_status: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PriceProvider(ABC):
    code: str
    display_name: str
    base_url: str | None

    @abstractmethod
    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        raise NotImplementedError

    def is_configured(self) -> bool:
        return True

    def search_link(self, jan: str) -> str | None:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _first_image(value: Any) -> str | None:
    if isinstance(value, dict):
        return str(value.get("imageUrl") or value.get("url") or "") or None
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and value:
        return _first_image(value[0])
    return None


def _jan_evidence(text: str, searched_jan: str) -> str | None:
    values = JAN_PATTERN.findall(text or "")
    if searched_jan in values:
        return searched_jan
    return values[0] if values else None


def _host(value: str) -> str:
    return (urlparse(value).hostname or "").casefold().removeprefix("www.")


def _trusted_web_rank(value: str) -> int | None:
    host = _host(value)
    if not host:
        return None
    for index, domain in enumerate(TRUSTED_WEB_DOMAINS):
        if host == domain or host.endswith(f".{domain}"):
            return index
    if host.endswith(".co.jp") or host.endswith(".jp"):
        return len(TRUSTED_WEB_DOMAINS) + 10
    return None


class _SearchResultParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href") or ""
        if not href:
            return
        url = urljoin(self.base_url, unescape(href))
        parsed = urlparse(url)
        if parsed.path.startswith("/l/"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                url = unquote(target)
        if url.startswith("http"):
            self.links.append(url)


class _ProductPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.images: list[str] = []
        self.json_ld_parts: list[str] = []
        self._tag_stack: list[str] = []
        self._json_ld_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)
        values = {key.casefold(): value for key, value in attrs if value}
        if tag == "script" and "ld+json" in (values.get("type") or "").casefold():
            self._json_ld_depth += 1
            return
        if tag in {"img", "source"}:
            for key in ("src", "data-src", "data-original", "data-lazy", "data-srcset", "srcset"):
                raw = values.get(key)
                if not raw:
                    continue
                for item in str(raw).split(","):
                    url = item.strip().split(" ", 1)[0]
                    if url and not url.startswith("data:"):
                        self.images.append(unescape(url))
        if tag != "meta":
            return
        key = values.get("property") or values.get("name") or values.get("itemprop") or ""
        content = values.get("content") or ""
        if key and content:
            self.meta[key.casefold()] = unescape(content).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self.json_ld_parts.append(data)
            return
        text = re.sub(r"\s+", " ", unescape(data or "")).strip()
        if not text:
            return
        self.text_parts.append(text)
        current = self._tag_stack[-1] if self._tag_stack else ""
        if current == "title":
            self.title_parts.append(text)
        elif current == "h1":
            self.h1_parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)

    @property
    def title(self) -> str:
        return (
            " ".join(self.h1_parts)
            or self.meta.get("og:title")
            or self.meta.get("twitter:title")
            or " ".join(self.title_parts)
        ).strip()

    @property
    def image(self) -> str | None:
        return self.meta.get("og:image") or self.meta.get("twitter:image") or self.meta.get("image")


def _json_ld_objects(parser: _ProductPageParser) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        output.append(value)
        if "@graph" in value:
            walk(value["@graph"])

    for raw in parser.json_ld_parts:
        try:
            walk(json.loads(raw.strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return output


def _json_ld_products(parser: _ProductPageParser) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for item in _json_ld_objects(parser):
        type_value = item.get("@type")
        types = {str(value).casefold() for value in type_value} if isinstance(type_value, list) else {str(type_value).casefold()}
        if "product" in types:
            products.append(item)
    return products


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("value") or value.get("@id")
        if isinstance(value, list):
            found = _first_text(*value)
            if found:
                return found
            continue
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text:
            return unescape(text)
    return None


def _json_ld_product_for_jan(products: list[dict[str, Any]], searched_jan: str) -> dict[str, Any] | None:
    for item in products:
        haystack = json.dumps(item, ensure_ascii=False)
        if searched_jan in JAN_PATTERN.findall(haystack):
            return item
    return products[0] if len(products) == 1 else None


def _json_ld_price(product: dict[str, Any] | None) -> int | None:
    if not product:
        return None
    offers = product.get("offers")
    candidates = offers if isinstance(offers, list) else [offers]
    for offer in candidates:
        if not isinstance(offer, dict):
            continue
        for value in (
            offer.get("price"),
            (offer.get("priceSpecification") or {}).get("price") if isinstance(offer.get("priceSpecification"), dict) else None,
            offer.get("lowPrice"),
        ):
            text = str(value or "").replace(",", "")
            if re.fullmatch(r"\d+(?:\.\d+)?", text):
                return int(float(text))
    return None


def _json_ld_image(product: dict[str, Any] | None) -> str | None:
    if not product:
        return None
    return _first_image(product.get("image"))


def _extract_price(text: str) -> int | None:
    for index, pattern in enumerate(YEN_PRICE_PATTERNS):
        for match in pattern.finditer(text):
            start = max(0, match.start() - 18)
            end = min(len(text), match.end() + 18)
            context = text[start:end]
            if re.search(r"送料|送料無料|以上購入|手数料", context):
                continue
            # Pattern 0 already anchors on an explicit "current price" label
            # (セール価格/税込価格/...) immediately before the number, and
            # "通常価格"/"参考価格"/etc. were deliberately left out of that
            # label list -- so only the label-less patterns (1/2, a bare ¥
            # amount) need this extra check, otherwise a nearby, unrelated
            # "通常価格 ¥2,500" a few characters before a genuine
            # "セール価格 ¥1,980" would wrongly veto the correct match too.
            if index != 0 and re.search(r"通常価格|参考価格|旧価格|定価", context):
                continue
            value = match.group(1).replace(",", "")
            if value.isdigit():
                return int(value)
    return None


def _seller_or_site_name(parser: _ProductPageParser, host: str) -> str:
    return (
        parser.meta.get("og:site_name")
        or parser.meta.get("application-name")
        or host
        or "Web"
    )


def _looks_like_site_name(title: str, parser: _ProductPageParser, host: str) -> bool:
    clean = normalize_product_name_whitespace(title).casefold()
    if not clean:
        return True
    site = normalize_product_name_whitespace(_seller_or_site_name(parser, host)).casefold()
    if site and clean == site:
        return True
    site_markers = (
        "東映アニメーションオフィシャルストア",
        "東映動畫官方商店",
        "东映动画官方商店",
        "ロフトネットストア",
        "loft",
    )
    product_markers = ("【", "】", "(", "（", "ml", "g", "mm", "cm", "個", "本", "枚", "セット", "jan")
    return any(marker.casefold() == clean or marker.casefold() in clean for marker in site_markers) and not any(
        marker.casefold() in clean for marker in product_markers
    )


def _clean_title_for_host(title: str | None, parser: _ProductPageParser, host: str) -> str | None:
    title = normalize_web_title(title)
    if not title or _looks_like_site_name(title, parser, host):
        return None
    return title


def _official_page_title(parser: _ProductPageParser, product: dict[str, Any] | None, host: str) -> str | None:
    json_name = _first_text((product or {}).get("name"))
    if title := _clean_title_for_host(json_name, parser, host):
        return title
    for h1 in parser.h1_parts:
        if title := _clean_title_for_host(h1, parser, host):
            return title
    for key in ("og:title", "twitter:title"):
        if title := _clean_title_for_host(parser.meta.get(key), parser, host):
            return title
    if title := _clean_title_for_host(" ".join(parser.title_parts), parser, host):
        return title
    return None


def _official_page_image(parser: _ProductPageParser, product: dict[str, Any] | None, host: str) -> str | None:
    if image := _json_ld_image(product):
        return image
    candidates = list(dict.fromkeys(parser.images + [parser.image] if parser.image else parser.images))
    if not candidates:
        return None

    def rank(url: str) -> tuple[int, int]:
        text = url.casefold()
        if "goods/l/" in text or "/img/goods/l/" in text:
            return (0, -len(url))
        if "/shop_assets/img/goods/l/" in text:
            return (0, -len(url))
        if "goods/m/" in text or "goods/s/" in text or "/shop_assets/img/goods/" in text:
            return (1, -len(url))
        if "goods" in text and not re.search(r"logo|ogp|banner|bnr|icon", text):
            return (2, -len(url))
        if re.search(r"logo|ogp|banner|bnr|icon|sprite", text):
            return (9, -len(url))
        return (5, -len(url))

    return min(candidates, key=rank)


def _official_page_raw_data(
    jan: str,
    parser: _ProductPageParser,
    product: dict[str, Any] | None,
    *,
    host: str,
    source_url: str,
    price: int | None,
    image_url: str | None,
    spec_text: str | None,
) -> dict[str, Any]:
    specs = parse_product_specs(spec_text or "", parser.text)
    brand = _first_text((product or {}).get("brand"), parser.meta.get("brand"))
    manufacturer = _first_text((product or {}).get("manufacturer"), parser.meta.get("manufacturer"))
    raw = {
        "janCode": jan,
        "shopName": _seller_or_site_name(parser, host),
        "seller": _seller_or_site_name(parser, host),
        "source": "web_fallback",
        "source_url": source_url,
        "source_domain": host,
        "availability": "unknown",
        "specification": spec_text,
        "spec_text": spec_text,
        "brand": brand,
        "manufacturer": manufacturer,
        "productImageUrl": image_url,
        "originalImageUrl": image_url,
        "price": price,
    }
    raw.update({key: value for key, value in specs.as_dict().items() if value is not None})
    return {key: value for key, value in raw.items() if value not in {None, ""}}


def normalize_product_name_whitespace(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_web_title(value: str | None) -> str:
    title = normalize_product_name_whitespace(value)
    title = re.sub(
        r"\s*[\|\-｜]\s*(?:通販|公式(?:通販|サイト)?|商品情報|楽天市場|Yahoo!ショッピング|"
        r"東映アニメーションオフィシャルストア|ロフトネットストア).*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    return title[:128]


class RakutenPriceProvider(PriceProvider):
    code = "rakuten"
    display_name = "Rakuten"
    base_url = "https://www.rakuten.co.jp/"
    product_endpoint = "https://openapi.rakuten.co.jp/ichibaproduct/api/Product/Search/20250801"
    item_endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
    genre_endpoint = "https://openapi.rakuten.co.jp/ichibagt/api/IchibaGenre/Search/20260701"
    item_api_version = "20260701"
    user_agent = "JapanBuyingAgent/1.0"
    _working_access_key_mode: str | None = None

    def __init__(self, client: Any | None = None):
        self.client = client

    def is_configured(self) -> bool:
        return bool(
            clean_env_value(os.getenv("JBA_RAKUTEN_APPLICATION_ID"))
            and clean_env_value(os.getenv("JBA_RAKUTEN_ACCESS_KEY"))
        )

    def search_link(self, jan: str) -> str:
        return f"https://search.rakuten.co.jp/search/mall/{quote_plus(jan)}/"

    def _key_hint(self, application_id: str, access_key: str) -> str:
        return f"env:JBA_RAKUTEN_APPLICATION_ID/**{application_id[-4:]} + env:JBA_RAKUTEN_ACCESS_KEY/**{access_key[-4:]}"

    def _product_search_enabled(self) -> bool:
        return os.getenv("JBA_RAKUTEN_PRODUCT_SEARCH_ENABLED", "").strip().casefold() in {"1", "true", "yes", "on"}

    def _rakuten_headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Referer": rakuten_http_referer(),
            "User-Agent": self.user_agent,
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _retry_after_seconds(self, response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(1, int(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            return max(1, int((target - datetime.now(timezone.utc)).total_seconds()))

    def _credential_diagnostics(self, application_id: str, access_key: str) -> dict[str, Any]:
        referer = rakuten_http_referer()
        return {
            "applicationIdPresent": bool(application_id),
            "applicationIdLength": len(application_id),
            "accessKeyPresent": bool(access_key),
            "accessKeyLength": len(access_key),
            "endpoint": self.item_endpoint,
            "apiVersion": self.item_api_version,
            "httpReferer": referer,
            "userAgent": self.user_agent,
        }

    def _error_fields(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        nested = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
        error_code = str(
            payload.get("errorCode")
            or payload.get("error")
            or payload.get("error_code")
            or nested.get("errorCode")
            or ""
        )[:120] or None
        error_message = str(
            payload.get("errorMessage")
            or payload.get("error_description")
            or payload.get("message")
            or payload.get("Message")
            or nested.get("errorMessage")
            or ""
        )[:240] or None
        return {
            "error": error_code,
            "error_description": error_message,
            "errorCode": error_code,
            "errorMessage": error_message,
        }

    @staticmethod
    def _redacted_url(url: str, application_id: str, access_key: str) -> str:
        redacted = url
        for secret in (application_id, access_key):
            if secret:
                redacted = redacted.replace(secret, "<redacted>")
        return redacted

    @staticmethod
    def _proxy_env_names() -> list[str]:
        names = (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        )
        return sorted(name for name in names if os.getenv(name))

    @staticmethod
    def _referer_origin(referer: str) -> dict[str, Any]:
        parsed = urlparse(referer)
        port = parsed.port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None)
        return {"scheme": parsed.scheme, "hostname": parsed.hostname, "port": port}

    def _response_trace(
        self,
        response: httpx.Response,
        *,
        application_id: str,
        access_key: str,
        expected_headers: dict[str, str],
    ) -> dict[str, Any]:
        request_headers = response.request.headers if response.request is not None else httpx.Headers()
        header_names = sorted({str(name) for name in request_headers.keys()})
        referer = expected_headers["Referer"]
        return {
            "final_url": self._redacted_url(str(response.url), application_id, access_key),
            "redirect_count": len(response.history),
            "redirect_history": [
                {
                    "http_status": item.status_code,
                    "url": self._redacted_url(str(item.url), application_id, access_key),
                    "location": self._redacted_url(item.headers.get("location", ""), application_id, access_key)
                    if item.headers.get("location") else None,
                }
                for item in response.history
            ],
            "request_has_referer": request_headers.get("Referer") == referer,
            "request_has_user_agent": request_headers.get("User-Agent") == self.user_agent,
            "request_header_names": header_names,
            "referer_origin": self._referer_origin(referer),
            "http_client": "httpx.Client",
            "proxy_env_present": bool(self._proxy_env_names()),
            "proxy_env_names": self._proxy_env_names(),
            "used_redirect": bool(response.history),
        }

    @staticmethod
    def _should_check_public_ip(response: httpx.Response, fields: dict[str, Any]) -> bool:
        if response.status_code != 403:
            return False
        combined = " ".join(str(fields.get(key) or "") for key in ("error", "error_description", "errorCode", "errorMessage")).casefold()
        return any(token in combined for token in ("request_context", "referrer", "referer", "ip", "address"))

    def _rakuten_ip_diagnostics(self, response: httpx.Response, fields: dict[str, Any]) -> dict[str, Any]:
        if self.client is not None or not self._should_check_public_ip(response, fields):
            return {}
        status = rakuten_public_ip_status(force=True)
        warning = rakuten_ip_warning_message(status)
        return {
            "rakuten_public_ip_status": status.as_dict(),
            "rakuten_public_ip_warning": warning,
        }

    def _get_once(
        self,
        url: str,
        params: dict[str, Any],
        timeout_seconds: float,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = self._rakuten_headers(headers)
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
                response = client.get(url, params=params, headers=request_headers)
                if response.history and response.request.headers.get("Referer") != request_headers["Referer"]:
                    response = client.get(str(response.url), headers=request_headers)
                return response
        kwargs: dict[str, Any] = {
            "params": params,
            "timeout": timeout_seconds,
            "headers": request_headers,
            "follow_redirects": True,
        }
        try:
            return self.client.get(url, **kwargs)
        except TypeError:
            kwargs.pop("follow_redirects", None)
            return self.client.get(url, **kwargs)

    @staticmethod
    def _redacted_preview(response: httpx.Response, application_id: str, access_key: str) -> str:
        body = response.text[:1000]
        for secret in (application_id, access_key):
            if secret:
                body = body.replace(secret, "<redacted>")
        return body

    def _diagnostic_request(
        self,
        *,
        label: str,
        url: str,
        params: dict[str, Any],
        timeout_seconds: float,
        application_id: str,
        access_key: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response | None]:
        request_headers = self._rakuten_headers(headers)
        try:
            response = self._get_once(url, params, timeout_seconds, headers=headers)
        except Exception as exc:
            return ({
                "label": label,
                "http_status": None,
                "content_type": None,
                "body_preview": None,
                "error": None,
                "error_description": str(exc)[:500],
                "errorCode": None,
                "errorMessage": str(exc)[:500],
                "exception_type": type(exc).__name__,
                "final_url": None,
                "redirect_count": None,
                "redirect_history": [],
                "request_has_referer": False,
                "request_has_user_agent": False,
                "request_header_names": sorted(request_headers.keys()),
                "referer_origin": self._referer_origin(request_headers["Referer"]),
                "http_client": "httpx.Client",
                "proxy_env_present": bool(self._proxy_env_names()),
                "proxy_env_names": self._proxy_env_names(),
                "used_redirect": False,
            }, None)
        fields = self._error_fields(response)
        trace = self._response_trace(
            response, application_id=application_id, access_key=access_key, expected_headers=request_headers,
        )
        ip_diagnostics = self._rakuten_ip_diagnostics(response, fields)
        return ({
            "label": label,
            "http_status": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "body_preview": self._redacted_preview(response, application_id, access_key),
            "error": fields.get("error"),
            "error_description": fields.get("error_description"),
            "errorCode": fields.get("errorCode"),
            "errorMessage": fields.get("errorMessage"),
            "exception_type": None,
            **trace,
            **ip_diagnostics,
        }, response)

    @staticmethod
    def _rakuten_judgment(results: dict[str, dict[str, Any]]) -> str:
        query_status = results["item_query"].get("http_status")
        header_status = results["item_header"].get("http_status")
        genre_status = results["genre_query"].get("http_status")
        if query_status == 200:
            return "请求代码正确：Item Search query 模式成功，生产调用使用 query 模式"
        if header_status == 200:
            return "请求代码正确：Item Search Header 模式成功，生产调用使用 Header 模式"
        combined = " ".join(
            str(result.get(key) or "")
            for result in results.values()
            for key in ("error", "error_description", "body_preview")
        ).casefold()
        if "request_context_body_http_referrer_missing" in combined:
            return (
                "Rakuten App ID approval或API权限问题：应用要求HTTP Referrer但服务端请求未获允许；"
                "请在Rakuten应用后台核对来源限制/审批，不再盲改密钥传递代码"
            )
        if any(token in combined for token in ("mismatch", "does not match", "invalid access", "invalid application")):
            return "applicationId/accessKey不匹配"
        if query_status == header_status == genre_status == 403:
            return "Rakuten App ID approval或API权限问题：Item query/Header 与 Genre 全部403，不再盲改请求代码"
        if genre_status == 200 and query_status in {401, 403} and header_status in {401, 403}:
            return "Rakuten Item Search API权限问题：Genre认证成功但Item两种模式均被拒绝"
        if query_status is None and header_status is None and genre_status is None:
            return "请求代码或网络错误：三项请求均未获得HTTP响应"
        return "Rakuten认证失败：请依据三项真实响应核对applicationId/accessKey匹配及应用审批权限"

    def diagnose_credentials(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        application_id = clean_env_value(os.getenv("JBA_RAKUTEN_APPLICATION_ID"))
        access_key = clean_env_value(os.getenv("JBA_RAKUTEN_ACCESS_KEY"))
        key_hint = self._key_hint(application_id, access_key) if application_id and access_key else ""
        if not application_id or not access_key:
            return ProviderResponse(
                "unconfigured", message="配置未读取：applicationId或accessKey为空", error_code="UNCONFIGURED",
                diagnostics={
                    "credential_source": key_hint,
                    "rakuten_final_judgment": "配置未读取",
                    "rakuten_item_self_check": self._credential_diagnostics(application_id, access_key),
                },
            )
        common = {
            "applicationId": application_id,
            "keyword": jan,
            "format": "json",
            "formatVersion": 2,
            "hits": 1,
        }
        query_result, query_response = self._diagnostic_request(
            label="Item Search query", url=self.item_endpoint,
            params={**common, "accessKey": access_key}, timeout_seconds=timeout_seconds,
            application_id=application_id, access_key=access_key,
        )
        header_result, header_response = self._diagnostic_request(
            label="Item Search Header", url=self.item_endpoint,
            params=common, headers={"accessKey": access_key}, timeout_seconds=timeout_seconds,
            application_id=application_id, access_key=access_key,
        )
        genre_result, _ = self._diagnostic_request(
            label="Genre Search query", url=self.genre_endpoint,
            params={"applicationId": application_id, "accessKey": access_key, "genreId": "0", "format": "json"},
            timeout_seconds=timeout_seconds, application_id=application_id, access_key=access_key,
        )
        results = {"item_query": query_result, "item_header": header_result, "genre_query": genre_result}
        judgment = self._rakuten_judgment(results)
        successful_response = query_response if query_result["http_status"] == 200 else (
            header_response if header_result["http_status"] == 200 else None
        )
        if query_result["http_status"] == 200:
            type(self)._working_access_key_mode = "query"
        elif header_result["http_status"] == 200:
            type(self)._working_access_key_mode = "header"
        offers: list[PriceCandidate] = []
        if successful_response is not None:
            try:
                offers = self._item_offers(successful_response.json(), jan)
            except ValueError:
                offers = []
        diagnostics = {
            "credential_source": key_hint,
            "rakuten_item_self_check": self._credential_diagnostics(application_id, access_key),
            "rakuten_auth_tests": results,
            "rakuten_final_judgment": judgment,
        }
        if successful_response is not None:
            return ProviderResponse(
                "success" if offers else "empty", tuple(offers),
                message=judgment, error_code=None if offers else "NOT_FOUND",
                http_status=successful_response.status_code, diagnostics=diagnostics,
            )
        statuses = [result.get("http_status") for result in results.values()]
        http_status = next((status for status in statuses if status is not None), None)
        return ProviderResponse(
            "error", message=judgment,
            error_code="AUTH_FAILED" if any(status in {401, 403} for status in statuses) else "REQUEST_FAILED",
            http_status=http_status, diagnostics=diagnostics,
        )

    def _product_offers(self, payload: dict[str, Any], jan: str) -> list[PriceCandidate]:
        offers: list[PriceCandidate] = []
        rows = payload.get("products") or payload.get("Products") or []
        for wrapped in rows:
            item = wrapped.get("product", wrapped.get("Product", wrapped)) if isinstance(wrapped, dict) else {}
            title = str(item.get("productName") or "").strip()
            caption = str(item.get("productCaption") or "")
            url = str(
                item.get("affiliateUrl")
                or item.get("productUrlPC")
                or item.get("searchUrl")
                or ""
            ).strip()
            if not title or not url:
                continue
            returned_jan = str(item.get("productCode") or "").strip() or None
            jan_verified = returned_jan == jan
            offers.append(PriceCandidate(
                title=title,
                url=url,
                image_url=_first_image(item.get("mediumImageUrl") or item.get("smallImageUrl")),
                seller=None,
                item_price=_as_int(item.get("salesMinPrice") or item.get("minPrice")),
                shipping_price=0,
                shipping_known=False,
                jan=returned_jan or _jan_evidence(" ".join((title, caption, url)), jan),
                stock_status="in_stock" if _as_int(item.get("salesItemCount")) > 0 else "unknown",
                condition="new",
                listing_type="single",
                brand=str(item.get("brandName") or "").strip() or None,
                link_type="product" if item.get("productUrlPC") else "search",
                jan_verified=jan_verified,
                match_type="EXACT_JAN" if jan_verified else "UNVERIFIED",
                confidence=1.0 if jan_verified else 0.0,
                fetched_at=datetime.now(timezone.utc),
                raw_data=item,
            ))
        return offers

    def _item_offers(self, payload: dict[str, Any], jan: str) -> list[PriceCandidate]:
        offers: list[PriceCandidate] = []
        rows = payload.get("Items") or payload.get("items") or []
        for wrapped in rows:
            item = wrapped.get("Item", wrapped.get("item", wrapped)) if isinstance(wrapped, dict) else {}
            title = str(item.get("itemName") or item.get("title") or "").strip()
            caption = str(item.get("itemCaption") or "")
            url = str(item.get("affiliateUrl") or item.get("itemUrl") or "").strip()
            if not title or not url:
                continue
            evidence = _jan_evidence(" ".join((
                title,
                caption,
                str(item.get("itemCode") or ""),
                url,
            )), jan)
            jan_verified = False
            postage_flag = item.get("postageFlag")
            shipping_known = postage_flag in {1, "1"}
            availability = item.get("availability")
            if availability in {0, "0", False}:
                stock_status = "out_of_stock"
            elif availability in {1, "1", True}:
                stock_status = "in_stock"
            else:
                stock_status = "unknown"
            offers.append(PriceCandidate(
                title=title,
                url=url,
                image_url=_first_image(item.get("mediumImageUrls") or item.get("smallImageUrls")),
                seller=str(item.get("shopName") or item.get("shopCode") or "").strip() or None,
                item_price=_as_int(item.get("itemPrice")),
                shipping_price=0,
                shipping_known=shipping_known,
                jan=evidence,
                stock_status=stock_status,
                condition="new",
                listing_type="single",
                brand=str(item.get("genreId") or "").strip() or None,
                link_type="product",
                jan_verified=jan_verified,
                match_type="UNVERIFIED",
                confidence=0.35,
                fetched_at=datetime.now(timezone.utc),
                raw_data=item,
            ))
        return offers

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        application_id = clean_env_value(os.getenv("JBA_RAKUTEN_APPLICATION_ID"))
        access_key = clean_env_value(os.getenv("JBA_RAKUTEN_ACCESS_KEY"))
        key_hint = self._key_hint(application_id, access_key) if application_id and access_key else ""
        if not application_id or not access_key:
            return ProviderResponse(
                "unconfigured",
                message="API未配置，已保留手动核对/平台搜索链接",
                search_url=self.search_link(jan),
                error_code="UNCONFIGURED",
                diagnostics={
                    "credential_source": key_hint,
                    "rakuten_item_self_check": self._credential_diagnostics(application_id, access_key),
                },
            )
        item_params: dict[str, Any] = {
            "applicationId": application_id,
            "keyword": jan,
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            "imageFlag": 1,
        }
        item_headers: dict[str, str] | None = None
        if type(self)._working_access_key_mode == "header":
            item_headers = {"accessKey": access_key}
        else:
            item_params["accessKey"] = access_key
        affiliate_id = clean_env_value(os.getenv("JBA_RAKUTEN_AFFILIATE_ID"))
        if affiliate_id:
            item_params["affiliateId"] = affiliate_id
        product_response: httpx.Response | None = None
        product_offers: list[PriceCandidate] = []
        product_error = None
        product_error_fields: dict[str, Any] = {}
        product_trace: dict[str, Any] = {}
        product_enabled = self._product_search_enabled()
        if product_enabled:
            product_params = {
                "applicationId": application_id,
                "accessKey": access_key,
                "productCode": jan,
                "format": "json",
                "formatVersion": 2,
                "hits": 30,
            }
            if affiliate_id:
                product_params["affiliateId"] = affiliate_id
            product_headers = self._rakuten_headers()
            product_response = self._get_once(self.product_endpoint, product_params, timeout_seconds)
            product_trace = self._response_trace(
                product_response,
                application_id=application_id,
                access_key=access_key,
                expected_headers=product_headers,
            )
            if product_response.status_code == 200:
                product_payload = product_response.json()
                product_offers = self._product_offers(product_payload, jan)
                product_error = None
            else:
                product_error = f"HTTP_{product_response.status_code}"
                product_error_fields = self._error_fields(product_response)
                product_trace.update(self._rakuten_ip_diagnostics(product_response, product_error_fields))
        item_expected_headers = self._rakuten_headers(item_headers)
        item_response = self._get_once(self.item_endpoint, item_params, timeout_seconds, headers=item_headers)
        if item_response.status_code in {401, 403}:
            fallback_params = dict(item_params)
            fallback_headers: dict[str, str] | None
            if item_headers:
                fallback_params["accessKey"] = access_key
                fallback_headers = None
                fallback_mode = "query"
            else:
                fallback_params.pop("accessKey", None)
                fallback_headers = {"accessKey": access_key}
                fallback_mode = "header"
            fallback_response = self._get_once(
                self.item_endpoint, fallback_params, timeout_seconds, headers=fallback_headers,
            )
            if fallback_response.status_code == 200:
                item_response = fallback_response
                item_expected_headers = self._rakuten_headers(fallback_headers)
                type(self)._working_access_key_mode = fallback_mode
        elif item_response.status_code == 200:
            type(self)._working_access_key_mode = "header" if item_headers else "query"
        item_trace = self._response_trace(
            item_response,
            application_id=application_id,
            access_key=access_key,
            expected_headers=item_expected_headers,
        )
        item_offers: list[PriceCandidate] = []
        item_error = None
        item_error_fields: dict[str, Any] = {}
        if item_response.status_code == 200:
            item_payload = item_response.json()
            item_offers = self._item_offers(item_payload, jan)
        else:
            item_error = f"HTTP_{item_response.status_code}"
            item_error_fields = self._error_fields(item_response)
            item_trace.update(self._rakuten_ip_diagnostics(item_response, item_error_fields))
        offers = product_offers + item_offers
        diagnostics = {
            "credential_source": key_hint,
            "endpoint_strategy": "item_search_only" if not product_enabled else "item_search_plus_product_search",
            "rakuten_item_self_check": {
                **self._credential_diagnostics(application_id, access_key),
                "http_status": item_response.status_code,
                "error": item_error_fields.get("error"),
                "error_description": item_error_fields.get("error_description"),
            },
            "product_search": {
                "enabled": product_enabled,
                "http_status": product_response.status_code if product_response is not None else None,
                "result_count": len(product_offers),
                "error": product_error_fields.get("error") or product_error,
                "error_description": product_error_fields.get("error_description"),
                "errorCode": product_error_fields.get("errorCode"),
                "errorMessage": product_error_fields.get("errorMessage"),
                **product_trace,
            },
            "item_search": {
                "endpoint": self.item_endpoint,
                "api_version": self.item_api_version,
                "http_status": item_response.status_code,
                "result_count": len(item_offers),
                "error": item_error_fields.get("error") or item_error,
                "error_description": item_error_fields.get("error_description"),
                "errorCode": item_error_fields.get("errorCode"),
                "errorMessage": item_error_fields.get("errorMessage"),
                **item_trace,
            },
        }
        if item_response.status_code == 429:
            retry_after = self._retry_after_seconds(item_response)
            diagnostics["item_search"]["retry_after_seconds"] = retry_after
            diagnostics["cooldown_decision"] = {
                "provider": self.code,
                "endpoint": "item_search",
                "retry": False,
                "cooldown_seconds": retry_after or 60,
            }
            return ProviderResponse(
                "error",
                tuple(),
                message="Rakuten Item Search 触发限流，稍后再试",
                search_url=self.search_link(jan),
                error_code="RATE_LIMITED",
                http_status=429,
                diagnostics=diagnostics,
            )
        if item_error and not offers:
            error_code = "AUTH_FAILED" if item_response.status_code in {401, 403} else item_error
            ip_warning = item_trace.get("rakuten_public_ip_warning")
            return ProviderResponse(
                "error",
                tuple(),
                message=str(ip_warning) if ip_warning else (
                    "Rakuten Item Search 认证失败" if error_code == "AUTH_FAILED" else "Rakuten Item Search HTTP 请求失败"
                ),
                search_url=self.search_link(jan),
                error_code=error_code,
                http_status=item_response.status_code,
                diagnostics=diagnostics,
            )
        if product_response is not None and product_error and not offers and product_response.status_code in {401, 403}:
            return ProviderResponse(
                "error",
                tuple(),
                message="Rakuten Product Search 认证失败",
                search_url=self.search_link(jan),
                error_code="AUTH_FAILED",
                http_status=product_response.status_code,
                diagnostics=diagnostics,
            )
        return ProviderResponse(
            "success" if offers else "empty",
            tuple(offers),
            None if offers else ("；".join(filter(None, (product_error, item_error))) or "Rakuten 未返回结果"),
            search_url=self.search_link(jan),
            error_code=None if offers else "NOT_FOUND",
            http_status=item_response.status_code,
            diagnostics=diagnostics,
        )


class YahooShoppingPriceProvider(PriceProvider):
    code = "yahoo_shopping"
    display_name = "Yahoo Shopping"
    base_url = "https://shopping.yahoo.co.jp/"
    endpoint = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"

    def __init__(self, client: Any | None = None):
        self.client = client

    def is_configured(self) -> bool:
        return bool(os.getenv("JBA_YAHOO_CLIENT_ID", "").strip())

    def search_link(self, jan: str) -> str:
        return f"https://shopping.yahoo.co.jp/search?p={quote_plus(jan)}"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        client_id = os.getenv("JBA_YAHOO_CLIENT_ID", "").strip()
        if not client_id:
            return ProviderResponse(
                "unconfigured",
                message="API未配置，已保留手动核对/平台搜索链接",
                search_url=self.search_link(jan),
                error_code="UNCONFIGURED",
            )
        params = {
            "appid": client_id,
            "jan_code": jan,
            "results": 20,
            "sort": "+price",
            "condition": "new",
            "image_size": 600,
        }
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(self.endpoint, params=params)
        else:
            response = self.client.get(self.endpoint, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        offers: list[PriceCandidate] = []
        for item in payload.get("hits", []):
            title = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title or not url:
                continue
            shipping_code = (item.get("shipping") or {}).get("code")
            subscription = bool(SUBSCRIPTION_PATTERN.search(title))
            returned_jan = str(item.get("janCode") or "").strip() or None
            jan_verified = returned_jan == jan
            brand_value = item.get("brand")
            brand = (
                str(brand_value.get("name") or "").strip()
                if isinstance(brand_value, dict)
                else str(brand_value or "").strip()
            )
            image = item.get("image") or {}
            extended_image = item.get("exImage") or {}
            if item.get("inStock") is True:
                stock_status = "in_stock"
            elif item.get("inStock") is False:
                stock_status = "out_of_stock"
            else:
                stock_status = "unknown"
            offers.append(PriceCandidate(
                title=title,
                url=url,
                image_url=extended_image.get("url") or image.get("medium") or image.get("small"),
                seller=(item.get("seller") or {}).get("name"),
                item_price=_as_int(item.get("price")),
                shipping_price=0,
                shipping_known=shipping_code in {2, "2"},
                jan=returned_jan,
                stock_status=stock_status,
                condition=str(item.get("condition") or "new"),
                listing_type="subscription" if subscription else "single",
                brand=brand or None,
                link_type="product",
                jan_verified=jan_verified,
                match_type="EXACT_JAN" if jan_verified else "UNVERIFIED",
                confidence=1.0 if jan_verified else 0.0,
                fetched_at=datetime.now(timezone.utc),
                raw_data=item,
            ))
        return ProviderResponse(
            "success" if offers else "empty",
            tuple(offers),
            None if offers else "Yahoo Shopping 未返回结果",
            search_url=self.search_link(jan),
            error_code=None if offers else "NOT_FOUND",
            http_status=response.status_code,
        )


class LocalQinsiPriceProvider(PriceProvider):
    code = "local_qinsi"
    display_name = "Local/Qinsi"
    base_url = None

    def __init__(self, session: Session):
        self.session = session

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        resolution = resolve_local_product_by_jan(self.session, jan)
        if resolution.is_conflict:
            labels = "、".join(
                product_display_label(product)
                for product in resolution.candidate_products
            )
            return ProviderResponse(
                "error",
                message=f"本地 JAN 对应多个商品，需人工处理：{labels}",
                error_code="AMBIGUOUS",
            )
        product = resolution.product
        if product is None:
            return ProviderResponse("empty", message="本地商品和秦丝条码均未命中", error_code="NOT_FOUND")
        price = _as_int(product.purchase_price)
        candidate = PriceCandidate(
            title=product_display_label(product),
            url=f"/products/{product.id}",
            item_price=price,
            shipping_price=0,
            shipping_known=True,
            image_url=preferred_product_image_url(product),
            seller="本地/秦丝",
            jan=jan,
            stock_status="unknown",
            brand=product.brand,
            link_type="product",
            jan_verified=True,
            match_type="EXACT_JAN",
            confidence=1.0,
            fetched_at=datetime.now(timezone.utc),
            raw_data={"internal_sku": product.internal_sku, "qinsi_product_code": product.qinsi_product_code},
        )
        return ProviderResponse("success", (candidate,))


class AmazonCreatorsPriceProvider(PriceProvider):
    code = "amazon_creators"
    display_name = "Amazon Creators API"
    base_url = "https://www.amazon.co.jp/"

    def is_configured(self) -> bool:
        return all(
            os.getenv(name, "").strip()
            for name in (
                "JBA_AMAZON_CREATORS_PUBLIC_KEY",
                "JBA_AMAZON_CREATORS_PRIVATE_KEY",
                "JBA_AMAZON_JP_PARTNER_TAG",
                "JBA_AMAZON_JP_MARKETPLACE",
            )
        )

    def search_link(self, jan: str) -> str:
        tag = os.getenv("JBA_AMAZON_JP_PARTNER_TAG", "").strip()
        suffix = f"&tag={quote_plus(tag)}" if tag else ""
        return f"https://www.amazon.co.jp/s?k={quote_plus(jan)}{suffix}"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        if not self.is_configured():
            return ProviderResponse(
                "unconfigured",
                message="API未配置，已保留手动核对/平台搜索链接",
                search_url=self.search_link(jan),
                error_code="UNCONFIGURED",
            )
        # P0 only reserves the Creators API boundary. It intentionally never scrapes Amazon pages.
        return ProviderResponse(
            "manual_only",
            message="Creators API 调用器待授权后启用；当前结果为 SEARCH_ONLY，JAN 未验证",
            search_url=self.search_link(jan),
            error_code="SEARCH_ONLY",
        )


class WebFallbackPriceProvider(PriceProvider):
    code = "web_fallback"
    display_name = "Web Fallback"
    base_url = "https://duckduckgo.com/html/"
    user_agent = "JapanBuyingAgent/1.0"

    def __init__(self, client: Any | None = None):
        self.client = client

    def search_link(self, jan: str) -> str:
        return f"https://duckduckgo.com/html/?q={quote_plus('\"' + jan + '\"')}"

    def _get(self, url: str, timeout_seconds: float, **kwargs) -> httpx.Response:
        headers = {"User-Agent": self.user_agent, **kwargs.pop("headers", {})}
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
                return client.get(url, headers=headers, **kwargs)
        return self.client.get(url, timeout=timeout_seconds, follow_redirects=True, headers=headers, **kwargs)

    def _search_links(self, jan: str, timeout_seconds: float) -> list[str]:
        response = self._get(
            self.base_url,
            timeout_seconds,
            params={"q": f'"{jan}"', "kl": "jp-jp"},
        )
        response.raise_for_status()
        parser = _SearchResultParser(str(response.url))
        parser.feed(response.text)
        seen: set[str] = set()
        trusted: list[tuple[int, int, str]] = []
        for index, url in enumerate(parser.links):
            clean = url.split("#", 1)[0]
            rank = _trusted_web_rank(clean)
            if rank is None or clean in seen:
                continue
            seen.add(clean)
            trusted.append((rank, index, clean))
        return [url for _, _, url in sorted(trusted)[:5]]

    def _candidate_from_page(self, jan: str, url: str, timeout_seconds: float) -> PriceCandidate | None:
        response = self._get(url, timeout_seconds)
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.casefold():
            return None
        parser = _ProductPageParser()
        parser.feed(response.text[:500_000])
        json_products = _json_ld_products(parser)
        json_product = _json_ld_product_for_jan(json_products, jan)
        page_jans = JAN_PATTERN.findall(parser.text)
        json_jans = JAN_PATTERN.findall(json.dumps(json_product or {}, ensure_ascii=False))
        if jan not in set(page_jans + json_jans):
            return None
        host = _host(str(response.url))
        title = _official_page_title(parser, json_product, host)
        if not title:
            return None
        image = _official_page_image(parser, json_product, host)
        image_url = urljoin(str(response.url), image) if image else None
        price = _json_ld_price(json_product)
        if price is None:
            price = _extract_price(parser.text)
        spec_text = extract_spec_text(parser.text)
        return PriceCandidate(
            title=title,
            url=str(response.url),
            image_url=image_url,
            seller=host or "Web",
            item_price=price or 0,
            shipping_price=0,
            shipping_known=False,
            jan=jan,
            stock_status="unknown",
            condition="new",
            listing_type="single",
            link_type="product",
            jan_verified=True,
            match_type="EXACT_JAN",
            confidence=0.8 if _trusted_web_rank(str(response.url)) is not None else 0.55,
            fetched_at=datetime.now(timezone.utc),
            raw_data=_official_page_raw_data(
                jan,
                parser,
                json_product,
                host=host,
                source_url=str(response.url),
                price=price,
                image_url=image_url,
                spec_text=spec_text,
            ),
        )

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        if self.client is None and os.getenv("JBA_TESTING") == "1" and os.getenv("JBA_WEB_FALLBACK_ENABLED") is None:
            return ProviderResponse(
                "manual_only",
                message="测试环境未注入 Web fallback client，跳过真实 Web 请求",
                search_url=self.search_link(jan),
                error_code="TESTING_DISABLED",
            )
        try:
            links = self._search_links(jan, timeout_seconds)
        except Exception as exc:
            return ProviderResponse(
                "error",
                message=f"Web 搜索失败：{type(exc).__name__}",
                search_url=self.search_link(jan),
                error_code="WEB_SEARCH_FAILED",
            )
        offers: list[PriceCandidate] = []
        for url in links:
            try:
                candidate = self._candidate_from_page(jan, url, timeout_seconds)
            except Exception:
                candidate = None
            if candidate is not None:
                offers.append(candidate)
            if len(offers) >= MAX_WEB_FALLBACK_OFFERS:
                break
        return ProviderResponse(
            "success" if offers else "empty",
            tuple(offers),
            None if offers else "Web fallback 未找到 JAN 一致的可信商品页",
            search_url=self.search_link(jan),
            error_code=None if offers else "NOT_FOUND",
        )


NISHIMATSUYA_DOMAIN = "24028-net.jp"


class NishimatsuyaPriceProvider(PriceProvider):
    """西松屋 has no public API and its own site (24028-net.jp) could not be
    reached at all while building this (CloudFront returns a country-level
    403 for every path, including robots.txt, from this environment) --
    confirmed a real access barrier, not something to route around. Per the
    agreed fallback plan this reuses the same DuckDuckGo site-scoped search +
    official-page-parsing machinery as WebFallbackPriceProvider (JSON-LD
    first, then the shared price regex), just scoped to this one domain via
    the search query and result-link filtering, and reported under its own
    provider code so results are still attributed to 西松屋 specifically.
    """

    code = "nishimatsuya"
    display_name = "西松屋"
    base_url = "https://www.24028-net.jp/"
    user_agent = "JapanBuyingAgent/1.0"

    def __init__(self, client: Any | None = None):
        self.client = client

    def search_link(self, jan: str) -> str:
        return f"https://www.google.com/search?q={quote_plus(f'site:{NISHIMATSUYA_DOMAIN} {jan}')}"

    def _get(self, url: str, timeout_seconds: float, **kwargs) -> httpx.Response:
        headers = {"User-Agent": self.user_agent, **kwargs.pop("headers", {})}
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
                return client.get(url, headers=headers, **kwargs)
        return self.client.get(url, timeout=timeout_seconds, follow_redirects=True, headers=headers, **kwargs)

    def _search_links(self, jan: str, timeout_seconds: float) -> list[str]:
        response = self._get(
            "https://duckduckgo.com/html/",
            timeout_seconds,
            params={"q": f'site:{NISHIMATSUYA_DOMAIN} "{jan}"', "kl": "jp-jp"},
        )
        response.raise_for_status()
        parser = _SearchResultParser(str(response.url))
        parser.feed(response.text)
        seen: set[str] = set()
        links: list[str] = []
        for url in parser.links:
            clean = url.split("#", 1)[0]
            if NISHIMATSUYA_DOMAIN not in _host(clean) or clean in seen:
                continue
            seen.add(clean)
            links.append(clean)
        return links[:3]

    def _candidate_from_page(self, jan: str, url: str, timeout_seconds: float) -> PriceCandidate | None:
        response = self._get(url, timeout_seconds)
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.casefold():
            return None
        parser = _ProductPageParser()
        parser.feed(response.text[:500_000])
        json_products = _json_ld_products(parser)
        json_product = _json_ld_product_for_jan(json_products, jan)
        page_jans = JAN_PATTERN.findall(parser.text)
        json_jans = JAN_PATTERN.findall(json.dumps(json_product or {}, ensure_ascii=False))
        jan_verified = jan in set(page_jans + json_jans)
        host = _host(str(response.url))
        title = _official_page_title(parser, json_product, host)
        if not title:
            return None
        image = _official_page_image(parser, json_product, host)
        image_url = urljoin(str(response.url), image) if image else None
        # JSON-LD Offer.price is schema.org's own definition of the current
        # transactional price; only fall back to the shared regex (which
        # cannot tell a struck-through original price from the real one as
        # reliably) when a page has no structured data at all.
        price = _json_ld_price(json_product)
        if price is None:
            price = _extract_price(parser.text)
        if price is None:
            return None
        spec_text = extract_spec_text(parser.text)
        return PriceCandidate(
            title=title,
            url=str(response.url),
            image_url=image_url,
            seller="西松屋",
            item_price=price,
            shipping_price=0,
            shipping_known=False,
            jan=jan if jan_verified else None,
            stock_status="unknown",
            condition="new",
            listing_type="single",
            link_type="product",
            jan_verified=jan_verified,
            match_type="EXACT_JAN" if jan_verified else "UNVERIFIED",
            confidence=0.75 if jan_verified else 0.3,
            fetched_at=datetime.now(timezone.utc),
            raw_data=_official_page_raw_data(
                jan, parser, json_product, host=host, source_url=str(response.url),
                price=price, image_url=image_url, spec_text=spec_text,
            ),
        )

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        if self.client is None and os.getenv("JBA_TESTING") == "1" and os.getenv("JBA_WEB_FALLBACK_ENABLED") is None:
            return ProviderResponse(
                "manual_only",
                message="测试环境未注入西松屋查询client，跳过真实Web请求",
                search_url=self.search_link(jan),
                error_code="TESTING_DISABLED",
            )
        try:
            links = self._search_links(jan, timeout_seconds)
        except Exception as exc:
            return ProviderResponse(
                "error",
                message=f"西松屋搜索失败：{type(exc).__name__}",
                search_url=self.search_link(jan),
                error_code="SEARCH_FAILED",
            )
        offers: list[PriceCandidate] = []
        for url in links:
            try:
                candidate = self._candidate_from_page(jan, url, timeout_seconds)
            except Exception:
                candidate = None
            if candidate is not None:
                offers.append(candidate)
        return ProviderResponse(
            "success" if offers else "empty",
            tuple(offers),
            None if offers else "西松屋未找到JAN一致的可信商品页",
            search_url=self.search_link(jan),
            error_code=None if offers else "NOT_FOUND",
        )


class AnpanmanStorePriceProvider(PriceProvider):
    """アンパンマン公式オンラインストア (store.anpanman.jp) is a standard
    Shopify storefront. There is no separate developer API to authenticate
    against, but Shopify's public storefront itself exposes two stable,
    unauthenticated, documented-by-platform-convention surfaces every
    Shopify store has: server-rendered search HTML at /search?q=...&type=product
    (confirmed server-rendered -- no JS needed, robots.txt explicitly marks
    product/search pages crawlable) and a public per-product JSON view at
    /products/{handle}.json (confirmed live: returns title/vendor/variant
    price/compare_at_price/barcode/images with no auth). Searching by JAN
    text was confirmed to return the exact matching product; the JSON's
    variant `barcode` field is then compared against the searched JAN for
    verification, rather than trusting title similarity.
    """

    code = "anpanman_store"
    display_name = "アンパンマン公式オンラインストア"
    base_url = "https://store.anpanman.jp/"
    user_agent = "JapanBuyingAgent/1.0"

    def __init__(self, client: Any | None = None):
        self.client = client

    def search_link(self, jan: str) -> str:
        return f"{self.base_url}search?q={quote_plus(jan)}&type=product"

    def _get(self, url: str, timeout_seconds: float, **kwargs) -> httpx.Response:
        headers = {"User-Agent": self.user_agent, **kwargs.pop("headers", {})}
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
                return client.get(url, headers=headers, **kwargs)
        return self.client.get(url, timeout=timeout_seconds, follow_redirects=True, headers=headers, **kwargs)

    def _search_handles(self, jan: str, timeout_seconds: float) -> list[str]:
        response = self._get(f"{self.base_url}search", timeout_seconds, params={"q": jan, "type": "product"})
        response.raise_for_status()
        handles: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r'href="/products/([a-z0-9_-]+)(?:[?"])', response.text, re.IGNORECASE):
            handle = match.group(1)
            if handle not in seen:
                seen.add(handle)
                handles.append(handle)
        return handles[:5]

    def _product_offer(self, handle: str, jan: str, timeout_seconds: float) -> PriceCandidate | None:
        response = self._get(f"{self.base_url}products/{handle}.json", timeout_seconds)
        if response.status_code != 200:
            return None
        payload = (response.json() or {}).get("product") or {}
        variants = payload.get("variants") or []
        if not variants:
            return None
        # Prefer the variant whose own barcode matches the searched JAN --
        # a multi-variant product (e.g. different colors) can have a
        # different barcode per variant, so variants[0] is not always right.
        variant = next(
            (item for item in variants if str(item.get("barcode") or "").strip() == jan), variants[0],
        )
        title = str(payload.get("title") or "").strip()
        if not title:
            return None
        price = _as_int(variant.get("price"))
        if price <= 0:
            return None
        compare_at_raw = str(variant.get("compare_at_price") or "").strip()
        compare_at_price = _as_int(compare_at_raw) if compare_at_raw else None
        barcode = str(variant.get("barcode") or "").strip() or None
        jan_verified = bool(barcode) and barcode == jan
        images = payload.get("images") or []
        image_url = str(images[0].get("src")) if images and isinstance(images[0], dict) else None
        return PriceCandidate(
            title=title,
            url=f"{self.base_url}products/{handle}",
            image_url=image_url,
            seller=self.display_name,
            item_price=price,
            shipping_price=0,
            shipping_known=False,
            jan=barcode or (jan if jan_verified else None),
            stock_status="unknown",
            condition="new",
            listing_type="single",
            brand=str(payload.get("vendor") or "").strip() or None,
            link_type="product",
            jan_verified=jan_verified,
            match_type="EXACT_JAN" if jan_verified else "UNVERIFIED",
            confidence=1.0 if jan_verified else 0.4,
            fetched_at=datetime.now(timezone.utc),
            raw_data={
                "handle": handle,
                "shopName": self.display_name,
                "seller": self.display_name,
                "brand": payload.get("vendor"),
                "productImageUrl": image_url,
                "compare_at_price": compare_at_price,
                "price": price,
            },
        )

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        if self.client is None and os.getenv("JBA_TESTING") == "1" and os.getenv("JBA_WEB_FALLBACK_ENABLED") is None:
            return ProviderResponse(
                "manual_only",
                message="测试环境未注入アンパンマン查询client，跳过真实Web请求",
                search_url=self.search_link(jan),
                error_code="TESTING_DISABLED",
            )
        try:
            handles = self._search_handles(jan, timeout_seconds)
        except Exception as exc:
            return ProviderResponse(
                "error",
                message=f"アンパンマン公式ストア搜索失败：{type(exc).__name__}",
                search_url=self.search_link(jan),
                error_code="SEARCH_FAILED",
            )
        offers: list[PriceCandidate] = []
        for handle in handles:
            try:
                candidate = self._product_offer(handle, jan, timeout_seconds)
            except Exception:
                candidate = None
            if candidate is not None:
                offers.append(candidate)
        return ProviderResponse(
            "success" if offers else "empty",
            tuple(offers),
            None if offers else "アンパンマン公式ストア未找到匹配商品",
            search_url=self.search_link(jan),
            error_code=None if offers else "NOT_FOUND",
        )


class ManualFallbackPriceProvider(PriceProvider):
    code = "manual"
    display_name = "Manual/Fallback"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse(
            "manual_only",
            message="API未配置，已保留手动核对/平台搜索链接",
            error_code="MANUAL_REQUIRED",
        )


def get_default_price_providers() -> list[PriceProvider]:
    return [
        YahooShoppingPriceProvider(),
        RakutenPriceProvider(),
        AmazonCreatorsPriceProvider(),
        NishimatsuyaPriceProvider(),
        AnpanmanStorePriceProvider(),
        WebFallbackPriceProvider(),
        ManualFallbackPriceProvider(),
    ]
