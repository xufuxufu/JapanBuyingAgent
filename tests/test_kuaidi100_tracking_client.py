from __future__ import annotations

import pytest

from app.kuaidi100_tracking_client import (
    Kuaidi100ClientError,
    event_hash,
    parse_tracking_response,
    query_zhongtong_tracking,
)


def test_ischeck_1_is_delivered_and_terminal_regardless_of_state():
    body = {"message": "ok", "status": "200", "ischeck": "1", "state": "3", "com": "zhongtong", "data": [
        {"time": "2026-08-28 20:53:03", "context": "已签收", "areaCode": "CN420100000000", "areaName": "湖北,武汉市", "status": "签收"},
    ]}
    result = parse_tracking_response(body, tracking_no="70000000000001", carrier="zhongtong")
    assert result.tracking_status == "delivered"
    assert result.terminal is True
    assert len(result.events) == 1
    assert result.events[0].description == "已签收"
    assert result.events[0].area_name == "湖北,武汉市"


@pytest.mark.parametrize("state,expected", [
    ("0", "in_transit"),
    ("1", "in_transit"),
    ("5", "out_for_delivery"),
    ("2", "exception"),
    ("4", "exception"),
    ("6", "exception"),
    ("14", "exception"),
    ("99", "unknown"),
])
def test_non_terminal_state_codes_map_to_internal_status(state, expected):
    body = {"message": "ok", "status": "200", "ischeck": "0", "state": state, "com": "zhongtong", "data": [
        {"time": "2026-08-27 09:20:55", "context": "运输中", "areaCode": None, "areaName": None, "status": "在途"},
    ]}
    result = parse_tracking_response(body, tracking_no="x", carrier="zhongtong")
    assert result.tracking_status == expected
    assert result.terminal is False


def test_no_events_and_not_delivered_is_no_info():
    body = {"message": "ok", "status": "200", "ischeck": "0", "com": "zhongtong", "data": []}
    result = parse_tracking_response(body, tracking_no="x", carrier="zhongtong")
    assert result.tracking_status == "no_info"
    assert result.terminal is False
    assert result.events == []


def test_events_sorted_descending_by_time():
    body = {"message": "ok", "status": "200", "ischeck": "0", "state": "0", "com": "zhongtong", "data": [
        {"time": "2026-08-27 00:00:00", "context": "早", "areaCode": None, "areaName": None, "status": "在途"},
        {"time": "2026-08-28 00:00:00", "context": "晚", "areaCode": None, "areaName": None, "status": "在途"},
    ]}
    result = parse_tracking_response(body, tracking_no="x", carrier="zhongtong")
    assert [ev.description for ev in result.events] == ["晚", "早"]


def test_api_failure_response_raises_client_error():
    body = {"message": "单号不存在", "status": "500", "result": False}
    with pytest.raises(Kuaidi100ClientError) as excinfo:
        parse_tracking_response(body, tracking_no="x", carrier="zhongtong")
    assert excinfo.value.category == "api_error"
    assert "单号不存在" in excinfo.value.message


def test_event_hash_stable_and_sensitive_to_content():
    from datetime import datetime, timezone
    t = datetime(2026, 8, 28, 20, 53, 3, tzinfo=timezone.utc)
    h1 = event_hash(t, "签收", "已签收")
    h2 = event_hash(t, "签收", "已签收")
    h3 = event_hash(t, "签收", "已揽收")
    assert h1 == h2
    assert h1 != h3


def test_missing_tracking_no_raises_before_any_request():
    with pytest.raises(Kuaidi100ClientError) as excinfo:
        query_zhongtong_tracking("", "13800000000")
    assert excinfo.value.category == "missing_tracking_no"


def test_missing_phone_raises_before_any_request():
    with pytest.raises(Kuaidi100ClientError) as excinfo:
        query_zhongtong_tracking("70000000000001", "")
    assert excinfo.value.category == "missing_phone"


def test_unconfigured_credentials_raise(monkeypatch):
    monkeypatch.delenv("KUAIDI100_KEY", raising=False)
    monkeypatch.delenv("KUAIDI100_CUSTOMER", raising=False)
    with pytest.raises(Kuaidi100ClientError) as excinfo:
        query_zhongtong_tracking("70000000000001", "13800000000")
    assert excinfo.value.category == "unconfigured"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data, timeout))
        return _FakeResponse(self.payload)


def test_query_zhongtong_tracking_signs_and_parses_via_injected_client(monkeypatch):
    monkeypatch.setenv("KUAIDI100_KEY", "test-key")
    monkeypatch.setenv("KUAIDI100_CUSTOMER", "test-customer")
    fake = _FakeClient({"message": "ok", "status": "200", "ischeck": "1", "state": "3", "com": "zhongtong", "data": []})
    result = query_zhongtong_tracking("70000000000001", "13800000000", client=fake)
    assert result.terminal is True
    assert len(fake.calls) == 1
    url, data, _timeout = fake.calls[0]
    assert url == "https://poll.kuaidi100.com/poll/query.do"
    assert data["customer"] == "test-customer"
    assert "sign" in data and len(data["sign"]) == 32  # MD5 hex, uppercased
    assert data["sign"] == data["sign"].upper()
    import json as _json
    param = _json.loads(data["param"])
    assert param["com"] == "zhongtong"
    assert param["num"] == "70000000000001"
    assert param["phone"] == "13800000000"


class _RaisingClient:
    def post(self, url, data=None, timeout=None):
        import httpx
        raise httpx.ConnectError("boom")


def test_network_error_wraps_into_client_error(monkeypatch):
    monkeypatch.setenv("KUAIDI100_KEY", "test-key")
    monkeypatch.setenv("KUAIDI100_CUSTOMER", "test-customer")
    with pytest.raises(Kuaidi100ClientError) as excinfo:
        query_zhongtong_tracking("70000000000001", "13800000000", client=_RaisingClient())
    assert excinfo.value.category == "network_error"
