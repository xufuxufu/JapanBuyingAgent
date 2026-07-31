"""QinSi goods import batches, masters, complete product fields, and snapshot links"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_0019"
down_revision = "20260717_0018"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _foreign_targets_exist(bind, table: str) -> bool:
    tables = _tables(bind)
    return all(
        foreign_key.get("referred_table") in tables
        for foreign_key in sa.inspect(bind).get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "qinsi_import_batches" not in tables:
        op.create_table(
            "qinsi_import_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("business_batch_key", sa.String(100)),
            sa.Column("source_system", sa.String(30), server_default="qinsi", nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("file_hash", sa.String(64), nullable=False),
            sa.Column("file_content", sa.LargeBinary(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("parse_version", sa.Integer(), server_default="2", nullable=False),
            sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("new_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("update_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("unchanged_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("warning_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("success_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("summary_json", sa.Text()),
            sa.Column("error_message", sa.Text()),
            sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        op.create_index("uq_qinsi_import_batches_file_hash", "qinsi_import_batches", ["file_hash"], unique=True)
        op.create_index("ix_qinsi_import_batches_business_batch", "qinsi_import_batches", ["business_batch_key"])

    tables = _tables(bind)
    if "qinsi_master_values" not in tables:
        op.create_table(
            "qinsi_master_values",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("master_type", sa.String(40), nullable=False),
            sa.Column("source_name", sa.String(255), nullable=False),
            sa.Column("normalized_name", sa.String(255), nullable=False),
            sa.Column("source_system", sa.String(30), server_default="qinsi", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column(
                "first_import_batch_id", sa.Integer(),
                sa.ForeignKey("qinsi_import_batches.id", ondelete="SET NULL"),
            ),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint(
                "source_system", "master_type", "source_name",
                name="uq_qinsi_master_source_name",
            ),
        )
        op.create_index("ix_qinsi_master_type_active", "qinsi_master_values", ["master_type", "is_active"])

    if "products" in _tables(bind):
        existing = _columns(bind, "products")
        additions = [
            sa.Column("unit_name", sa.String(128)),
            sa.Column("qinsi_sort_order", sa.Integer()),
            sa.Column("qinsi_points_enabled", sa.Boolean()),
            sa.Column("inventory_warning_lower", sa.Numeric(18, 3)),
            sa.Column("inventory_warning_upper", sa.Numeric(18, 3)),
            sa.Column("shelf_life_days", sa.Integer()),
            sa.Column("batch_enabled", sa.Boolean()),
            sa.Column("expiration_warning_days", sa.Integer()),
            sa.Column("product_note", sa.Text()),
            sa.Column("origin_place", sa.String(255)),
            sa.Column("applicable_age", sa.String(255)),
            sa.Column("weight_kg", sa.Numeric(18, 3)),
            sa.Column("serial_number_enabled", sa.Boolean()),
            sa.Column("qinsi_brand_master_id", sa.Integer()),
            sa.Column("qinsi_category_master_id", sa.Integer()),
            sa.Column("qinsi_unit_master_id", sa.Integer()),
        ]
        if bind.dialect.name == "sqlite":
            complete_sqlite_schema = {
                "locations", "price_lookup_histories", "receipt_items",
                "product_enrichment_tasks", "qinsi_inventory_snapshots",
            } <= _tables(bind)
            for column in additions:
                if column.name not in existing:
                    op.add_column("products", column)
            for name in ("purchase_price", "sale_price", "minimum_sale_price"):
                if name in existing:
                    legacy_name = f"{name}_integer_legacy"
                    op.execute(sa.text(
                        f'ALTER TABLE products RENAME COLUMN "{name}" TO "{legacy_name}"'
                    ))
                    op.add_column("products", sa.Column(name, sa.Numeric(18, 2)))
                    op.execute(sa.text(
                        f'UPDATE products SET "{name}" = "{legacy_name}"'
                    ))
                    if complete_sqlite_schema:
                        op.execute(sa.text(
                            f'ALTER TABLE products DROP COLUMN "{legacy_name}"'
                        ))
                else:
                    op.add_column("products", sa.Column(name, sa.Numeric(18, 2)))
        else:
            with op.batch_alter_table("products") as batch:
                for column in additions:
                    if column.name not in existing:
                        batch.add_column(column)
                for name in ("purchase_price", "sale_price", "minimum_sale_price"):
                    if name in existing:
                        batch.alter_column(name, existing_type=sa.Integer(), type_=sa.Numeric(18, 2))
                    else:
                        batch.add_column(sa.Column(name, sa.Numeric(18, 2)))
            product_fks = {
                tuple(item.get("constrained_columns") or ())
                for item in sa.inspect(bind).get_foreign_keys("products")
            }
            with op.batch_alter_table("products") as batch:
                for column, fk_name in (
                    ("qinsi_brand_master_id", "fk_products_qinsi_brand_master"),
                    ("qinsi_category_master_id", "fk_products_qinsi_category_master"),
                    ("qinsi_unit_master_id", "fk_products_qinsi_unit_master"),
                ):
                    if (column,) not in product_fks:
                        batch.create_foreign_key(
                            fk_name, "qinsi_master_values", [column], ["id"], ondelete="SET NULL",
                        )

    tables = _tables(bind)
    if "product_barcodes" not in tables and "products" in tables:
        op.create_table(
            "product_barcodes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("barcode", sa.String(100), nullable=False),
            sa.Column("source_system", sa.String(30), server_default="qinsi", nullable=False),
            sa.Column("is_primary", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("product_id", "barcode", name="uq_product_barcode_product_value"),
        )
        op.create_index("uq_product_barcodes_barcode", "product_barcodes", ["barcode"], unique=True)
        if "jan" in _columns(bind, "products"):
            op.execute(sa.text(
                "INSERT INTO product_barcodes "
                "(product_id, barcode, source_system, is_primary, created_at, updated_at) "
                "SELECT id, jan, 'legacy', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM products WHERE jan IS NOT NULL AND trim(jan) <> ''"
            ))

    tables = _tables(bind)
    if "qinsi_goods_import_rows" not in tables:
        op.create_table(
            "qinsi_goods_import_rows",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "import_batch_id", sa.Integer(),
                sa.ForeignKey("qinsi_import_batches.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("source_file_name", sa.String(255), nullable=False),
            sa.Column("sheet_name", sa.String(100), server_default="商品导入", nullable=False),
            sa.Column("excel_row_number", sa.Integer(), nullable=False),
            sa.Column("qinsi_product_code", sa.String(100)),
            sa.Column("barcode", sa.String(100)),
            sa.Column("parsed_data", sa.Text(), nullable=False),
            sa.Column("raw_json", sa.Text(), nullable=False),
            sa.Column("validation_status", sa.String(30), nullable=False),
            sa.Column("warnings", sa.Text()),
            sa.Column("errors", sa.Text()),
            sa.Column("conflict_json", sa.Text()),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("import_batch_id", "excel_row_number", name="uq_qinsi_goods_rows_batch_row"),
        )
        op.create_index("ix_qinsi_goods_rows_code", "qinsi_goods_import_rows", ["qinsi_product_code"])
        op.create_index("ix_qinsi_goods_rows_barcode", "qinsi_goods_import_rows", ["barcode"])
        op.create_index("ix_qinsi_goods_rows_status", "qinsi_goods_import_rows", ["import_batch_id", "validation_status"])

    if "qinsi_inventory_snapshots" in _tables(bind):
        existing = _columns(bind, "qinsi_inventory_snapshots")
        if "source_system" not in existing:
            op.add_column(
                "qinsi_inventory_snapshots",
                sa.Column("source_system", sa.String(30), server_default="qinsi", nullable=False),
            )
        if "snapshot_type" not in existing:
            op.add_column(
                "qinsi_inventory_snapshots",
                sa.Column("snapshot_type", sa.String(30), server_default="counted_inventory", nullable=False),
            )
        if "source_import_batch_id" not in existing:
            op.add_column(
                "qinsi_inventory_snapshots",
                sa.Column("source_import_batch_id", sa.Integer()),
            )
        snapshot_fks = {
            tuple(item.get("constrained_columns") or ())
            for item in sa.inspect(bind).get_foreign_keys("qinsi_inventory_snapshots")
        }
        if (
            bind.dialect.name != "sqlite"
            and ("source_import_batch_id",) not in snapshot_fks
            and _foreign_targets_exist(bind, "qinsi_inventory_snapshots")
        ):
            with op.batch_alter_table("qinsi_inventory_snapshots") as batch:
                batch.create_foreign_key(
                    "fk_qinsi_inventory_snapshot_import_batch",
                    "qinsi_import_batches", ["source_import_batch_id"], ["id"], ondelete="SET NULL",
                )
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("qinsi_inventory_snapshots")}
        if "uq_qinsi_inventory_snapshot_import_batch" not in indexes:
            op.create_index(
                "uq_qinsi_inventory_snapshot_import_batch",
                "qinsi_inventory_snapshots", ["source_import_batch_id"], unique=True,
            )

    if "qinsi_inventory_snapshot_lines" in _tables(bind):
        existing = _columns(bind, "qinsi_inventory_snapshot_lines")
        if "current_quantity" not in existing:
            op.add_column(
                "qinsi_inventory_snapshot_lines",
                sa.Column("current_quantity", sa.Numeric(18, 3)),
            )

    if "import_jobs" in _tables(bind) and "status" in _columns(bind, "import_jobs"):
        op.execute(sa.text(
            "UPDATE import_jobs SET status='obsolete' "
            "WHERE job_type='qinsi_products' AND status='previewed'"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if "qinsi_inventory_snapshot_lines" in _tables(bind) and "current_quantity" in _columns(bind, "qinsi_inventory_snapshot_lines"):
        with op.batch_alter_table("qinsi_inventory_snapshot_lines") as batch:
            batch.drop_column("current_quantity")
    if "qinsi_inventory_snapshots" in _tables(bind):
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("qinsi_inventory_snapshots")}
        if "uq_qinsi_inventory_snapshot_import_batch" in indexes:
            op.drop_index("uq_qinsi_inventory_snapshot_import_batch", table_name="qinsi_inventory_snapshots")
        existing = _columns(bind, "qinsi_inventory_snapshots")
        with op.batch_alter_table("qinsi_inventory_snapshots") as batch:
            for name in ("source_import_batch_id", "snapshot_type", "source_system"):
                if name in existing:
                    batch.drop_column(name)
    for table in ("qinsi_goods_import_rows", "product_barcodes"):
        if table in _tables(bind):
            op.drop_table(table)
    if "products" in _tables(bind):
        existing = _columns(bind, "products")
        with op.batch_alter_table("products") as batch:
            for name in (
                "qinsi_unit_master_id", "qinsi_category_master_id", "qinsi_brand_master_id",
                "serial_number_enabled", "weight_kg", "applicable_age", "origin_place",
                "product_note", "expiration_warning_days", "batch_enabled", "shelf_life_days",
                "inventory_warning_upper", "inventory_warning_lower", "qinsi_points_enabled",
                "qinsi_sort_order", "unit_name",
            ):
                if name in existing:
                    batch.drop_column(name)
            for name in ("purchase_price", "sale_price", "minimum_sale_price"):
                if name in existing:
                    batch.alter_column(name, existing_type=sa.Numeric(18, 2), type_=sa.Integer())
    for table in ("qinsi_master_values", "qinsi_import_batches"):
        if table in _tables(bind):
            op.drop_table(table)
