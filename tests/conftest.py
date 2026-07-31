from __future__ import annotations

import io
import os
import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import sessionmaker

os.environ["JBA_TESTING"] = "1"

from app.db import Base, build_engine, get_db
from app.main import app
import app.main as main_module
import app.services as services
import app.field_purchase as field_purchase


@pytest.fixture(scope="session")
def db_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("database-template") / "schema.sqlite3"
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def db_session(tmp_path, db_template):
    path = tmp_path / "test.sqlite3"
    shutil.copy2(db_template, path)
    engine = build_engine(f"sqlite:///{path.as_posix()}")
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db_session, tmp_path, monkeypatch):
    original = tmp_path / "uploads" / "original"
    preview = tmp_path / "uploads" / "preview"
    original.mkdir(parents=True)
    preview.mkdir(parents=True)
    monkeypatch.setattr(services, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(services, "ORIGINAL_DIR", original)
    monkeypatch.setattr(services, "PREVIEW_DIR", preview)
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    tag_evidence = tmp_path / "field-purchases" / "tag-evidence"
    tag_evidence.mkdir(parents=True)
    monkeypatch.setattr(main_module, "TAG_EVIDENCE_DIR", tag_evidence)
    monkeypatch.setattr(field_purchase, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(field_purchase, "TAG_EVIDENCE_DIR", tag_evidence)

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client, db_session, tmp_path
    app.dependency_overrides.clear()


def image_bytes(color=(20, 120, 80), size=(80, 120), fmt="JPEG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, fmt)
    return output.getvalue()


@pytest.fixture
def jpeg_bytes():
    return image_bytes()


@pytest.fixture
def valid_payload():
    return {
        "schema_version": "1.0",
        "store": {"raw_name": "测试药妆店", "purchased_at": None},
        "totals": {"subtotal": 1200, "discount_total": 100, "tax_total": 100, "paid_total": 1200},
        "items": [{
            "line_no": 1, "raw_name": "テスト商品", "recognized_name": "测试商品",
            "jan_candidate": "0490123456789", "quantity": 1, "unit_price": 1200,
            "discount_amount": 100, "tax_rate": 0.1, "line_total": 1100, "confidence": 0.9,
        }],
        "warnings": [],
    }
