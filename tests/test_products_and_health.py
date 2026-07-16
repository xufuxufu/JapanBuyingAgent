from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db import get_db
from app.main import app
from app.models import Product


def test_empty_jans_are_legal_and_non_empty_duplicate_is_not(db_session):
    db_session.add_all([Product(jan=None, name_cn="A"), Product(jan=None, name_cn="B"), Product(jan="0490000000001", name_cn="C")])
    db_session.commit()
    assert len(list(db_session.scalars(select(Product).where(Product.jan.is_(None))))) == 2
    db_session.add(Product(jan="0490000000001", name_cn="D"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_api_creates_products_with_and_without_jan_and_generates_unique_skus(client):
    http, db, _ = client
    with_jan = http.post("/api/products", json={"name_cn": "有JAN商品", "jan": "00123457"})
    without_jan = http.post("/api/products", json={"name_cn": "无JAN商品", "jan": None})
    assert with_jan.status_code == without_jan.status_code == 201
    first, second = with_jan.json(), without_jan.json()
    assert first["jan"] == "00123457" and second["jan"] is None
    assert re.fullmatch(r"NJ-\d{8}-\d{6}", first["internal_sku"])
    assert first["internal_sku"] != second["internal_sku"]
    assert len(list(db.scalars(select(Product)))) == 2


def test_later_jan_entry_keeps_internal_sku_and_duplicate_has_clear_conflict(client):
    http, _, _ = client
    first = http.post("/api/products", json={"name_cn": "先无JAN"}).json()
    second = http.post("/api/products", json={"name_cn": "已有JAN", "jan": "12345670"}).json()
    updated = http.patch(f"/api/products/{first['id']}", json={"jan": "00012345600012"})
    assert updated.status_code == 200
    assert updated.json()["internal_sku"] == first["internal_sku"]
    assert updated.json()["jan"] == "00012345600012"
    conflict = http.patch(f"/api/products/{first['id']}", json={"jan": second["jan"]})
    assert conflict.status_code == 409
    assert "JAN" in conflict.json()["detail"] and "不能重复保存" in conflict.json()["detail"]


def test_qinsi_product_code_never_populates_jan(client):
    http, _, _ = client
    response = http.post("/api/products", json={"name_cn": "仅秦丝编码", "qinsi_product_code": "4901872097296"})
    assert response.status_code == 201
    assert response.json()["qinsi_product_code"] == "4901872097296"
    assert response.json()["jan"] is None


def test_health_reports_database_unavailable():
    class BrokenSession:
        def execute(self, _statement):
            raise OperationalError("SELECT 1", {}, Exception("database unavailable"))

    def broken_db():
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_db
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_excel_mapping_document_has_locked_identifier_mappings():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "QINSI_FIELD_MAPPING.md").read_text(encoding="utf-8")
    assert re.search(r"条码\s*→\s*jan", text)
    assert re.search(r"货号\s*→\s*qinsi_product_code", text)
