from __future__ import annotations

from dataclasses import dataclass

import app.price_service as price_service
from app.models import Product
from app.price_providers import PriceCandidate, PriceProvider, ProviderResponse


VALID_JAN = "4901234567894"


@dataclass
class FakeProvider(PriceProvider):
    code: str
    response: ProviderResponse | None = None
    error: Exception | None = None
    display_name: str = "Fake"
    base_url: str | None = "https://example.test/"

    def search(self, jan: str, timeout_seconds: float) -> ProviderResponse:
        if self.error:
            raise self.error
        return self.response or ProviderResponse("empty")


def offer(price: int, *, title: str = "测试商品 100ml", jan: str = VALID_JAN) -> PriceCandidate:
    return PriceCandidate(
        title=title, url=f"https://example.test/{price}", image_url=f"https://img.test/{price}.jpg",
        seller="测试店", item_price=price, shipping_price=0, jan=jan, stock_status="in_stock",
        jan_verified=True,
    )


def test_api_lookup_success_returns_full_json_payload(client, monkeypatch):
    http, db, _ = client
    fake = FakeProvider("fake_success", ProviderResponse("success", (offer(1000),)))
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: [fake])

    response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["jan"] == VALID_JAN
    assert data["online_min_price_text"] == "¥1,000"
    assert data["result_url"] == f"/price-check/results/{data['history_id']}"
    assert len(data["offers"]) == 1
    assert data["offers"][0]["title"] == "测试商品 100ml"
    assert data["attempts"] == [{
        "provider_code": "fake_success", "status": "success", "status_label": "查询成功",
        "message": None, "result_count": 1,
    }]
    assert data["product"] is None  # no local product exists for this JAN yet
    assert data["providers_all_failed"] is False
    assert data["providers_partial_failed"] is False
    assert data["provider_banner"] is None


def test_api_lookup_invalid_jan_returns_422(client):
    http, db, _ = client
    response = http.post("/api/price-check/lookup", json={"jan": "not-a-jan"})
    assert response.status_code == 422
    data = response.json()
    assert data["ok"] is False and "JAN" in data["message"]


def test_api_lookup_one_provider_failing_still_returns_ok_true(client, monkeypatch):
    http, db, _ = client
    providers = [
        FakeProvider("boom", error=RuntimeError("boom")),
        FakeProvider("good", ProviderResponse("success", (offer(500),))),
    ]
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: providers)

    response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["providers_partial_failed"] is True
    assert data["providers_all_failed"] is False
    assert data["provider_banner"] == "部分平台查询失败，结果可能不完整"
    statuses = {a["provider_code"]: a["status"] for a in data["attempts"]}
    assert statuses == {"boom": "error", "good": "success"}


def test_api_lookup_all_providers_failing_still_returns_ok_true_with_banner(client, monkeypatch):
    http, db, _ = client
    providers = [FakeProvider("boom1", error=RuntimeError("x")), FakeProvider("boom2", error=RuntimeError("y"))]
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: providers)

    response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["providers_all_failed"] is True
    assert data["provider_banner"] == "查询失败，暂无线上结果"
    assert data["offers"] == []


def test_api_lookup_unexpected_exception_returns_friendly_json_not_traceback(client, monkeypatch):
    http, db, _ = client

    def _boom(*_args, **_kwargs):
        raise RuntimeError("private internal detail")

    monkeypatch.setattr("app.main.query_prices", _boom)

    response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})

    assert response.status_code == 500
    data = response.json()
    assert data == {"ok": False, "message": "查询失败，请稍后重试"}
    assert "private internal detail" not in response.text


def test_api_lookup_with_existing_local_product_includes_product_payload(client, monkeypatch):
    http, db, _ = client
    product = Product(jan=VALID_JAN, name_cn="本地商品", purchase_price=800)
    db.add(product)
    db.commit()
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: [])

    response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": False})

    assert response.status_code == 200
    data = response.json()
    assert data["product"]["id"] == product.id
    assert data["product"]["internal_sku"] == product.internal_sku
    assert data["product"]["watched"] is False


def test_old_result_page_route_still_works_after_json_lookup(client, monkeypatch):
    http, db, _ = client
    fake = FakeProvider("fake_success", ProviderResponse("success", (offer(1234),)))
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: [fake])
    api_response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})
    history_id = api_response.json()["history_id"]

    page = http.get(f"/price-check/results/{history_id}")

    assert page.status_code == 200
    assert f"JAN {VALID_JAN}" in page.text


def test_store_price_route_json_branch_returns_updated_comparison(client, monkeypatch):
    http, db, _ = client
    fake = FakeProvider("fake_success", ProviderResponse("success", (offer(1000),)))
    monkeypatch.setattr(price_service, "get_default_price_providers", lambda: [fake])
    api_response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})
    history_id = api_response.json()["history_id"]

    response = http.post(
        f"/price-check/results/{history_id}/store-price",
        data={"current_store_price": "1500"},
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["current_store_price"] == 1500
    assert data["comparison_status"] == "online_cheaper"
    assert data["comparison_label"] == "线上更便宜"


def test_store_price_route_json_branch_rejects_invalid_value(client):
    http, db, _ = client
    api_response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})
    history_id = api_response.json()["history_id"]

    response = http.post(
        f"/price-check/results/{history_id}/store-price",
        data={"current_store_price": "-5"},
        headers={"X-Requested-With": "fetch"},
    )

    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_store_price_route_without_fetch_header_still_redirects(client):
    # Regression: the classic full-page /price-check/results/{id} form must
    # keep behaving exactly as before for anyone still linking to it directly.
    http, db, _ = client
    api_response = http.post("/api/price-check/lookup", json={"jan": VALID_JAN, "force_refresh": True})
    history_id = api_response.json()["history_id"]

    response = http.post(
        f"/price-check/results/{history_id}/store-price",
        data={"current_store_price": "900"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/price-check/results/{history_id}?price_saved=1"
