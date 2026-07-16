"""QinSi inventory snapshots, mappings, and purchase assistance inputs"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0017"
down_revision = "20260716_0016"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "products" in tables and "low_stock_threshold" not in _columns(bind, "products"):
        op.add_column("products", sa.Column("low_stock_threshold", sa.Integer()))

    if "qinsi_inventory_snapshots" not in tables:
        op.create_table(
            "qinsi_inventory_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_no", sa.String(64), nullable=False, unique=True),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("file_hash", sa.String(64), nullable=False),
            sa.Column("file_content", sa.LargeBinary(), nullable=False),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("data_at", sa.DateTime(timezone=True)),
            sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("success_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("unmatched_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("exception_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("error_summary", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "status IN ('completed','completed_with_issues','failed')",
                name="ck_qinsi_inventory_snapshots_status",
            ),
        )
        op.create_index("uq_qinsi_inventory_snapshots_file_hash", "qinsi_inventory_snapshots", ["file_hash"], unique=True)
        op.create_index("ix_qinsi_inventory_snapshots_data_time", "qinsi_inventory_snapshots", ["data_at", "imported_at"])

    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_inventory_snapshot_lines" not in tables:
        op.create_table(
            "qinsi_inventory_snapshot_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("qinsi_inventory_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("original_row_no", sa.Integer(), nullable=False),
            sa.Column("raw_product_name", sa.String(255)),
            sa.Column("jan", sa.String(32)),
            sa.Column("qinsi_product_code", sa.String(100)),
            sa.Column("internal_sku", sa.String(32)),
            sa.Column("raw_warehouse_name", sa.String(255)),
            sa.Column("quantity", sa.Integer()),
            sa.Column("raw_summary_json", sa.Text(), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("warehouse_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="SET NULL")),
            sa.Column("matching_method", sa.String(40)),
            sa.Column("matching_status", sa.String(20), nullable=False),
            sa.Column("warehouse_status", sa.String(20), server_default="matched", nullable=False),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "matching_status IN ('matched','unmatched','conflict','ignored')",
                name="ck_qinsi_inventory_snapshot_lines_matching_status",
            ),
        )
        op.create_index("ix_qinsi_inventory_snapshot_lines_product_snapshot", "qinsi_inventory_snapshot_lines", ["product_id", "snapshot_id"])
        op.create_index("ix_qinsi_inventory_snapshot_lines_warehouse_snapshot", "qinsi_inventory_snapshot_lines", ["warehouse_id", "snapshot_id"])
        op.create_index("ix_qinsi_inventory_snapshot_lines_status", "qinsi_inventory_snapshot_lines", ["snapshot_id", "matching_status"])
        op.create_index("ix_qinsi_inventory_snapshot_lines_matching_status", "qinsi_inventory_snapshot_lines", ["matching_status"])

    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_product_mappings" not in tables:
        op.create_table(
            "qinsi_product_mappings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("qinsi_product_code", sa.String(100), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_snapshot_line_id", sa.Integer(), sa.ForeignKey("qinsi_inventory_snapshot_lines.id", ondelete="SET NULL")),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        op.create_index("uq_qinsi_product_mappings_code", "qinsi_product_mappings", ["qinsi_product_code"], unique=True)
        op.create_index("ix_qinsi_product_mappings_product", "qinsi_product_mappings", ["product_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("qinsi_product_mappings", "qinsi_inventory_snapshot_lines", "qinsi_inventory_snapshots"):
        if table in tables:
            op.drop_table(table)
    if "products" in tables and "low_stock_threshold" in _columns(bind, "products"):
        with op.batch_alter_table("products") as batch:
            batch.drop_column("low_stock_threshold")
