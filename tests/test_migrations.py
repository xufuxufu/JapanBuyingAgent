from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


HEAD_REVISION = "20260825_0037"


def test_migration_from_empty_and_repeat_safe(tmp_path, monkeypatch):
    db_path = tmp_path / "migration.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    assert {"receipt_batches", "receipt_images", "receipts", "receipt_items", "ai_recognition_runs", "products", "product_aliases", "store_brands", "stores", "store_aliases", "locations", "purchase_batches", "purchase_batch_items", "inventory_transactions", "import_jobs", "import_rows", "product_match_logs", "qinsi_export_jobs", "qinsi_export_lines", "qinsi_export_line_sources", "qinsi_purchase_export_jobs", "qinsi_purchase_export_lines", "qinsi_purchase_export_line_sources", "marketplaces", "price_search_runs", "product_offers", "price_provider_attempts", "price_lookup_histories", "price_watch_rules", "price_alerts", "product_watch_configs", "product_watch_recommendations", "duplicate_detection_logs", "zip_package_jobs", "zip_package_items"} <= tables
    assert {"product_watch_snapshots", "product_watch_notifications", "monitor_scheduler_states"} <= tables
    assert {"qinsi_inventory_snapshots", "qinsi_inventory_snapshot_lines", "qinsi_product_mappings"} <= tables
    assert {
        "qinsi_import_batches", "qinsi_goods_import_rows", "qinsi_master_values", "product_barcodes",
        "qinsi_conflict_resolutions",
        "field_purchase_batches", "field_purchase_items", "tag_evidence",
        "durable_background_jobs", "platform_provider_states", "platform_lookup_results",
            "enrichment_audit_logs", "product_operation_logs",
    } <= tables
    assert {"restock_lists", "restock_list_items"} <= tables
    assert {"low_stock_threshold", "unit_name", "weight_kg", "qinsi_brand_master_id"} <= product_columns
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_phase1_preserves_old_data(tmp_path, monkeypatch):
    db_path = tmp_path / "phase1.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260714_0001');
        CREATE TABLE receipt_batches (id INTEGER PRIMARY KEY, batch_no VARCHAR(40) NOT NULL, status VARCHAR(20) NOT NULL, image_count INTEGER NOT NULL, source_type VARCHAR(20) NOT NULL, recognition_engine VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
        CREATE TABLE receipt_images (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, original_filename VARCHAR(255) NOT NULL, stored_filename VARCHAR(100) NOT NULL, original_path TEXT NOT NULL, processed_path TEXT, page_no INTEGER NOT NULL, file_hash VARCHAR(64) NOT NULL, mime_type VARCHAR(100) NOT NULL, file_size INTEGER NOT NULL, width INTEGER, height INTEGER, preprocessing_status VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE receipts (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, raw_store_name VARCHAR(255), purchased_at DATETIME, receipt_number VARCHAR(100), subtotal INTEGER, discount_total INTEGER NOT NULL, tax_total INTEGER, paid_total INTEGER, currency VARCHAR(3) NOT NULL, recognition_status VARCHAR(20) NOT NULL, confirmation_status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, confirmed_at DATETIME);
        INSERT INTO receipt_batches VALUES (1,'RB-OLD','uploaded',1,'mobile','none','2026-07-14','2026-07-14');
        INSERT INTO receipt_images VALUES (1,1,'old.jpg','stored.jpg','data/uploads/original/stored.jpg','data/uploads/preview/stored.jpg',1,'abc','image/jpeg',10,100,200,'previewed','2026-07-14');
        INSERT INTO receipts VALUES (1,1,'旧店',NULL,NULL,100,0,10,110,'JPY','imported','pending','2026-07-14',NULL);
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        image = connection.execute("SELECT original_filename, recognition_source, rotation_degrees, recognition_filename FROM receipt_images WHERE id=1").fetchone()
        receipt = connection.execute("SELECT raw_store_name, confirmation_warning FROM receipts WHERE id=1").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert image[:3] == ("old.jpg", "original", 0)
    assert image[3] == "RCPT-20260714-0900-0001_P01.jpg"
    assert receipt == ("旧店", None)
    assert revision == (HEAD_REVISION,)


def test_sqlite_foreign_keys_enabled(db_session):
    assert db_session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_0036_restores_unique_product_jan_index(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0035-unique-jan.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260820_0035');
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            jan VARCHAR(32),
            qinsi_product_code VARCHAR(100),
            internal_sku VARCHAR(32) NOT NULL
        );
        CREATE INDEX ix_products_jan_not_null ON products (jan) WHERE jan IS NOT NULL;
        INSERT INTO products VALUES (1,'4571609352419','QINSI-A','NJ-A');
        INSERT INTO products VALUES (2,NULL,'QINSI-B','NJ-B');
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")

    with sqlite3.connect(db_path) as connection:
        indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(products)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert indexes["uq_products_jan_not_null"] == 1
    assert "ix_products_jan_not_null" not in indexes
    assert revision == (HEAD_REVISION,)


def test_0036_blocks_when_duplicate_product_jan_remains(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0035-duplicate-jan.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260820_0035');
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            jan VARCHAR(32),
            qinsi_product_code VARCHAR(100),
            internal_sku VARCHAR(32) NOT NULL
        );
        CREATE INDEX ix_products_jan_not_null ON products (jan) WHERE jan IS NOT NULL;
        INSERT INTO products VALUES (1,'4571609352419','QINSI-A','NJ-A');
        INSERT INTO products VALUES (2,'4571609352419',NULL,'NJ-B');
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    with pytest.raises(RuntimeError, match="products\\.jan still has duplicates"):
        command.upgrade(config, "head")

    with sqlite3.connect(db_path) as connection:
        indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(products)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert indexes["ix_products_jan_not_null"] == 0
    assert "uq_products_jan_not_null" not in indexes
    assert revision == ("20260820_0035",)


def test_0033_removes_product_jan_unique_index_and_preserves_product_history(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0031-jan-unique-index.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260819_0031');
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            jan VARCHAR(32),
            qinsi_product_code VARCHAR(100),
            internal_sku VARCHAR(32) NOT NULL
        );
        CREATE UNIQUE INDEX uq_products_internal_sku ON products (internal_sku);
        CREATE UNIQUE INDEX uq_products_jan_not_null ON products (jan) WHERE jan IS NOT NULL;
        CREATE UNIQUE INDEX uq_products_qinsi_code_not_null ON products (qinsi_product_code) WHERE qinsi_product_code IS NOT NULL;
        CREATE TABLE receipt_items (id INTEGER PRIMARY KEY, product_id INTEGER);
        CREATE TABLE purchase_batch_items (id INTEGER PRIMARY KEY, product_id INTEGER);
        INSERT INTO products VALUES (10,'020373218215','QINSI-OLD','NJ-OLD-000010');
        INSERT INTO receipt_items VALUES (20,10);
        INSERT INTO purchase_batch_items VALUES (30,10);
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "20260820_0033")

    with sqlite3.connect(db_path) as connection:
        indexes = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA index_list(products)")
        }
        connection.execute(
            "INSERT INTO products (id,jan,qinsi_product_code,internal_sku) VALUES (11,'020373218215','QINSI-NEW','NJ-NEW-000011')"
        )
        try:
            connection.execute(
                "INSERT INTO products (id,jan,qinsi_product_code,internal_sku) VALUES (12,'0490000000001','QINSI-NEW','NJ-NEW-000012')"
            )
        except sqlite3.IntegrityError:
            qinsi_code_still_unique = True
        else:
            qinsi_code_still_unique = False
        rows = connection.execute("SELECT id,jan,qinsi_product_code FROM products ORDER BY id").fetchall()
        receipt_product_id = connection.execute("SELECT product_id FROM receipt_items WHERE id=20").fetchone()[0]
        purchase_product_id = connection.execute("SELECT product_id FROM purchase_batch_items WHERE id=30").fetchone()[0]
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert indexes["ix_products_jan_not_null"] == 0
    assert indexes["uq_products_qinsi_code_not_null"] == 1
    assert ("uq_products_jan_not_null" not in indexes)
    assert rows[:2] == [(10, "020373218215", "QINSI-OLD"), (11, "020373218215", "QINSI-NEW")]
    assert qinsi_code_still_unique is True
    assert receipt_product_id == purchase_product_id == 10
    assert revision == ("20260820_0033",)


