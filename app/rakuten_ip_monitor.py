from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import rakuten_allowed_public_ip


PUBLIC_IP_CACHE_TTL = timedelta(minutes=10)
IPIFY_URL = "https://api.ipify.org?format=json"


@dataclass(frozen=True, slots=True)
class RakutenPublicIpStatus:
    configured_ip: str | None
    current_public_ip: str | None
    matches: bool | None
    checked_at: datetime | None
    status: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured_ip": self.configured_ip,
            "current_public_ip": self.current_public_ip,
            "matches": self.matches,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "status": self.status,
            "error": self.error,
        }


_cached_status: RakutenPublicIpStatus | None = None


def _valid_ip(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def get_current_public_ip(*, timeout_seconds: float = 4.0, client: Any | None = None) -> str:
    if client is None:
        with httpx.Client(timeout=timeout_seconds) as http:
            response = http.get(IPIFY_URL)
    else:
        response = client.get(IPIFY_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    value = payload.get("ip") if isinstance(payload, dict) else None
    ip = _valid_ip(str(value) if value is not None else None)
    if ip is None:
        raise ValueError("ipify returned an invalid IP address")
    return ip


def rakuten_public_ip_status(
    *,
    force: bool = False,
    timeout_seconds: float = 4.0,
    client: Any | None = None,
) -> RakutenPublicIpStatus:
    global _cached_status
    configured_ip = _valid_ip(rakuten_allowed_public_ip())
    now = datetime.now(timezone.utc)
    if configured_ip is None:
        _cached_status = RakutenPublicIpStatus(None, None, None, now, "not_configured")
        return _cached_status
    if not force and _cached_status and _cached_status.checked_at:
        if _cached_status.configured_ip == configured_ip and now - _cached_status.checked_at < PUBLIC_IP_CACHE_TTL:
            return _cached_status
    try:
        current_ip = get_current_public_ip(timeout_seconds=timeout_seconds, client=client)
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        _cached_status = RakutenPublicIpStatus(configured_ip, None, None, now, "check_failed", type(exc).__name__)
        return _cached_status
    matches = current_ip == configured_ip
    _cached_status = RakutenPublicIpStatus(
        configured_ip,
        current_ip,
        matches,
        now,
        "matched" if matches else "mismatched",
    )
    return _cached_status


def reset_rakuten_public_ip_cache() -> None:
    global _cached_status
    _cached_status = None


def rakuten_ip_warning_message(status: RakutenPublicIpStatus) -> str | None:
    if status.status == "mismatched" and status.current_public_ip:
        return f"当前公网IP已变化，请将Rakuten后台许可IP更新为：{status.current_public_ip}"
    return None
