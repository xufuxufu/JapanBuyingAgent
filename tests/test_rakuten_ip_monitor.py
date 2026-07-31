from __future__ import annotations

import httpx

import app.price_providers as price_providers
from app.price_providers import RakutenPriceProvider
from app.rakuten_ip_monitor import rakuten_public_ip_status, reset_rakuten_public_ip_cache


class IpifyClient:
    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc
        self.calls = 0

    def get(self, url, timeout):
        self.calls += 1
        if self.exc:
            raise self.exc
        return httpx.Response(200, json={"ip": self.value}, request=httpx.Request("GET", url))


def test_rakuten_public_ip_matched(monkeypatch):
    reset_rakuten_public_ip_cache()
    monkeypatch.setenv("JBA_RAKUTEN_ALLOWED_PUBLIC_IP", "14.10.7.65")
    status = rakuten_public_ip_status(force=True, client=IpifyClient("14.10.7.65"))
    assert status.status == "matched"
    assert status.matches is True
    assert status.configured_ip == "14.10.7.65"


def test_rakuten_public_ip_mismatched(monkeypatch):
    reset_rakuten_public_ip_cache()
    monkeypatch.setenv("JBA_RAKUTEN_ALLOWED_PUBLIC_IP", "14.10.7.65")
    status = rakuten_public_ip_status(force=True, client=IpifyClient("203.0.113.9"))
    assert status.status == "mismatched"
    assert status.matches is False
    assert status.current_public_ip == "203.0.113.9"


def test_ipify_timeout_does_not_raise(monkeypatch):
    reset_rakuten_public_ip_cache()
    monkeypatch.setenv("JBA_RAKUTEN_ALLOWED_PUBLIC_IP", "14.10.7.65")
    status = rakuten_public_ip_status(force=True, client=IpifyClient(exc=httpx.TimeoutException("timeout")))
    assert status.status == "check_failed"
    assert status.current_public_ip is None


def test_rakuten_403_request_context_triggers_public_ip_recheck(monkeypatch):
    calls = []

    def fake_ip_status(force=False):
        calls.append(force)
        class Status:
            status = "mismatched"
            current_public_ip = "203.0.113.9"
            def as_dict(self):
                return {
                    "configured_ip": "14.10.7.65",
                    "current_public_ip": "203.0.113.9",
                    "matches": False,
                    "checked_at": None,
                    "status": "mismatched",
                    "error": None,
                }
        return Status()

    monkeypatch.setattr(price_providers, "rakuten_public_ip_status", fake_ip_status)
    provider = RakutenPriceProvider()
    response = httpx.Response(
        403,
        json={"errorCode": "403", "errorMessage": "REQUEST_CONTEXT_BODY_HTTP_REFERRER_MISSING"},
        request=httpx.Request("GET", provider.item_endpoint, headers={"Referer": "https://xufu-cp.taile96adb.ts.net:8020/", "User-Agent": "JapanBuyingAgent/1.0"}),
    )
    fields = provider._error_fields(response)
    diagnostics = provider._rakuten_ip_diagnostics(response, fields)
    assert calls == [True]
    assert diagnostics["rakuten_public_ip_status"]["status"] == "mismatched"