def test_0033_repairs_schema_when_0032_was_marked_but_table_unique_remained(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0032-table-unique.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260820_0032');
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            jan VARCHAR(32) UNIQUE,
            qinsi_product_code VARCHAR(100),
            internal_sku VARCHAR(32) NOT NULL
        );
        CREATE UNIQUE INDEX uq_products_internal_sku ON products (internal_sku);
        CREATE UNIQUE INDEX uq_products_qinsi_code_not_null ON products (qinsi_product_code) WHERE qinsi_product_code IS NOT NULL;
        INSERT INTO products VALUES (10,'020373218215','QINSI-OLD','NJ-OLD-000010');
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "20260820_0033")

    with sqlite3.connect(db_path) as connection:
        table_sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='products'").fetchone()[0]
        indexes = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA index_list(products)")
        }
        connection.execute(
            "INSERT INTO products (id,jan,qinsi_product_code,internal_sku) VALUES (11,'020373218215','QINSI-NEW','NJ-NEW-000011')"
        )
        rows = connection.execute("SELECT id,jan,qinsi_product_code FROM products ORDER BY id").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert "UNIQUE" not in table_sql.upper()
    assert indexes["ix_products_jan_not_null"] == 0
    assert indexes["uq_products_qinsi_code_not_null"] == 1
    assert rows == [(10, "020373218215", "QINSI-OLD"), (11, "020373218215", "QINSI-NEW")]
    assert revision == ("20260820_0033",)


