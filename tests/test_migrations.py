from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


HEAD_REVISION = "20260905_0048"


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
        sales_order_columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_orders)")}
        sales_order_item_columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_order_items)")}
        procurement_demand_columns = {row[1] for row in connection.execute("PRAGMA table_info(procurement_demands)")}
    assert {"receipt_batches", "receipt_images", "receipts", "receipt_items", "ai_recognition_runs", "products", "product_aliases", "store_brands", "stores", "store_aliases", "locations", "purchase_batches", "purchase_batch_items", "inventory_transactions", "import_jobs", "import_rows", "product_match_logs", "qinsi_export_jobs", "qinsi_export_lines", "qinsi_export_line_sources", "qinsi_purchase_export_jobs", "qinsi_purchase_export_lines", "qinsi_purchase_export_line_sources", "marketplaces", "price_search_runs", "product_offers", "price_provider_attempts", "price_lookup_histories", "price_watch_rules", "price_alerts", "product_watch_configs", "product_watch_recommendations", "duplicate_detection_logs", "zip_package_jobs", "zip_package_items"} <= tables
    assert {"product_watch_snapshots", "product_watch_notifications", "monitor_scheduler_states"} <= tables
    assert {"qinsi_inventory_snapshots", "qinsi_inventory_snapshot_lines", "qinsi_product_mappings"} <= tables
    assert {"qinsi_sales_summary_snapshots", "qinsi_sales_summary_lines"} <= tables
    assert {
        "qinsi_import_batches", "qinsi_goods_import_rows", "qinsi_master_values", "product_barcodes",
        "qinsi_conflict_resolutions",
        "field_purchase_batches", "field_purchase_items", "tag_evidence",
        "durable_background_jobs", "platform_provider_states", "platform_lookup_results",
            "enrichment_audit_logs", "product_operation_logs",
    } <= tables
    assert {"restock_lists", "restock_list_items"} <= tables
    assert {
        "customers", "customer_addresses", "salespersons", "sales_orders", "sales_order_items",
        "sales_order_shipping_labels", "sales_shipments", "sales_shipment_items",
    } <= tables
    assert {"low_stock_threshold", "unit_name", "weight_kg", "qinsi_brand_master_id"} <= product_columns
    assert {"recipient_name_snapshot", "recipient_phone_snapshot", "shipping_address_snapshot"} <= sales_order_columns
    assert {"manual_image_relative_path", "manual_image_original_filename", "manual_image_content_type", "manual_image_file_size"} <= sales_order_item_columns
    assert {"manual_image_relative_path", "manual_image_original_filename", "manual_image_content_type", "manual_image_file_size"} <= procurement_demand_columns
    assert revision == (HEAD_REVISION,)


def test_0046_procurement_demand_manual_image_columns_upgrade_and_downgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "procurement-manual-image.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(procurement_demands)")}
        connection.execute(
            "INSERT INTO procurement_demands "
            "(product_name_snapshot, demand_type, source_person, source_type, status, manual_image_relative_path, "
            "created_at, updated_at) "
            "VALUES ('手工商品（图片）', 'channel_shortage', '丈母娘', 'channel_shortage', 'open', "
            "'data/procurement-demands/item-images/1/x.jpg', datetime('now'), datetime('now'))"
        )
        connection.commit()
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {
        "manual_image_relative_path", "manual_image_original_filename",
        "manual_image_content_type", "manual_image_file_size",
    } <= columns
    assert fk_check == []

    command.downgrade(config, "20260831_0045")
    with sqlite3.connect(db_path) as connection:
        columns_after = {row[1] for row in connection.execute("PRAGMA table_info(procurement_demands)")}
        row = connection.execute("SELECT product_name_snapshot, status FROM procurement_demands").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert not any(c.startswith("manual_image") for c in columns_after)
    assert row == ("手工商品（图片）", "open")
    assert integrity == ("ok",)

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        columns_final = {row[1] for row in connection.execute("PRAGMA table_info(procurement_demands)")}
    assert revision == (HEAD_REVISION,)
    assert {
        "manual_image_relative_path", "manual_image_original_filename",
        "manual_image_content_type", "manual_image_file_size",
    } <= columns_final


