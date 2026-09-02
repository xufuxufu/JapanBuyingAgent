from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.location_service import (
    DEFAULT_PHYSICAL_LOCATION_CODE, get_default_physical_location,
    initialize_default_locations, list_locations,
)
from app.models import Location, Product, Receipt, ReceiptBatch, ReceiptItem
from app.services import confirm_receipt


# QinSi renamed "千羽"/"招财猫" for the new calendar year; both the 2025 and
# 2026 spellings stay as separate seeded Location rows (see location_service.py).
QINSI_NAMES = {
    "2025千羽", "2025招财猫", "2026千羽", "2026招财猫", "无条码商品", "新日本仓库", "日本家里库存",
}
DEFAULT_LOCATION_COUNT = 11


def test_default_locations_exist_and_repeated_initialization_is_idempotent(db_session):
    first = initialize_default_locations(db_session)
    second = initialize_default_locations(db_session)
    assert len(first) == len(second) == DEFAULT_LOCATION_COUNT
    assert db_session.scalar(select(func.count()).select_from(Location)) == DEFAULT_LOCATION_COUNT
    assert len({location.internal_code for location in second}) == DEFAULT_LOCATION_COUNT


def test_internal_code_is_database_unique(db_session):
    initialize_default_locations(db_session)
    db_session.add(Location(
        internal_code=DEFAULT_PHYSICAL_LOCATION_CODE, display_name="重复编码",
        location_type="local_physical", is_qinsi_warehouse=False, is_active=True, sort_order=999,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_qinsi_names_types_and_default_physical_location_are_exact(db_session):
    locations = initialize_default_locations(db_session)
    qinsi = {location.display_name for location in locations if location.is_qinsi_warehouse}
    assert qinsi == QINSI_NAMES
    assert {location.location_type for location in locations} == {"qinsi_warehouse", "local_physical", "transit", "system_status"}
    by_name = {location.display_name: location for location in locations}
    assert all(by_name[name].location_type == "qinsi_warehouse" for name in QINSI_NAMES - {"日本家里库存"})
    assert by_name["日本家里库存"].location_type == "local_physical"
    default = get_default_physical_location(db_session)
    assert default.internal_code == DEFAULT_PHYSICAL_LOCATION_CODE and default.display_name == "日本家里库存"


def test_disabling_or_renaming_location_preserves_historical_id_reference(db_session):
    initialize_default_locations(db_session)
    location = get_default_physical_location(db_session)
    location_id, internal_code = location.id, location.internal_code
    connection = db_session.connection()
    connection.exec_driver_sql("CREATE TABLE location_history_test (id INTEGER PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES locations(id))")
    connection.exec_driver_sql("INSERT INTO location_history_test (id,location_id) VALUES (1,?)", (location_id,))
    location.display_name = "日本自宅保管"
    location.is_active = False
    db_session.commit()
    retained = db_session.get(Location, location_id)
    linked = db_session.connection().exec_driver_sql("SELECT location_id FROM location_history_test WHERE id=1").scalar_one()
    assert retained is not None and retained.internal_code == internal_code and not retained.is_active
    assert linked == retained.id
    assert retained in list_locations(db_session) and retained not in list_locations(db_session, active_only=True)


def test_location_list_page_and_api_are_read_only_and_return_200(client):
    http, db, _ = client
    initialize_default_locations(db)
    page = http.get("/locations")
    api = http.get("/api/locations")
    assert page.status_code == api.status_code == 200
    assert "位置管理" in page.text and "日本家里库存" in page.text
    assert len(api.json()) == DEFAULT_LOCATION_COUNT
    assert http.delete("/locations/1").status_code in {404, 405}


def test_location_initialization_does_not_change_product_receipt_or_review_flow(db_session):
    product = Product(name_cn="既有商品")
    batch = ReceiptBatch(batch_no="LOC-REGRESSION", status="review", image_status="ready", gpt_status="json_imported")
    receipt = Receipt(batch=batch, raw_store_name="既有店铺", confirmation_status="pending", review_status="pending")
    item = ReceiptItem(receipt=receipt, line_no=1, raw_name="既有商品", product_id=None, quantity=1, confidence=1, review_status="pending", match_status="unmatched")
    db_session.add_all([product, batch, receipt, item])
    db_session.commit()
    product_id, receipt_id, item_id = product.id, receipt.id, item.id
    initialize_default_locations(db_session)
    warnings = confirm_receipt(db_session, batch, receipt)
    assert warnings == []
    assert db_session.get(Product, product_id).name_cn == "既有商品"
    assert db_session.get(Receipt, receipt_id).confirmation_status == "confirmed"
    assert db_session.get(ReceiptItem, item_id).review_status == "confirmed"
