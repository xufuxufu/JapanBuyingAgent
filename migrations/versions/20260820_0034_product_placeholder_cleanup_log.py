"""Product placeholder cleanup audit log"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_0034"
down_revision = "20260820_0033"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "product_placeholder_cleanup_logs" in _tables(bind):
        return
    op.create_table(
        "product_placeholder_cleanup_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("old_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
        sa.Column("new_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
        sa.Column("jan", sa.String(32), nullable=False),
        sa.Column("migrated_association_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operation_type", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_product_placeholder_cleanup_old", "product_placeholder_cleanup_logs", ["old_product_id", "created_at"])
    op.create_index("ix_product_placeholder_cleanup_new", "product_placeholder_cleanup_logs", ["new_product_id", "created_at"])
    op.create_index("ix_product_placeholder_cleanup_jan", "product_placeholder_cleanup_logs", ["jan"])


def downgrade() -> None:
    bind = op.get_bind()
    if "product_placeholder_cleanup_logs" not in _tables(bind):
        return
    op.drop_index("ix_product_placeholder_cleanup_jan", table_name="product_placeholder_cleanup_logs")
    op.drop_index("ix_product_placeholder_cleanup_new", table_name="product_placeholder_cleanup_logs")
    op.drop_index("ix_product_placeholder_cleanup_old", table_name="product_placeholder_cleanup_logs")
    op.drop_table("product_placeholder_cleanup_logs")