def test_0047_qinsi_sales_summary_upgrade_downgrade_reupgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "sales-summary.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        snapshot_columns = {row[1] for row in connection.execute("PRAGMA table_info(qinsi_sales_summary_snapshots)")}
        line_columns = {row[1] for row in connection.execute("PRAGMA table_info(qinsi_sales_summary_lines)")}
        connection.execute(
            "INSERT INTO qinsi_sales_summary_snapshots "
            "(snapshot_no, period_start, period_end, period_days, imported_at, original_filename, "
            "file_hash, file_content, total_rows, matched_rows, unmatched_rows, conflict_rows, status, created_at) "
            "VALUES ('QSS-TEST-1', '2026-08-01', '2026-08-30', 30, datetime('now'), 't.zip', 'h1', x'00', "
            "1, 1, 0, 0, 'completed', datetime('now'))"
        )
        snapshot_id = connection.execute("SELECT id FROM qinsi_sales_summary_snapshots").fetchone()[0]
        connection.execute(
            "INSERT INTO qinsi_sales_summary_lines "
            "(snapshot_id, original_row_no, product_name_snapshot, qinsi_product_code, jan_candidate, "
            "match_status, sales_quantity, sales_amount, raw_row_json, created_at) "
            "VALUES (?, 1, '测试商品', '000123', '4900000000001', 'matched', 5, '1234.50', '{}', datetime('now'))",
            (snapshot_id,),
        )
        connection.commit()
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {
        "snapshot_no", "period_start", "period_end", "period_days", "total_rows",
        "matched_rows", "unmatched_rows", "conflict_rows", "status",
    } <= snapshot_columns
    assert {
        "product_id", "match_status", "purchase_quantity", "purchase_amount",
        "sales_quantity", "sales_amount", "customer_count",
        "reported_current_inventory", "reported_support_sales_days",
    } <= line_columns
    assert fk_check == []

    command.downgrade(config, "20260901_0046")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert "qinsi_sales_summary_snapshots" not in tables
    assert "qinsi_sales_summary_lines" not in tables
    assert integrity == ("ok",)

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert revision == (HEAD_REVISION,)
    assert {"qinsi_sales_summary_snapshots", "qinsi_sales_summary_lines"} <= tables


def test_0048_domestic_logistics_tracking_upgrade_downgrade_reupgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "domestic-logistics.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        shipment_columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_shipments)")}
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(shipment_tracking_events)")}
        connection.execute("INSERT INTO customers (name, created_at, updated_at) VALUES ('测试客户', datetime('now'), datetime('now'))")
        customer_id = connection.execute("SELECT id FROM customers").fetchone()[0]
        connection.execute("INSERT INTO salespersons (name, active, created_at, updated_at) VALUES ('测试销售', 1, datetime('now'), datetime('now'))")
        salesperson_id = connection.execute("SELECT id FROM salespersons").fetchone()[0]
        connection.execute(
            "INSERT INTO sales_orders (order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at) "
            "VALUES ('SO-TEST-0048', ?, ?, 'paid', datetime('now'), datetime('now'), datetime('now'))",
            (customer_id, salesperson_id),
        )
        order_id = connection.execute("SELECT id FROM sales_orders").fetchone()[0]
        connection.execute(
            "INSERT INTO sales_shipments "
            "(sales_order_id, shipment_no, status, recipient_name_snapshot, shipping_address_snapshot, "
            "carrier, tracking_no, tracking_terminal, created_at, updated_at) "
            "VALUES (?, 'SO-TEST-0048-S1', 'shipped', '测试客户', '测试地址', '中通', '70000000000001', 0, datetime('now'), datetime('now'))",
            (order_id,),
        )
        shipment_id = connection.execute("SELECT id FROM sales_shipments").fetchone()[0]
        connection.execute(
            "INSERT INTO shipment_tracking_events "
            "(shipment_id, event_time, description, status, event_hash, created_at) "
            "VALUES (?, datetime('now'), '已签收', '签收', 'testhash1', datetime('now'))",
            (shipment_id,),
        )
        connection.commit()
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {
        "tracking_status", "tracking_terminal", "tracking_last_checked_at",
        "tracking_last_event_at", "tracking_next_check_at", "tracking_error",
    } <= shipment_columns
    assert {"shipment_id", "event_time", "description", "area_code", "area_name", "status", "event_hash"} <= event_columns
    assert fk_check == []

    command.downgrade(config, "20260904_0047")
    with sqlite3.connect(db_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        shipment_columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_shipments)")}
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert "shipment_tracking_events" not in tables
    assert "tracking_status" not in shipment_columns
    assert integrity == ("ok",)

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert revision == (HEAD_REVISION,)
    assert "shipment_tracking_events" in tables


