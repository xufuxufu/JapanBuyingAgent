"""purchase batch QinSi export confirmation loop"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0011"
down_revision = "20260716_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_purchase_export_jobs" not in tables:
        op.create_table(
            "qinsi_purchase_export_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("export_no", sa.String(64), nullable=False),
            sa.Column("selection_key", sa.String(160), nullable=False),
            sa.Column("export_type", sa.String(20), nullable=False),
            sa.Column("purchase_batch_id", sa.Integer(), sa.ForeignKey("purchase_batches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("qinsi_target_warehouse_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("parent_export_job_id", sa.Integer(), sa.ForeignKey("qinsi_purchase_export_jobs.id", ondelete="RESTRICT")),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("file_content", sa.LargeBinary(), nullable=False),
            sa.Column("status", sa.String(30), server_default="generated", nullable=False),
            sa.Column("line_count", sa.Integer(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("export_no", name="uq_qinsi_purchase_export_jobs_export_no"),
            sa.UniqueConstraint("selection_key", name="uq_qinsi_purchase_export_jobs_selection_key"),
            sa.CheckConstraint("export_type IN ('new_product','restock')", name="ck_qinsi_purchase_export_jobs_type"),
            sa.CheckConstraint(
                "status IN ('generated','imported','partially_failed','failed','cancelled')",
                name="ck_qinsi_purchase_export_jobs_status",
            ),
        )
        op.create_index("ix_qinsi_purchase_export_jobs_export_type", "qinsi_purchase_export_jobs", ["export_type"])
        op.create_index("ix_qinsi_purchase_export_jobs_purchase_batch_id", "qinsi_purchase_export_jobs", ["purchase_batch_id"])
        op.create_index("ix_qinsi_purchase_export_jobs_parent_export_job_id", "qinsi_purchase_export_jobs", ["parent_export_job_id"])
        op.create_index("ix_qinsi_purchase_export_jobs_status", "qinsi_purchase_export_jobs", ["status"])
    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_purchase_export_lines" not in tables:
        op.create_table(
            "qinsi_purchase_export_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("export_job_id", sa.Integer(), sa.ForeignKey("qinsi_purchase_export_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purchase_batch_id", sa.Integer(), sa.ForeignKey("purchase_batches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("purchase_batch_item_id", sa.Integer(), sa.ForeignKey("purchase_batch_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("receipt_id", sa.Integer(), sa.ForeignKey("receipts.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("qinsi_target_warehouse_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("row_no", sa.Integer(), nullable=False),
            sa.Column("internal_sku", sa.String(32), nullable=False),
            sa.Column("jan", sa.String(32)),
            sa.Column("qinsi_product_code", sa.String(100), nullable=False),
            sa.Column("product_name", sa.String(255), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("purchase_price", sa.Integer()),
            sa.Column("status", sa.String(20), server_default="generated", nullable=False),
            sa.Column("failure_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("export_job_id", "purchase_batch_item_id", name="uq_qinsi_purchase_export_line_job_item"),
            sa.CheckConstraint("status IN ('generated','imported','failed','cancelled')", name="ck_qinsi_purchase_export_lines_status"),
            sa.CheckConstraint("quantity > 0", name="ck_qinsi_purchase_export_lines_quantity_positive"),
        )
        for column in ("export_job_id", "purchase_batch_id", "purchase_batch_item_id", "receipt_id", "receipt_item_id", "product_id", "status"):
            op.create_index(f"ix_qinsi_purchase_export_lines_{column}", "qinsi_purchase_export_lines", [column])
    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_purchase_export_line_sources" not in tables:
        op.create_table(
            "qinsi_purchase_export_line_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("export_line_id", sa.Integer(), sa.ForeignKey("qinsi_purchase_export_lines.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purchase_batch_item_id", sa.Integer(), sa.ForeignKey("purchase_batch_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("export_line_id", name="uq_qinsi_purchase_export_line_source_line"),
        )
        op.create_index("ix_qinsi_purchase_export_line_sources_export_line_id", "qinsi_purchase_export_line_sources", ["export_line_id"])
        op.create_index("ix_qinsi_purchase_export_line_sources_purchase_batch_item_id", "qinsi_purchase_export_line_sources", ["purchase_batch_item_id"])
        op.create_index(
            "uq_qinsi_active_purchase_batch_item",
            "qinsi_purchase_export_line_sources",
            ["purchase_batch_item_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "qinsi_purchase_export_line_sources" in tables:
        op.drop_table("qinsi_purchase_export_line_sources")
    if "qinsi_purchase_export_lines" in tables:
        op.drop_table("qinsi_purchase_export_lines")
    if "qinsi_purchase_export_jobs" in tables:
        op.drop_table("qinsi_purchase_export_jobs")
