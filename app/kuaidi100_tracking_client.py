"""Kuaidi100 官方"实时查询"接口客户端 -- Phase 10A, 中通(zhongtong)专用。

Request shape (endpoint, signature, param fields) verified verbatim against
the official demo repo https://github.com/kuaidi100-api/python-demo
(code/synquery.py, fetched 2026-09-05) -- not guessed. That demo signs with
only `key` + `customer`; `secret` is not part of this endpoint's signature.

Terminal (已签收) detection uses ONLY the official `ischeck` field
(0=未签收, 1=已签收) -- never inferred from Chinese status text. Cross-
validated against one real ZTO tracking number during this phase's research,
which returned ischeck="1" together with state="3" (see STATE_CODE_STATUS
below for the state-code mapping, corroborated by that same real response).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_kuaidi100_config

QUERY_URL = "https://poll.kuaidi100.com/poll/query.do"

# Only carrier supported this phase.
SUPPORTED_CARRIER_CODE = "zhongtong"

# Kuaidi100 event timestamps are naive "YYYY-MM-DD HH:MM:SS" strings in
# Beijing local time (UTC+8).
CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

TERMINAL_ISCHECK_VALUES = {"1", 1, True}

# state code -> internal tracking_status. Widely-documented Kuaidi100 table,
# cross-validated (state="3" alongside ischeck="1"=delivered) against a real
# response captured during this phase's research; not independently
# re-verified for every code (only one real API call was permitted this
# phase) -- treat non-"3" mappings as best-effort until more real data is seen.
STATE_CODE_STATUS = {
    "0": "in_transit",       # 在途
    "1": "in_transit",       # 揽收
    "2": "exception",        # 疑难
    "3": "delivered",        # 签收
    "4": "exception",        # 退签
    "5": "out_for_delivery", # 派件
    "6": "exception",        # 退回
    "7": "in_transit",       # 转单
    "8": "in_transit",       # 清关
    "10": "in_transit",      # 待清关
    "14": "exception",       # 拒签
}

TRACKING_STATUS_LABELS = {
    "unknown": "未知",
    "no_info": "暂无轨迹",
    "in_transit": "运输中",
    "out_for_delivery": "派送中",
    "delivered": "已签收",
    "exception": "异常",
}


class Kuaidi100ClientError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True, slots=True)
class TrackingEventData:
    event_time: datetime
    description: str | None
    area_code: str | None
    area_name: str | None
    status: str | None


@dataclass(frozen=True, slots=True)
class TrackingQueryResult:
    tracking_no: str
    carrier: str
    tracking_status: str
    terminal: bool
    events: list[TrackingEventData] = field(default_factory=list)
    raw_message: str | None = None


def get_kuaidi100_client(timeout_seconds: float = 15.0) -> httpx.Client:
    return httpx.Client(timeout=timeout_seconds)


def _parse_event_time(value: str) -> datetime:
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=CHINA_TZ)


def event_hash(event_time: datetime, status: str | None, description: str | None) -> str:
    raw = f"{event_time.isoformat()}|{status or ''}|{description or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_tracking_response(body: dict, *, tracking_no: str, carrier: str) -> TrackingQueryResult:
    return_code = str(body.get("returnCode")) if body.get("returnCode") is not None else None
    api_success = body.get("result") in (True, "true") or body.get("status") == "200" or return_code == "200"
    if not api_success:
        message = body.get("message") or "查询失败"
        raise Kuaidi100ClientError("api_error", f"快递100返回失败：{message}")

    events = [
        TrackingEventData(
            event_time=_parse_event_time(item["time"]),
            description=item.get("context"),
            area_code=item.get("areaCode"),
            area_name=item.get("areaName"),
            status=item.get("status"),
        )
        for item in (body.get("data") or [])
        if item.get("time")
    ]
    events.sort(key=lambda ev: ev.event_time, reverse=True)

    ischeck = body.get("ischeck")
    if ischeck in TERMINAL_ISCHECK_VALUES:
        tracking_status = "delivered"
        terminal = True
    elif not events:
        tracking_status = "no_info"
        terminal = False
    else:
        state = str(body.get("state")) if body.get("state") is not None else None
        tracking_status = STATE_CODE_STATUS.get(state, "unknown")
        terminal = False

    return TrackingQueryResult(
        tracking_no=tracking_no,
        carrier=body.get("com") or carrier,
        tracking_status=tracking_status,
        terminal=terminal,
        events=events,
        raw_message=body.get("message"),
    )


def query_zhongtong_tracking(
    tracking_no: str,
    recipient_phone: str,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = 15.0,
) -> TrackingQueryResult:
    """Single, non-retrying call. Caller is responsible for not looping."""
    tracking_no = (tracking_no or "").strip()
    recipient_phone = (recipient_phone or "").strip()
    if not tracking_no:
        raise Kuaidi100ClientError("missing_tracking_no", "缺少运单号")
    if not recipient_phone:
        raise Kuaidi100ClientError("missing_phone", "缺少收件人手机号，中通查询必须提供")

    config = get_kuaidi100_config()
    if not config.configured:
        raise Kuaidi100ClientError("unconfigured", "快递100未配置（缺少 KUAIDI100_KEY / KUAIDI100_CUSTOMER）")

    param = {
        "com": SUPPORTED_CARRIER_CODE,
        "num": tracking_no,
        "phone": recipient_phone,
        "from": "",
        "to": "",
        "resultv2": "1",
        "show": "0",
        "order": "desc",
    }
    param_str = json.dumps(param)
    sign = hashlib.md5((param_str + config.key + config.customer).encode()).hexdigest().upper()
    request_data = {"customer": config.customer, "param": param_str, "sign": sign}

    owns_client = client is None
    http = client or get_kuaidi100_client(timeout_seconds)
    try:
        response = http.post(QUERY_URL, data=request_data, timeout=timeout_seconds)
    except httpx.TimeoutException as exc:
        raise Kuaidi100ClientError("timeout", "快递100请求超时") from exc
    except httpx.HTTPError as exc:
        raise Kuaidi100ClientError("network_error", f"快递100网络错误：{type(exc).__name__}") from exc
    finally:
        if owns_client:
            http.close()

    try:
        body = response.json()
    except ValueError as exc:
        raise Kuaidi100ClientError("invalid_response", "快递100返回非JSON响应") from exc

    return parse_tracking_response(body, tracking_no=tracking_no, carrier=SUPPORTED_CARRIER_CODE)
