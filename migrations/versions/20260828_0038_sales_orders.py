"""Sales orders MVP: customers, salespersons, sales_orders, sales_order_items"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0038"
down_revision = "20260825_0037"
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

    if "customers" not in tables:
        op.create_table(
            "customers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("wechat_name", sa.String(128), nullable=True),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    if "salespersons" not in tables:
        op.create_table(
            "salespersons",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    tables = _tables(bind)
    if "sales_orders" not in tables:
        op.create_table(
            "sales_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_no", sa.String(40), nullable=False, unique=True),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("salesperson_id", sa.Integer(), sa.ForeignKey("salespersons.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="submitted"),
            sa.Column("order_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('submitted','cancelled')", name="ck_sales_orders_status"),
        )

    if "sales_order_items" not in tables:
        op.create_table(
            "sales_order_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sales_order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
            sa.Column("product_name_snapshot", sa.String(255), nullable=False),
            sa.Column("jan_snapshot", sa.String(32), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_sale_price", sa.Numeric(18, 2), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("quantity > 0", name="ck_sales_order_items_quantity_positive"),
            sa.CheckConstraint("unit_sale_price >= 0", name="ck_sales_order_items_price_non_negative"),
        )

    indexes = _indexes(bind, "sales_orders")
    if "ix_sales_orders_customer_id" not in indexes:
        op.create_index("ix_sales_orders_customer_id", "sales_orders", ["customer_id"])
    if "ix_sales_orders_salesperson_id" not in indexes:
        op.create_index("ix_sales_orders_salesperson_id", "sales_orders", ["salesperson_id"])
    if "ix_sales_orders_status" not in indexes:
        op.create_index("ix_sales_orders_status", "sales_orders", ["status"])
    if "ix_sales_orders_created_at" not in indexes:
        op.create_index("ix_sales_orders_created_at", "sales_orders", ["created_at"])
    if "ix_sales_orders_status_created" not in indexes:
        op.create_index("ix_sales_orders_status_created", "sales_orders", ["status", "created_at"])

    indexes = _indexes(bind, "sales_order_items")
    if "ix_sales_order_items_sales_order_id" not in indexes:
        op.create_index("ix_sales_order_items_sales_order_id", "sales_order_items", ["sales_order_id"])
    if "ix_sales_order_items_product_id" not in indexes:
        op.create_index("ix_sales_order_items_product_id", "sales_order_items", ["product_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "sales_order_items" in tables:
        op.drop_table("sales_order_items")
    if "sales_orders" in tables:
        op.drop_table("sales_orders")
    if "salespersons" in tables:
        op.drop_table("salespersons")
    if "customers" in tables:
        op.drop_table("customers")
