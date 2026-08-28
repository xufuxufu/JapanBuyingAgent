"""Sales order shipping labels (domestic warehouse dispatch photos)"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0040"
down_revision = "20260828_0039"
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
    if "sales_order_shipping_labels" not in _tables(bind):
        op.create_table(
            "sales_order_shipping_labels",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sales_order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("stored_filename", sa.String(255), nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=True),
            sa.Column("relative_path", sa.Text(), nullable=False),
            sa.Column("content_type", sa.String(100), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    indexes = _indexes(bind, "sales_order_shipping_labels")
    if "ix_sales_order_shipping_labels_sales_order_id" not in indexes:
        op.create_index(
            "ix_sales_order_shipping_labels_sales_order_id", "sales_order_shipping_labels", ["sales_order_id"],
        )
    if "ix_sales_order_shipping_labels_created_at" not in indexes:
        op.create_index(
            "ix_sales_order_shipping_labels_created_at", "sales_order_shipping_labels", ["created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "sales_order_shipping_labels" in _tables(bind):
        op.drop_table("sales_order_shipping_labels")
