"""Procurement purchase executions: actual buys against a plan, pre-receipt (Phase 2D)"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0043"
down_revision = "20260828_0042"
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

    if "procurement_purchase_executions" not in tables:
        op.create_table(
            "procurement_purchase_executions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "plan_id", sa.Integer(), sa.ForeignKey("procurement_demand_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="SET NULL"), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending_receipt"),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("quantity > 0", name="ck_procurement_purchase_executions_quantity_positive"),
            sa.CheckConstraint(
                "status IN ('pending_receipt','reconciled','cancelled')",
                name="ck_procurement_purchase_executions_status",
            ),
        )

    indexes = _indexes(bind, "procurement_purchase_executions")
    if "ix_procurement_purchase_executions_plan_id" not in indexes:
        op.create_index("ix_procurement_purchase_executions_plan_id", "procurement_purchase_executions", ["plan_id"])
    if "ix_procurement_purchase_executions_store_id" not in indexes:
        op.create_index("ix_procurement_purchase_executions_store_id", "procurement_purchase_executions", ["store_id"])
    if "ix_procurement_purchase_executions_status" not in indexes:
        op.create_index("ix_procurement_purchase_executions_status", "procurement_purchase_executions", ["status"])
    if "ix_procurement_purchase_executions_created_at" not in indexes:
        op.create_index(
            "ix_procurement_purchase_executions_created_at", "procurement_purchase_executions", ["created_at"],
        )
    if "ix_procurement_purchase_executions_plan_status" not in indexes:
        op.create_index(
            "ix_procurement_purchase_executions_plan_status", "procurement_purchase_executions",
            ["plan_id", "status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "procurement_purchase_executions" in _tables(bind):
        op.drop_table("procurement_purchase_executions")
