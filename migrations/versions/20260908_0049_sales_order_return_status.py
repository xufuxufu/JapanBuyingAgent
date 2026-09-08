"""Sales orders: minimal after-sales return status (none/returning/returned)"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0049"
down_revision = "20260905_0048"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind, "sales_orders")
    if "return_status" not in columns:
        op.add_column(
            "sales_orders",
            sa.Column("return_status", sa.String(20), nullable=False, server_default="none"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind, "sales_orders")
    if "return_status" in columns:
        op.drop_column("sales_orders", "return_status")