def test_0038_sales_order_tables_are_empty_and_repeat_safe(tmp_path, monkeypatch):
    db_path = tmp_path / "sales-orders.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("customers", "salespersons", "sales_orders", "sales_order_items")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_order_items)")}
    assert counts == {"customers": 0, "salespersons": 0, "sales_orders": 0, "sales_order_items": 0}
    assert {"product_id", "product_name_snapshot", "jan_snapshot", "quantity", "unit_sale_price"} <= columns


def test_0039_upgrade_preserves_submitted_and_cancelled_orders_and_downgrade_works(tmp_path, monkeypatch):
    # Seed the *literal* 0038 schema by hand (not via command.upgrade), because
    # migration 20260714_0001 bootstraps every table straight from the current,
    # live app/models.py -- on an empty database it would create sales_orders
    # already in its post-0039 shape and make 0039's own rebuild a silent no-op,
    # so a command.upgrade(..., "20260828_0038")-based setup would never actually
    # exercise migration 0039's real ALTER/rebuild logic (this bit a first version
    # of this test: it "passed" without the rebuild ever running).
    db_path = tmp_path / "sales-order-fulfillment.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260828_0038');
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, phone VARCHAR(50),
            wechat_name VARCHAR(128), address TEXT, note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE salespersons (
            id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE products (id INTEGER PRIMARY KEY, internal_sku VARCHAR(32) NOT NULL);
        CREATE TABLE sales_orders (
            id INTEGER PRIMARY KEY,
            order_no VARCHAR(40) NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            salesperson_id INTEGER NOT NULL REFERENCES salespersons(id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL,
            order_date DATETIME NOT NULL,
            note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            CONSTRAINT ck_sales_orders_status CHECK (status IN ('submitted','cancelled'))
        );
        CREATE TABLE sales_order_items (
            id INTEGER PRIMARY KEY,
            sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            product_name_snapshot VARCHAR(255) NOT NULL,
            jan_snapshot VARCHAR(32),
            quantity INTEGER NOT NULL,
            unit_sale_price NUMERIC(18,2) NOT NULL,
            note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO customers (id, name, created_at, updated_at) VALUES (1, '旧客户', datetime('now'), datetime('now'));
        INSERT INTO salespersons (id, name, active, created_at, updated_at) VALUES (1, '秀', 1, datetime('now'), datetime('now'));
        INSERT INTO sales_orders (order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at)
            VALUES ('SO-OLD-0001', 1, 1, 'submitted', datetime('now'), datetime('now'), datetime('now'));
        INSERT INTO sales_orders (order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at)
            VALUES ('SO-OLD-0002', 1, 1, 'cancelled', datetime('now'), datetime('now'), datetime('now'));
        INSERT INTO sales_order_items (sales_order_id, product_name_snapshot, quantity, unit_sale_price, created_at, updated_at)
            VALUES (1, '旧商品', 1, 100, datetime('now'), datetime('now'));
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        rows = {
            row[0]: row[1] for row in connection.execute("SELECT order_no, status FROM sales_orders")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_orders)")}
        item_fk_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='sales_order_items'").fetchone()[0]
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        # sales_order_items must still reference the live "sales_orders" table, not a
        # leftover rebuild artifact -- this is the regression this test guards against.
        connection.execute(
            "INSERT INTO sales_order_items (sales_order_id, product_name_snapshot, quantity, unit_sale_price, created_at, updated_at) "
            "VALUES (1, '迁移后新商品', 1, 50, datetime('now'), datetime('now'))"
        )
        connection.commit()
    assert rows == {"SO-OLD-0001": "submitted", "SO-OLD-0002": "cancelled"}
    assert {"recipient_name_snapshot", "recipient_phone_snapshot", "shipping_address_snapshot"} <= columns
    assert "REFERENCES sales_orders" in item_fk_sql
    assert revision == (HEAD_REVISION,)

    command.downgrade(config, "20260828_0038")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        rows = {
            row[0]: row[1] for row in connection.execute("SELECT order_no, status FROM sales_orders")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_orders)")}
        item_fk_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='sales_order_items'").fetchone()[0]
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        connection.execute(
            "INSERT INTO sales_order_items (sales_order_id, product_name_snapshot, quantity, unit_sale_price, created_at, updated_at) "
            "VALUES (1, '降级后新商品', 1, 50, datetime('now'), datetime('now'))"
        )
        connection.commit()
    assert rows == {"SO-OLD-0001": "submitted", "SO-OLD-0002": "cancelled"}
    assert "recipient_name_snapshot" not in columns
    assert "REFERENCES sales_orders" in item_fk_sql
    assert revision == ("20260828_0038",)


def test_0040_upgrade_adds_shipping_labels_without_touching_existing_fks(tmp_path, monkeypatch):
    # Seed the literal 0039 schema by hand for the same reason 0039's own test does:
    # migration 20260714_0001 bootstraps every table from the current, live
    # app/models.py, so a command.upgrade(..., "20260828_0039")-based setup on an
    # empty database would already include sales_order_shipping_labels and make
    # 0040's own create_table a no-op, never exercising the real migration.
    db_path = tmp_path / "sales-order-shipping-labels.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260828_0039');
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, phone VARCHAR(50),
            wechat_name VARCHAR(128), address TEXT, note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE salespersons (
            id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE products (id INTEGER PRIMARY KEY, internal_sku VARCHAR(32) NOT NULL);
        CREATE TABLE sales_orders (
            id INTEGER PRIMARY KEY,
            order_no VARCHAR(40) NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            salesperson_id INTEGER NOT NULL REFERENCES salespersons(id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL,
            order_date DATETIME NOT NULL,
            note TEXT,
            recipient_name_snapshot VARCHAR(255), recipient_phone_snapshot VARCHAR(50), shipping_address_snapshot TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            CONSTRAINT ck_sales_orders_status CHECK (status IN ('submitted','ready_to_ship','shipped','completed','cancelled'))
        );
        CREATE TABLE sales_order_items (
            id INTEGER PRIMARY KEY,
            sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            product_name_snapshot VARCHAR(255) NOT NULL,
            jan_snapshot VARCHAR(32),
            quantity INTEGER NOT NULL,
            unit_sale_price NUMERIC(18,2) NOT NULL,
            note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO customers (id, name, created_at, updated_at) VALUES (1, '旧客户', datetime('now'), datetime('now'));
        INSERT INTO salespersons (id, name, active, created_at, updated_at) VALUES (1, '秀', 1, datetime('now'), datetime('now'));
        INSERT INTO sales_orders (order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at)
            VALUES ('SO-OLD-0001', 1, 1, 'ready_to_ship', datetime('now'), datetime('now'), datetime('now'));
        INSERT INTO sales_order_items (sales_order_id, product_name_snapshot, quantity, unit_sale_price, created_at, updated_at)
            VALUES (1, '旧商品', 1, 100, datetime('now'), datetime('now'));
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        item_fk_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='sales_order_items'").fetchone()[0]
        order_row = connection.execute("SELECT order_no, status FROM sales_orders WHERE id=1").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        # A fresh shipping label must actually be insertable and cascade-delete cleanly.
        connection.execute(
            "INSERT INTO sales_order_shipping_labels (sales_order_id, stored_filename, relative_path, created_at) "
            "VALUES (1, 'abc123.jpg', 'data/sales-orders/shipping-labels/1/abc123.jpg', datetime('now'))"
        )
        connection.commit()
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
        label_fk = connection.execute("PRAGMA foreign_key_list(sales_order_shipping_labels)").fetchall()
    assert "sales_order_shipping_labels" in tables
    assert "REFERENCES sales_orders" in item_fk_sql
    # ready_to_ship no longer exists as of 20260831_0045; it converts to paid.
    assert order_row == ("SO-OLD-0001", "paid")
    assert fk_check == []
    assert any(row[2] == "sales_orders" and row[3] == "sales_order_id" for row in label_fk)
    assert revision == (HEAD_REVISION,)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM sales_orders WHERE id=1")
        connection.commit()
        remaining_labels = connection.execute("SELECT COUNT(*) FROM sales_order_shipping_labels").fetchone()[0]
    assert remaining_labels == 0  # ON DELETE CASCADE removed the label with its order

    command.downgrade(config, "20260828_0039")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        item_fk_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='sales_order_items'").fetchone()[0]
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "sales_order_shipping_labels" not in tables
    assert "REFERENCES sales_orders" in item_fk_sql
    assert fk_check == []
    assert revision == ("20260828_0039",)


def test_0045_upgrade_converts_ready_to_ship_and_downgrade_blocks_on_paid_orders(tmp_path, monkeypatch):
    # Seed the literal 0044 schema by hand for the same reason 0039/0040's own
    # tests do: on an empty DB, migration 20260714_0001 bootstraps every table
    # from the current, live app/models.py, which would already include
    # customer_addresses/sales_shipments/the widened status set and make
    # 0045's own rebuild logic a silent no-op.
    db_path = tmp_path / "sales-order-shipments.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260828_0044');
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, phone VARCHAR(50),
            wechat_name VARCHAR(128), address TEXT, note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE salespersons (
            id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL, active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE products (id INTEGER PRIMARY KEY, internal_sku VARCHAR(32) NOT NULL);
        CREATE TABLE sales_orders (
            id INTEGER PRIMARY KEY,
            order_no VARCHAR(40) NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
            salesperson_id INTEGER NOT NULL REFERENCES salespersons(id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL,
            order_date DATETIME NOT NULL,
            note TEXT,
            recipient_name_snapshot VARCHAR(255), recipient_phone_snapshot VARCHAR(50), shipping_address_snapshot TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            CONSTRAINT ck_sales_orders_status CHECK (status IN ('submitted','ready_to_ship','shipped','completed','cancelled'))
        );
        CREATE TABLE sales_order_items (
            id INTEGER PRIMARY KEY,
            sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
            product_name_snapshot VARCHAR(255) NOT NULL,
            jan_snapshot VARCHAR(32),
            quantity INTEGER NOT NULL,
            unit_sale_price NUMERIC(18,2) NOT NULL,
            note TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE sales_order_shipping_labels (
            id INTEGER PRIMARY KEY,
            sales_order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
            stored_filename VARCHAR(255) NOT NULL,
            original_filename VARCHAR(255),
            relative_path TEXT NOT NULL,
            content_type VARCHAR(100),
            file_size INTEGER,
            created_at DATETIME NOT NULL
        );
        INSERT INTO customers (id, name, phone, address, created_at, updated_at)
            VALUES (1, '老客户', '13900000000', '北京市朝阳区', datetime('now'), datetime('now'));
        INSERT INTO salespersons (id, name, active, created_at, updated_at) VALUES (1, '秀', 1, datetime('now'), datetime('now'));
        INSERT INTO sales_orders (id, order_no, customer_id, salesperson_id, status, order_date, recipient_name_snapshot, recipient_phone_snapshot, shipping_address_snapshot, created_at, updated_at)
            VALUES (1, 'SO-OLD-0001', 1, 1, 'ready_to_ship', datetime('now'), '老客户', '13900000000', '北京市朝阳区', datetime('now'), datetime('now'));
        INSERT INTO sales_orders (id, order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at)
            VALUES (2, 'SO-OLD-0002', 1, 1, 'completed', datetime('now'), datetime('now'), datetime('now'));
        INSERT INTO sales_order_items (id, sales_order_id, product_name_snapshot, quantity, unit_sale_price, created_at, updated_at)
            VALUES (1, 1, '旧商品', 3, 65.00, datetime('now'), datetime('now'));
        INSERT INTO sales_order_shipping_labels (id, sales_order_id, stored_filename, relative_path, created_at)
            VALUES (1, 1, 'old.jpg', 'data/sales-orders/shipping-labels/1/old.jpg', datetime('now'));
        """)
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # idempotency
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        statuses = dict(connection.execute("SELECT order_no, status FROM sales_orders"))
        item = connection.execute(
            "SELECT product_name_snapshot, quantity, unit_sale_price, manual_image_relative_path FROM sales_order_items WHERE id=1"
        ).fetchone()
        label_shipment_id = connection.execute("SELECT shipment_id FROM sales_order_shipping_labels WHERE id=1").fetchone()[0]
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        # a fresh identity-less (image-only) item must now be insertable
        connection.execute(
            "INSERT INTO sales_order_items (sales_order_id, manual_image_relative_path, quantity, unit_sale_price, created_at, updated_at) "
            "VALUES (1, 'data/sales-orders/item-images/1/x.jpg', 1, 10.00, datetime('now'), datetime('now'))"
        )
        connection.commit()
    assert {"customer_addresses", "sales_shipments", "sales_shipment_items"} <= tables
    assert statuses == {"SO-OLD-0001": "paid", "SO-OLD-0002": "completed"}
    # the pre-existing purchase fact (quantity/price) must be byte-for-byte unchanged
    assert item == ("旧商品", 3, 65, None)
    assert label_shipment_id is None  # pre-shipment-model label stays order-level, untouched
    assert fk_check == []
    assert revision == (HEAD_REVISION,)

    # Downgrading with a 'paid' order present must be refused (proven in
    # test_0045_downgrade_blocks_when_paid_or_partially_shipped_orders_exist);
    # to exercise the happy downgrade path here, first move the order past
    # 'paid' the only way this test cares about: delete it (its item and the
    # order-level label cascade with it), isolating "does the rebuild itself
    # work" from "does the blocking guard work".
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM sales_orders WHERE order_no='SO-OLD-0001'")
        connection.commit()

    command.downgrade(config, "20260828_0044")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        statuses = dict(connection.execute("SELECT order_no, status FROM sales_orders"))
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(sales_order_items)")}
        fk_check = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"customer_addresses", "sales_shipments", "sales_shipment_items"}.isdisjoint(tables)
    assert "shipment_id" not in {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(sales_order_shipping_labels)")}
    assert "manual_image_relative_path" not in item_columns
    assert statuses == {"SO-OLD-0002": "completed"}
    assert fk_check == []
    assert revision == ("20260828_0044",)


def test_0045_downgrade_blocks_when_paid_or_partially_shipped_orders_exist(tmp_path, monkeypatch):
    db_path = tmp_path / "sales-order-shipments-block.sqlite3"
    monkeypatch.setenv("JBA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO customers (id, name, created_at, updated_at) VALUES (1, '客户', datetime('now'), datetime('now'))")
        connection.execute("INSERT INTO salespersons (id, name, active, created_at, updated_at) VALUES (1, '秀', 1, datetime('now'), datetime('now'))")
        connection.execute(
            "INSERT INTO sales_orders (order_no, customer_id, salesperson_id, status, order_date, created_at, updated_at) "
            "VALUES ('SO-BLOCK-0001', 1, 1, 'paid', datetime('now'), datetime('now'), datetime('now'))"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="paid/partially_shipped"):
        command.downgrade(config, "20260828_0044")


def test_upgrade_from_phase1_preserves_old_data(tmp_path, monkeypatch):
    db_path = tmp_path / "phase1.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('20260714_0001');
        CREATE TABLE receipt_batches (id INTEGER PRIMARY KEY, batch_no VARCHAR(40) NOT NULL, status VARCHAR(20) NOT NULL, image_count INTEGER NOT NULL, source_type VARCHAR(20) NOT NULL, recognition_engine VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
        CREATE TABLE receipt_images (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, original_filename VARCHAR(255) NOT NULL, stored_filename VARCHAR(100) NOT NULL, original_path TEXT NOT NULL, processed_path TEXT, page_no INTEGER NOT NULL, file_hash VARCHAR(64) NOT NULL, mime_type VARCHAR(100) NOT NULL, file_size INTEGER NOT NULL, width INTEGER, height INTEGER, preprocessing_status VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE receipts (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, raw_store_name VARCHAR(255), purchased_at DATETIME, receipt_number VARCHAR(100), subtotal INTEGER, discount_total INTEGER NOT NULL, tax_total INTEGER, paid_total INTEGER, currency VARCHAR(3) NOT NULL, recognition_status VARCHAR(20) NOT NULL, confirmation_status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, confirmed_at DATETIME);
        CREATE TABLE products (id INTEGER PRIMARY KEY, jan VARCHAR(32), qinsi_product_code VARCHAR(100), internal_sku VARCHAR(32) NOT NULL, created_at DATETIME, updated_at DATETIME);
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
        connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, jan VARCHAR(32), qinsi_product_code VARCHAR(100), internal_sku VARCHAR(32) NOT NULL)")
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
        connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, jan VARCHAR(32), qinsi_product_code VARCHAR(100), internal_sku VARCHAR(32) NOT NULL)")
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
