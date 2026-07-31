"""product lifecycle operation log

Revision ID: 20260731_0028
Revises: 20260731_0027
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0028"
down_revision = "20260731_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("product_operation_logs"):
        return
    op.create_table(
        "product_operation_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("internal_sku", sa.String(32), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("action IN ('edit','archive','restore','delete')", name="ck_product_operation_logs_action"),
    )
    op.create_index("ix_product_operation_logs_product_id", "product_operation_logs", ["product_id"])
    op.create_index("ix_product_operation_logs_action", "product_operation_logs", ["action"])
    op.create_index(
        "ix_product_operation_logs_product_created",
        "product_operation_logs",
        ["product_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("product_operation_logs"):
        return
    op.drop_index("ix_product_operation_logs_product_created", table_name="product_operation_logs")
    op.drop_index("ix_product_operation_logs_action", table_name="product_operation_logs")
    op.drop_index("ix_product_operation_logs_product_id", table_name="product_operation_logs")
    op.drop_table("product_operation_logs")
