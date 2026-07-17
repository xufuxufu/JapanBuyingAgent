from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


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
    assert {"restock_lists", "restock_list_items"} <= tables
    assert "low_stock_threshold" in product_columns
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


def test_sqlite_foreign_keys_enabled(db_session):
    assert db_session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


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
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


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
    assert revision == ("20260717_0018",)


def test_upgrade_from_0012_preserves_receipt_product_and_purchase_text(tmp_path, monkeypatch):
    db_path = tmp_path / "from-0012-store-trace.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260716_0012');
        CREATE TABLE stores (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE receipt_batches (id INTEGER PRIMARY KEY, batch_no VARCHAR(40) NOT NULL);
        CREATE TABLE receipts (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, raw_store_name VARCHAR(255));
        CREATE TABLE products (id INTEGER PRIMARY KEY, internal_sku VARCHAR(32) NOT NULL, name_cn VARCHAR(255));
        CREATE TABLE purchase_batches (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL, store_name VARCHAR(255));
        INSERT INTO receipt_batches VALUES (1,'OLD-BATCH');
        INSERT INTO receipts VALUES (1,1,'旧门店原始文字');
        INSERT INTO products VALUES (1,'NJ-20260716-000001','旧商品');
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
    assert revision == ("20260717_0018",)
