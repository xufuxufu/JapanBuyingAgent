from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx


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
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    status: str
    offers: tuple[PriceCandidate, ...] = ()
    message: str | None = None


class PriceProvider(ABC):
    code: str
    display_name: str
    base_url: str | None

    @abstractmethod
    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        raise NotImplementedError


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
    endpoint = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

    def __init__(self, client: Any | None = None):
        self.client = client

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        application_id = os.getenv("JBA_RAKUTEN_APPLICATION_ID", "").strip()
        access_key = os.getenv("JBA_RAKUTEN_ACCESS_KEY", "").strip()
        if not application_id or not access_key:
            return ProviderResponse("unconfigured", message="Rakuten API 配置缺失")
        params = {
            "applicationId": application_id,
            "accessKey": access_key,
            "keyword": jan,
            "format": "json",
            "formatVersion": 2,
            "hits": 20,
            "sort": "+itemPrice",
            "availability": 0,
            "imageFlag": 1,
            "postageFlag": 1,
            "purchaseType": 0,
            "carrier": 2,
        }
        if self.client is None:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.get(self.endpoint, params=params)
        else:
            response = self.client.get(self.endpoint, params=params, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        offers: list[PriceCandidate] = []
        for wrapped in payload.get("items", []):
            item = wrapped.get("item", wrapped) if isinstance(wrapped, dict) else {}
            title = str(item.get("itemName") or "").strip()
            caption = str(item.get("itemCaption") or "")
            url = str(item.get("itemUrl") or "").strip()
            if not title or not url:
                continue
            subscription = bool(SUBSCRIPTION_PATTERN.search(f"{title} {caption}"))
            postage_flag = item.get("postageFlag", 0)
            offers.append(PriceCandidate(
                title=title,
                url=url,
                image_url=_first_image(item.get("mediumImageUrls") or item.get("smallImageUrls")),
                seller=str(item.get("shopName") or "").strip() or None,
                item_price=_as_int(item.get("itemPrice")),
                shipping_price=0,
                shipping_known=postage_flag in {0, "0", None},
                jan=_jan_evidence(f"{title} {caption}", jan),
                stock_status="in_stock" if item.get("availability", 1) in {1, "1", True} else "out_of_stock",
                condition="new",
                listing_type="subscription" if subscription else "single",
                raw_data=item,
            ))
        return ProviderResponse("success" if offers else "empty", tuple(offers), None if offers else "Rakuten 未返回结果")


class YahooShoppingPriceProvider(PriceProvider):
    code = "yahoo_shopping"
    display_name = "Yahoo Shopping"
    base_url = "https://shopping.yahoo.co.jp/"
    endpoint = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"

    def __init__(self, client: Any | None = None):
        self.client = client

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        client_id = os.getenv("JBA_YAHOO_CLIENT_ID", "").strip()
        if not client_id:
            return ProviderResponse("unconfigured", message="Yahoo Shopping API 配置缺失")
        params = {
            "appid": client_id,
            "jan_code": jan,
            "hits": 20,
            "sort": "+price",
            "condition": "new",
            "shipping": "free",
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
            offers.append(PriceCandidate(
                title=title,
                url=url,
                image_url=(item.get("image") or {}).get("medium") or (item.get("image") or {}).get("small"),
                seller=(item.get("seller") or {}).get("name"),
                item_price=_as_int(item.get("price")),
                shipping_price=0,
                shipping_known=shipping_code in {2, "2"},
                jan=str(item.get("janCode") or "").strip() or None,
                stock_status="in_stock" if item.get("inStock") is True else "out_of_stock",
                condition=str(item.get("condition") or "new"),
                listing_type="subscription" if subscription else "single",
                raw_data=item,
            ))
        return ProviderResponse("success" if offers else "empty", tuple(offers), None if offers else "Yahoo Shopping 未返回结果")


class ManualFallbackPriceProvider(PriceProvider):
    code = "manual"
    display_name = "Manual/Fallback"
    base_url = None

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        return ProviderResponse("manual_only", message="当前可手动核对价格，自动录入留待后续阶段")


def get_default_price_providers() -> list[PriceProvider]:
    return [RakutenPriceProvider(), YahooShoppingPriceProvider(), ManualFallbackPriceProvider()]
