"""Procurement demand plan: selected purchase source store (Phase 2C)"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0042"
down_revision = "20260828_0041"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # Adding a column with a foreign key requires Alembic's SQLite batch mode
    # (copy-and-move): plain op.add_column's separate add_constraint step isn't
    # supported by the SQLite dialect. Batch mode is Alembic's own supported
    # strategy for this and safely handles the foreign_keys pragma around the
    # copy -- unlike 0039's original hand-written rebuild (see
    # KNOWN_ISSUES_AND_ROADMAP), this never touches sales_orders/sales_order_items
    # and only rebuilds procurement_demand_plans itself.
    if "selected_store_id" not in _columns(bind, "procurement_demand_plans"):
        has_stores = "stores" in _tables(bind)
        with op.batch_alter_table("procurement_demand_plans") as batch_op:
            batch_op.add_column(sa.Column("selected_store_id", sa.Integer(), nullable=True))
            if has_stores:
                batch_op.create_foreign_key(
                    "fk_procurement_demand_plans_selected_store", "stores", ["selected_store_id"], ["id"],
                    ondelete="SET NULL",
                )
            # Some old migration-regression fixtures intentionally represent a
            # database state that never created "stores" (see 20260716_0013,
            # which only alters "stores" if it already exists rather than
            # creating it). A real application database always has it by now;
            # skip only the foreign key there so the column still lands.
    if "ix_procurement_demand_plans_selected_store_id" not in _indexes(bind, "procurement_demand_plans"):
        op.create_index(
            "ix_procurement_demand_plans_selected_store_id", "procurement_demand_plans", ["selected_store_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_procurement_demand_plans_selected_store_id" in _indexes(bind, "procurement_demand_plans"):
        op.drop_index("ix_procurement_demand_plans_selected_store_id", table_name="procurement_demand_plans")
    if "selected_store_id" in _columns(bind, "procurement_demand_plans"):
        # SQLite does not preserve the FK constraint's name in the recreated
        # table (see upgrade()), so there is nothing to drop_constraint by name
        # here -- dropping the column removes its foreign key along with it.
        with op.batch_alter_table("procurement_demand_plans") as batch_op:
            batch_op.drop_column("selected_store_id")
