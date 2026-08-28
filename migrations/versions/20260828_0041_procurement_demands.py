"""Procurement demand pool: raw demand sources, plus lightweight planned-purchase decisions"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0041"
down_revision = "20260828_0040"
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

    if "procurement_demands" not in tables:
        op.create_table(
            "procurement_demands",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
            sa.Column("product_name_snapshot", sa.String(255), nullable=False),
            sa.Column("jan_snapshot", sa.String(32), nullable=True),
            sa.Column("demand_type", sa.String(20), nullable=False),
            sa.Column("source_person", sa.String(20), nullable=False),
            sa.Column("source_channel", sa.String(50), nullable=True),
            sa.Column("source_type", sa.String(20), nullable=False),
            sa.Column(
                "sales_order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("requested_quantity", sa.Integer(), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "demand_type IN ('sales_confirmed','channel_shortage','manual_restock','investigation','system_restock')",
                name="ck_procurement_demands_demand_type",
            ),
            sa.CheckConstraint("source_person IN ('秀','丈母娘','老婆','系统')", name="ck_procurement_demands_source_person"),
            sa.CheckConstraint(
                "source_type IN ('sales_order','channel_shortage','manual','investigation','system_restock')",
                name="ck_procurement_demands_source_type",
            ),
            sa.CheckConstraint("status IN ('open','planned','closed','cancelled')", name="ck_procurement_demands_status"),
            sa.CheckConstraint(
                "requested_quantity IS NULL OR requested_quantity > 0", name="ck_procurement_demands_quantity_positive",
            ),
        )

    if "procurement_demand_plans" not in tables:
        op.create_table(
            "procurement_demand_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
            sa.Column("product_name_snapshot", sa.String(255), nullable=False),
            sa.Column("jan_snapshot", sa.String(32), nullable=True),
            sa.Column("planned_quantity", sa.Integer(), nullable=False),
            sa.Column("confirmed_demand_quantity_snapshot", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
            sa.Column("created_by", sa.String(20), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("planned_quantity > 0", name="ck_procurement_demand_plans_quantity_positive"),
            sa.CheckConstraint("status IN ('planned','cancelled')", name="ck_procurement_demand_plans_status"),
        )

    if "procurement_demand_plan_sources" not in tables:
        op.create_table(
            "procurement_demand_plan_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "plan_id", sa.Integer(), sa.ForeignKey("procurement_demand_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "demand_id", sa.Integer(), sa.ForeignKey("procurement_demands.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("quantity_snapshot", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("plan_id", "demand_id", name="uq_procurement_demand_plan_sources_plan_demand"),
        )

    indexes = _indexes(bind, "procurement_demands")
    if "ix_procurement_demands_product_id" not in indexes:
        op.create_index("ix_procurement_demands_product_id", "procurement_demands", ["product_id"])
    if "ix_procurement_demands_demand_type" not in indexes:
        op.create_index("ix_procurement_demands_demand_type", "procurement_demands", ["demand_type"])
    if "ix_procurement_demands_sales_order_item_id" not in indexes:
        op.create_index(
            "ix_procurement_demands_sales_order_item_id", "procurement_demands", ["sales_order_item_id"],
        )
    if "ix_procurement_demands_status" not in indexes:
        op.create_index("ix_procurement_demands_status", "procurement_demands", ["status"])
    if "ix_procurement_demands_created_at" not in indexes:
        op.create_index("ix_procurement_demands_created_at", "procurement_demands", ["created_at"])
    if "ix_procurement_demands_status_type" not in indexes:
        op.create_index("ix_procurement_demands_status_type", "procurement_demands", ["status", "demand_type"])
    if "ix_procurement_demands_product_status" not in indexes:
        op.create_index("ix_procurement_demands_product_status", "procurement_demands", ["product_id", "status"])
    if "uq_procurement_demands_sales_order_item" not in indexes:
        op.create_index(
            "uq_procurement_demands_sales_order_item", "procurement_demands", ["sales_order_item_id"],
            unique=True, sqlite_where=sa.text("sales_order_item_id IS NOT NULL"),
        )

    indexes = _indexes(bind, "procurement_demand_plans")
    if "ix_procurement_demand_plans_product_id" not in indexes:
        op.create_index("ix_procurement_demand_plans_product_id", "procurement_demand_plans", ["product_id"])
    if "ix_procurement_demand_plans_status" not in indexes:
        op.create_index("ix_procurement_demand_plans_status", "procurement_demand_plans", ["status"])
    if "ix_procurement_demand_plans_created_at" not in indexes:
        op.create_index("ix_procurement_demand_plans_created_at", "procurement_demand_plans", ["created_at"])
    if "ix_procurement_demand_plans_status_created" not in indexes:
        op.create_index(
            "ix_procurement_demand_plans_status_created", "procurement_demand_plans", ["status", "created_at"],
        )

    indexes = _indexes(bind, "procurement_demand_plan_sources")
    if "ix_procurement_demand_plan_sources_plan_id" not in indexes:
        op.create_index(
            "ix_procurement_demand_plan_sources_plan_id", "procurement_demand_plan_sources", ["plan_id"],
        )
    if "ix_procurement_demand_plan_sources_demand_id" not in indexes:
        op.create_index(
            "ix_procurement_demand_plan_sources_demand_id", "procurement_demand_plan_sources", ["demand_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "procurement_demand_plan_sources" in tables:
        op.drop_table("procurement_demand_plan_sources")
    if "procurement_demand_plans" in tables:
        op.drop_table("procurement_demand_plans")
    if "procurement_demands" in tables:
        op.drop_table("procurement_demands")
