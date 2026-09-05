"""QinSi sales summary (进销存汇总) snapshots and lines (Phase 9A)"""
from alembic import op
import sqlalchemy as sa


revision = "20260904_0047"
down_revision = "20260901_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "qinsi_sales_summary_snapshots" not in tables:
        op.create_table(
            "qinsi_sales_summary_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_no", sa.String(64), nullable=False, unique=True),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("period_days", sa.Integer(), nullable=False),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("file_hash", sa.String(64), nullable=False),
            sa.Column("file_content", sa.LargeBinary(), nullable=False),
            sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("matched_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("unmatched_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("conflict_rows", sa.Integer(), server_default="0", nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("error_summary", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "status IN ('completed','completed_with_issues','failed')",
                name="ck_qinsi_sales_summary_snapshots_status",
            ),
            sa.CheckConstraint("period_end >= period_start", name="ck_qinsi_sales_summary_snapshots_period_order"),
        )
        op.create_index(
            "uq_qinsi_sales_summary_snapshots_file_hash", "qinsi_sales_summary_snapshots", ["file_hash"], unique=True,
        )
        op.create_index(
            "ix_qinsi_sales_summary_snapshots_period", "qinsi_sales_summary_snapshots", ["period_start", "period_end"],
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "qinsi_sales_summary_lines" not in tables:
        op.create_table(
            "qinsi_sales_summary_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "snapshot_id", sa.Integer(),
                sa.ForeignKey("qinsi_sales_summary_snapshots.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("original_row_no", sa.Integer(), nullable=False),
            sa.Column("product_name_snapshot", sa.String(255)),
            sa.Column("qinsi_product_code", sa.String(100)),
            sa.Column("jan_candidate", sa.String(32)),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("match_status", sa.String(20), nullable=False),
            sa.Column("matching_method", sa.String(40)),
            sa.Column("purchase_quantity", sa.Integer()),
            sa.Column("purchase_amount", sa.Numeric(18, 2)),
            sa.Column("sales_quantity", sa.Integer()),
            sa.Column("sales_amount", sa.Numeric(18, 2)),
            sa.Column("customer_count", sa.Integer()),
            sa.Column("reported_current_inventory", sa.Integer()),
            sa.Column("reported_support_sales_days", sa.Integer()),
            sa.Column("raw_row_json", sa.Text(), nullable=False),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "match_status IN ('matched','unmatched','conflict')",
                name="ck_qinsi_sales_summary_lines_match_status",
            ),
        )
        op.create_index(
            "ix_qinsi_sales_summary_lines_product_snapshot", "qinsi_sales_summary_lines", ["product_id", "snapshot_id"],
        )
        op.create_index(
            "ix_qinsi_sales_summary_lines_status", "qinsi_sales_summary_lines", ["snapshot_id", "match_status"],
        )
        op.create_index(
            "ix_qinsi_sales_summary_lines_match_status", "qinsi_sales_summary_lines", ["match_status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("qinsi_sales_summary_lines", "qinsi_sales_summary_snapshots"):
        if table in tables:
            op.drop_table(table)