def test_upgrade_from_current_0004_preserves_rows_and_defers_visual_hashes(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0004.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260715_0004")
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO receipt_batches (id,batch_no,status,current_stage,image_status,gpt_status,product_status,qinsi_status,zip_download_count,image_count,source_type,recognition_engine,created_at,updated_at) VALUES (1,'RCPT-20260715-0300-OLD1','uploaded','complete','uploaded','not_packaged','not_matched','not_exported',0,1,'mobile','none','2026-07-14 18:00:00','2026-07-14 18:00:00')")
        connection.execute("INSERT INTO receipt_images (id,batch_id,original_filename,recognition_filename,stored_filename,original_path,processed_path,page_no,file_hash,mime_type,file_size,width,height,preprocessing_status,recognition_source,rotation_degrees,duplicate_status,created_at) VALUES (1,1,'old.jpg','RCPT-20260715-0300-OLD1_P01.jpg','old.jpg','data/uploads/original/old.jpg','data/uploads/preview/old.jpg',1,'abc123','image/jpeg',10,100,200,'processed','original',0,'none','2026-07-14 18:00:00')")
        connection.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT original_filename,sha256,normalized_image_hash,perceptual_hash,duplicate_status FROM receipt_images WHERE id=1").fetchone()
        status = connection.execute("SELECT image_status,gpt_status,product_status,qinsi_status FROM receipt_batches WHERE id=1").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == ("old.jpg", "abc123", None, None, "none")
    assert status == ("ready", "not_packaged", "not_matched", "not_exported")
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0007_backfills_stable_unique_internal_skus(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0007-products.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260715_0007');
        CREATE TABLE products (
            id INTEGER PRIMARY KEY, jan VARCHAR(32), qinsi_product_code VARCHAR(100),
            name_cn VARCHAR(255), name_ja VARCHAR(255), specification VARCHAR(255), model_spec VARCHAR(255),
            purchase_price INTEGER, sale_price INTEGER, minimum_sale_price INTEGER, image_url TEXT,
            location_code VARCHAR(100), status VARCHAR(20) NOT NULL, source VARCHAR(50) NOT NULL,
            product_origin VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO products VALUES (1,NULL,'Q-OLD-1','旧商品一',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'active','manual','manual','2026-07-14 15:30:00','2026-07-14 15:30:00');
        INSERT INTO products VALUES (2,'00123457','Q-OLD-2','旧商品二',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'active','manual','manual','2026-07-14 15:31:00','2026-07-14 15:31:00');
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT id,jan,qinsi_product_code,internal_sku FROM products ORDER BY id").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        internal_sku_column = next(row for row in connection.execute("PRAGMA table_info(products)") if row[1] == "internal_sku")
    assert rows[0][:3] == (1, None, "Q-OLD-1") and rows[1][:3] == (2, "00123457", "Q-OLD-2")
    assert rows[0][3] == "NJ-20260715-000001" and rows[1][3] == "NJ-20260715-000002"
    assert internal_sku_column[3] == 1
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0008_creates_and_seeds_location_master(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0008-locations.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20260715_0008')")
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT internal_code,display_name,location_type,is_qinsi_warehouse FROM locations ORDER BY sort_order").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert len(rows) == 9
    assert rows[0] == ("QW-2025-QIANYU", "2025千羽", "qinsi_warehouse", 1)
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0009_creates_purchase_batch_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0009-purchase-batches.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20260715_0009')")
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"purchase_batches", "purchase_batch_items"} <= tables
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0010_creates_qinsi_purchase_export_loop_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0010-qinsi-export.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260716_0010")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"qinsi_purchase_export_jobs", "qinsi_purchase_export_lines", "qinsi_purchase_export_line_sources"} <= tables
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0011_creates_price_lookup_p0_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0011-price-lookup.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260716_0011")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    assert {"price_provider_attempts", "price_lookup_histories"} <= tables
    assert {"display_name", "main_image_path", "main_image_source_url", "product_data_confirmed"} <= product_columns
    assert revision == (HEAD_REVISION,)


def test_upgrade_from_0012_preserves_receipt_product_and_purchase_text(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0012-store-trace.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260716_0012');
        CREATE TABLE stores (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE receipt_batches (id INTEGER PRIMARY KEY, batch_no VARCHAR(40) NOT NULL);
        CREATE TABLE receipts (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, raw_store_name VARCHAR(255));
        CREATE TABLE products (id INTEGER PRIMARY KEY, internal_sku VARCHAR(32) NOT NULL, jan VARCHAR(32), name_cn VARCHAR(255));
        CREATE TABLE purchase_batches (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL, store_name VARCHAR(255));
        INSERT INTO receipt_batches VALUES (1,'OLD-BATCH');
        INSERT INTO receipts VALUES (1,1,'旧门店原始文字');
        INSERT INTO products VALUES (1,'NJ-20260716-000001',NULL,'旧商品');
        INSERT INTO purchase_batches VALUES (1,1,'旧采购门店文字');
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        receipt = connection.execute("SELECT id,raw_store_name,store_id FROM receipts").fetchone()
        product = connection.execute("SELECT id,internal_sku,name_cn FROM products").fetchone()
        purchase = connection.execute("SELECT id,store_name,store_id FROM purchase_batches").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert receipt == (1, "旧门店原始文字", None)
    assert product == (1, "NJ-20260716-000001", "旧商品")
    assert purchase == (1, "旧采购门店文字", None)
    assert revision == (HEAD_REVISION,)


def test_0021_backfills_safe_qinsi_derived_barcode_and_allows_null_field_store(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "from-0020-derived-barcode.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260720_0020")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO products (
                internal_sku, jan, qinsi_product_code, product_data_confirmed,
                name_locked, main_image_locked, needs_review, has_jan,
                status, source, product_origin, created_at, updated_at
            ) VALUES (?, NULL, ?, 0, 0, 0, 0, 0, 'active', 'qinsi_import', 'qinsi', ?, ?)
            """,
            (
                "NJ-20260720-900001",
                "/4550726010198",
                "2026-07-20 00:00:00",
                "2026-07-20 00:00:00",
            ),
        )
        connection.executemany(
            """
            INSERT INTO products (
                internal_sku, jan, qinsi_product_code, product_data_confirmed,
                name_locked, main_image_locked, needs_review, has_jan,
                status, source, product_origin, created_at, updated_at
            ) VALUES (?, ?, ?, 0, 0, 0, 0, ?, 'active', 'qinsi_import', 'qinsi', ?, ?)
            """,
            (
                (
                    "NJ-20260720-900002",
                    None,
                    "/4901234567894",
                    0,
                    "2026-07-20 00:00:00",
                    "2026-07-20 00:00:00",
                ),
                (
                    "NJ-20260720-900003",
                    "4901234567894",
                    "Q-CONFLICT",
                    1,
                    "2026-07-20 00:00:00",
                    "2026-07-20 00:00:00",
                ),
            ),
        )
        connection.commit()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        barcode = connection.execute(
            "SELECT barcode,source_system,is_primary FROM product_barcodes"
        ).fetchone()
        store_column = next(
            row for row in connection.execute("PRAGMA table_info(field_purchase_batches)")
            if row[1] == "store_id"
        )
        connection.execute(
            """
            INSERT INTO field_purchase_batches (
                batch_no,client_request_id,store_id,operator_name,status,
                started_at,created_at,updated_at
            ) VALUES ('FP-NULL-STORE','fp-null-store',NULL,'采购员','ACTIVE',?,?,?)
            """,
            (
                "2026-07-20 00:00:00",
                "2026-07-20 00:00:00",
                "2026-07-20 00:00:00",
            ),
        )
        connection.commit()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        alias_count = connection.execute(
            "SELECT COUNT(*) FROM product_barcodes WHERE barcode='4550726010198'"
        ).fetchone()[0]
        conflict_alias_count = connection.execute(
            "SELECT COUNT(*) FROM product_barcodes WHERE barcode='4901234567894'"
        ).fetchone()[0]
    assert barcode == ("4550726010198", "qinsi_sku_derived", 0)
    assert store_column[3] == 0
    assert alias_count == 1
    assert conflict_alias_count == 0
    assert revision == (HEAD_REVISION,)


def test_0022_moves_product_note_to_name_ja_with_conflict_audit_and_stats(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "from-0021-product-note.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260720_0021")
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO products (
                internal_sku, name_cn, name_ja, display_name, product_note,
                product_data_confirmed, name_locked, main_image_locked,
                needs_review, has_jan,
                status, source, product_origin, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 'active', 'qinsi_import', 'qinsi', ?, ?)
            """,
            (
                (
                    "NJ-20260720-910001", "中文一", None, None, "  日本語一  ",
                    "2026-07-20 00:00:00", "2026-07-20 00:00:00",
                ),
                (
                    "NJ-20260720-910002", "中文二", "旧日本名", "旧展示名", " 新日本名 ",
                    "2026-07-20 00:00:00", "2026-07-20 00:00:00",
                ),
                (
                    "NJ-20260720-910003", "中文三", None, None, "   ",
                    "2026-07-20 00:00:00", "2026-07-20 00:00:00",
                ),
            ),
        )
        connection.commit()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT internal_sku,name_ja,display_name,product_note FROM products "
            "WHERE internal_sku LIKE 'NJ-20260720-91%' ORDER BY internal_sku"
        ).fetchall()
        audits = connection.execute(
            "SELECT before_json,after_json FROM enrichment_audit_logs "
            "WHERE actor='migration:20260720_0022' ORDER BY id"
        ).fetchall()
        product_columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    assert rows == [
        ("NJ-20260720-910001", "日本語一", "中文一｜日本語一", None),
        ("NJ-20260720-910002", "新日本名", "中文二｜新日本名", None),
        ("NJ-20260720-910003", None, None, None),
    ]
    assert len(audits) == 2
    assert json.loads(audits[0][0])["name_ja"] == "旧日本名"
    summary = json.loads(audits[1][1])
    assert summary == {
        "migrated_count": 2,
        "conflict_count": 1,
        "empty_to_null_count": 1,
        "remaining_nonempty_product_note_count": 0,
    }
    assert {
        "display_image_url", "local_image_path", "image_sha256",
        "image_localization_status", "image_localization_source_url",
        "image_localized_at", "image_localization_error",
    } <= product_columns
