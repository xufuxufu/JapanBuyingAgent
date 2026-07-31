from __future__ import annotations

import os
import re
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from sqlalchemy.orm import Session

from app.config import clean_env_value, rakuten_http_referer
from app.local_product import resolve_local_product_by_jan
from app.product_image_localization import preferred_product_image_url
from app.rakuten_ip_monitor import rakuten_ip_warning_message, rakuten_public_ip_status


SUBSCRIPTION_PATTERN = re.compile(r"定期(?:購入|便)|サブスク|subscription", re.IGNORECASE)
JAN_PATTERN = re.compile(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)")


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
            "image_size": 300,
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
                product.display_name or product.name_cn or product.name_ja or product.internal_sku
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
            title=product.display_name or product.name_cn or product.name_ja or product.internal_sku,
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
        ManualFallbackPriceProvider(),
    ]
