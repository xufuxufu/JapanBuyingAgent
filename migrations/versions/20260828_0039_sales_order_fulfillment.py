"""Sales order fulfillment workflow: extended statuses and shipping snapshot"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_0039"
down_revision = "20260828_0038"
branch_labels = None
depends_on = None


OLD_STATUS_VALUES = ("submitted", "cancelled")
NEW_STATUS_VALUES = ("submitted", "ready_to_ship", "shipped", "completed", "cancelled")

SALES_ORDER_INDEXES = (
    ("ix_sales_orders_customer_id", ["customer_id"], False),
    ("ix_sales_orders_salesperson_id", ["salesperson_id"], False),
    ("ix_sales_orders_status", ["status"], False),
    ("ix_sales_orders_created_at", ["created_at"], False),
    ("ix_sales_orders_status_created", ["status", "created_at"], False),
)


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _table_sql(bind, table: str) -> str:
    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()
    return row[0] if row and row[0] else ""


def _sales_orders_columns(*, include_snapshot: bool) -> list[sa.Column]:
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_no", sa.String(40), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("salesperson_id", sa.Integer(), sa.ForeignKey("salespersons.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitted"),
        sa.Column("order_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]
    if include_snapshot:
        columns += [
            sa.Column("recipient_name_snapshot", sa.String(255), nullable=True),
            sa.Column("recipient_phone_snapshot", sa.String(50), nullable=True),
            sa.Column("shipping_address_snapshot", sa.Text(), nullable=True),
        ]
    return columns


def _rebuild_sales_orders(bind, *, status_values: tuple[str, ...], include_snapshot: bool) -> None:
    # PRAGMA foreign_keys must be toggled off before ANY statement (DDL or DML) runs
    # in the current transaction -- SQLite silently no-ops the pragma once a
    # transaction has open writes, so this has to be the very first thing this
    # function does. With it off:
    #  - DROP TABLE skips SQLite's whole-schema FK re-validation (which otherwise
    #    aborts the drop over any dangling FK anywhere in the schema, even in tables
    #    completely unrelated to sales_orders).
    #  - dependent tables (e.g. sales_order_items' "REFERENCES sales_orders") are not
    #    at risk either way here, because we DROP the old table and then RENAME the
    #    rebuilt one into the now-vacant "sales_orders" name -- we never rename the
    #    table that others still reference, so nothing needs fixing up.
    # (An earlier version of this migration renamed the OLD table out of the way
    # instead of dropping it, to dodge the FK re-validation issue -- but SQLite's
    # rename then rewrote sales_order_items' FK to follow the renamed-away table,
    # corrupting it. Drop-then-rename avoids both problems at once.)
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    temp_name = "_sales_orders_rebuild"
    columns = _sales_orders_columns(include_snapshot=include_snapshot)
    status_list_sql = ",".join(f"'{value}'" for value in status_values)
    check = sa.CheckConstraint(f"status IN ({status_list_sql})", name="ck_sales_orders_status")

    metadata = sa.MetaData()
    sa.Table("customers", metadata, autoload_with=bind)
    sa.Table("salespersons", metadata, autoload_with=bind)
    temp = sa.Table(temp_name, metadata, *columns, check)
    temp.create(bind)

    # only copy columns that also exist on the current live table
    live_columns = _columns(bind, "sales_orders")
    copy_names = [name for name in (column.name for column in columns) if name in live_columns]
    preparer = bind.dialect.identifier_preparer
    quoted = ", ".join(preparer.quote(name) for name in copy_names)
    op.execute(sa.text(
        f"INSERT INTO {preparer.quote(temp_name)} ({quoted}) "
        f"SELECT {quoted} FROM {preparer.quote('sales_orders')}"
    ))
    op.drop_table("sales_orders")
    op.rename_table(temp_name, "sales_orders")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    existing_index_names = {index["name"] for index in sa.inspect(bind).get_indexes("sales_orders")}
    for name, index_columns, unique in SALES_ORDER_INDEXES:
        if name not in existing_index_names:
            op.create_index(name, "sales_orders", index_columns, unique=unique)


def _already_migrated(bind) -> bool:
    columns = _columns(bind, "sales_orders")
    if "recipient_name_snapshot" not in columns:
        return False
    return "ready_to_ship" in _table_sql(bind, "sales_orders")


def upgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in _tables(bind):
        return
    if _already_migrated(bind):
        return
    _rebuild_sales_orders(bind, status_values=NEW_STATUS_VALUES, include_snapshot=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in _tables(bind):
        return
    if "recipient_name_snapshot" not in _columns(bind, "sales_orders"):
        return
    blocking = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM sales_orders WHERE status NOT IN ('submitted','cancelled')"
    ).scalar()
    if blocking:
        raise RuntimeError(
            "无法降级到 20260828_0038：存在状态不是 submitted/cancelled 的销售订单，"
            "请先人工处理这些订单后再降级（不允许自动批量修改历史订单状态）",
        )
    _rebuild_sales_orders(bind, status_values=OLD_STATUS_VALUES, include_snapshot=False)
