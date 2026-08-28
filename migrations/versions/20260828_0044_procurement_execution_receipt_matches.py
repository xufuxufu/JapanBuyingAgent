"""Procurement execution to receipt item reconciliation matches (Phase 2E)"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0044"
down_revision = "20260828_0043"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "procurement_execution_receipt_matches" not in tables:
        op.create_table(
            "procurement_execution_receipt_matches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "execution_id", sa.Integer(), sa.ForeignKey("procurement_purchase_executions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("matched_quantity", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("matched_quantity > 0", name="ck_procurement_execution_receipt_matches_quantity_positive"),
            sa.UniqueConstraint(
                "execution_id", "receipt_item_id", name="uq_procurement_execution_receipt_matches_pair",
            ),
        )

    indexes = _indexes(bind, "procurement_execution_receipt_matches")
    if "ix_procurement_execution_receipt_matches_execution_id" not in indexes:
        op.create_index(
            "ix_procurement_execution_receipt_matches_execution_id", "procurement_execution_receipt_matches",
            ["execution_id"],
        )
    if "ix_procurement_execution_receipt_matches_receipt_item_id" not in indexes:
        op.create_index(
            "ix_procurement_execution_receipt_matches_receipt_item_id", "procurement_execution_receipt_matches",
            ["receipt_item_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "procurement_execution_receipt_matches" in _tables(bind):
        op.drop_table("procurement_execution_receipt_matches")
