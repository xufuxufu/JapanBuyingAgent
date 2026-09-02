"""Sales order shipments, customer addresses, manual item images, CNY status expansion

Why this migration is needed (not reusable from existing schema):
- SalesOrder could previously only be shipped once as a whole; splitting/partial
  shipment needs new sales_shipments / sales_shipment_items tables.
- Customer previously had one free-text address column; multiple saved
  addresses per customer need a new one-to-many customer_addresses table.
- SalesOrderItem.product_name_snapshot was NOT NULL, blocking a manual item
  identified only by an uploaded photo; it must become nullable, and the item
  needs new manual-image columns plus a "some identity must be present" check
  (SQLite requires a full table rebuild to relax NOT NULL or add a CHECK).
- sales_orders.status only allowed ('submitted','ready_to_ship','shipped',
  'completed','cancelled'); the new shipment-driven state machine needs
  ('submitted','paid','partially_shipped','shipped','completed','cancelled').
  Historical 'ready_to_ship' rows are converted to 'paid' (the closest
  equivalent: order confirmed, not yet shipped) during the same rebuild so no
  existing order is left holding a status value the new CHECK would reject.
- sales_order_shipping_labels gets a new nullable shipment_id FK so future
  uploads attach to the shipment they document; existing order-level labels
  (shipment_id left NULL) stay exactly as they are and remain viewable.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0045"
down_revision = "20260828_0044"
branch_labels = None
depends_on = None


OLD_SALES_ORDER_STATUS_VALUES = ("submitted", "ready_to_ship", "shipped", "completed", "cancelled")
NEW_SALES_ORDER_STATUS_VALUES = ("submitted", "paid", "partially_shipped", "shipped", "completed", "cancelled")


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _table_sql(bind, table: str) -> str:
    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()
    return row[0] if row and row[0] else ""


# ---------------- customer_addresses ----------------


def _create_customer_addresses(bind) -> None:
    if "customer_addresses" in _tables(bind):
        return
    op.create_table(
        "customer_addresses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_name", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("label", sa.String(50), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_customer_addresses_customer_id", "customer_addresses", ["customer_id"])


# ---------------- sales_shipments / sales_shipment_items ----------------


def _create_sales_shipments(bind) -> None:
    if "sales_shipments" not in _tables(bind):
        op.create_table(
            "sales_shipments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("sales_order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("shipment_no", sa.String(40), nullable=False, unique=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("recipient_name_snapshot", sa.String(255), nullable=False),
            sa.Column("recipient_phone_snapshot", sa.String(50), nullable=True),
            sa.Column("shipping_address_snapshot", sa.Text(), nullable=False),
            sa.Column("carrier", sa.String(50), nullable=True, server_default="中通"),
            sa.Column("tracking_no", sa.String(100), nullable=True),
            sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("status IN ('pending','shipped')", name="ck_sales_shipments_status"),
        )
    indexes = _indexes(bind, "sales_shipments")
    if "ix_sales_shipments_sales_order_id" not in indexes:
        op.create_index("ix_sales_shipments_sales_order_id", "sales_shipments", ["sales_order_id"])
    if "ix_sales_shipments_status" not in indexes:
        op.create_index("ix_sales_shipments_status", "sales_shipments", ["status"])

    if "sales_shipment_items" not in _tables(bind):
        op.create_table(
            "sales_shipment_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("shipment_id", sa.Integer(), sa.ForeignKey("sales_shipments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sales_order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("quantity > 0", name="ck_sales_shipment_items_quantity_positive"),
        )
    indexes = _indexes(bind, "sales_shipment_items")
    if "ix_sales_shipment_items_shipment_id" not in indexes:
        op.create_index("ix_sales_shipment_items_shipment_id", "sales_shipment_items", ["shipment_id"])
    if "ix_sales_shipment_items_sales_order_item_id" not in indexes:
        op.create_index("ix_sales_shipment_items_sales_order_item_id", "sales_shipment_items", ["sales_order_item_id"])


# ---------------- sales_order_shipping_labels.shipment_id ----------------


def _add_shipping_label_shipment_id(bind) -> None:
    if "sales_order_shipping_labels" not in _tables(bind):
        return
    if "shipment_id" in _columns(bind, "sales_order_shipping_labels"):
        return
    # SQLite natively supports ALTER TABLE ADD COLUMN with an inline REFERENCES
    # clause in one statement -- but Alembic's op.add_column (outside batch
    # mode) tries to add the FK as a separate ALTER step ("No support for
    # ALTER of constraints in SQLite dialect"), and batch mode itself rejects
    # the unnamed inline ForeignKey ("Constraint must have a name"). Raw SQL
    # sidesteps both: this is exactly what SQLite itself accepts directly.
    bind.exec_driver_sql(
        "ALTER TABLE sales_order_shipping_labels "
        "ADD COLUMN shipment_id INTEGER REFERENCES sales_shipments(id) ON DELETE CASCADE"
    )
    op.create_index("ix_sales_order_shipping_labels_shipment_id", "sales_order_shipping_labels", ["shipment_id"])


# ---------------- sales_order_items rebuild ----------------


def _sales_order_items_columns(*, with_manual_image: bool, name_nullable: bool) -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sales_order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_name_snapshot", sa.String(255), nullable=name_nullable),
        sa.Column("jan_snapshot", sa.String(32), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_sale_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        *([
            sa.Column("manual_image_relative_path", sa.Text(), nullable=True),
            sa.Column("manual_image_original_filename", sa.String(255), nullable=True),
            sa.Column("manual_image_content_type", sa.String(100), nullable=True),
            sa.Column("manual_image_file_size", sa.Integer(), nullable=True),
        ] if with_manual_image else []),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _rebuild_sales_order_items(bind, *, with_manual_image: bool, name_nullable: bool) -> None:
    # Same rationale/ordering as _rebuild_sales_orders in 20260828_0039: turn
    # off FK enforcement first (must be the first statement in the
    # transaction), drop the live table, then rename the rebuilt temp table
    # into place -- never rename the live table itself, since sales_shipment_
    # items.sales_order_item_id must keep pointing at the "sales_order_items"
    # name throughout.
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    temp_name = "_sales_order_items_rebuild"
    columns = _sales_order_items_columns(with_manual_image=with_manual_image, name_nullable=name_nullable)
    checks = [
        sa.CheckConstraint("quantity > 0", name="ck_sales_order_items_quantity_positive"),
        sa.CheckConstraint("unit_sale_price >= 0", name="ck_sales_order_items_price_non_negative"),
    ]
    if name_nullable:
        checks.append(sa.CheckConstraint(
            "product_id IS NOT NULL OR product_name_snapshot IS NOT NULL OR manual_image_relative_path IS NOT NULL",
            name="ck_sales_order_items_identity_present",
        ))

    metadata = sa.MetaData()
    sa.Table("sales_orders", metadata, autoload_with=bind)
    sa.Table("products", metadata, autoload_with=bind)
    temp = sa.Table(temp_name, metadata, *columns, *checks)
    temp.create(bind)

    live_columns = _columns(bind, "sales_order_items")
    copy_names = [name for name in (column.name for column in columns) if name in live_columns]
    preparer = bind.dialect.identifier_preparer
    quoted = ", ".join(preparer.quote(name) for name in copy_names)
    op.execute(sa.text(
        f"INSERT INTO {preparer.quote(temp_name)} ({quoted}) "
        f"SELECT {quoted} FROM {preparer.quote('sales_order_items')}"
    ))
    op.drop_table("sales_order_items")
    op.rename_table(temp_name, "sales_order_items")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    existing_index_names = {index["name"] for index in sa.inspect(bind).get_indexes("sales_order_items")}
    if "ix_sales_order_items_sales_order_id" not in existing_index_names:
        op.create_index("ix_sales_order_items_sales_order_id", "sales_order_items", ["sales_order_id"])
    if "ix_sales_order_items_product_id" not in existing_index_names:
        op.create_index("ix_sales_order_items_product_id", "sales_order_items", ["product_id"])


# ---------------- sales_orders rebuild (status widen + ready_to_ship -> paid) ----------------


def _sales_orders_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_no", sa.String(40), nullable=False, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("salesperson_id", sa.Integer(), sa.ForeignKey("salespersons.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitted"),
        sa.Column("order_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recipient_name_snapshot", sa.String(255), nullable=True),
        sa.Column("recipient_phone_snapshot", sa.String(50), nullable=True),
        sa.Column("shipping_address_snapshot", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _rebuild_sales_orders_status(bind) -> None:
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")

    temp_name = "_sales_orders_rebuild_0045"
    columns = _sales_orders_columns()
    status_list_sql = ",".join(f"'{value}'" for value in NEW_SALES_ORDER_STATUS_VALUES)
    check = sa.CheckConstraint(f"status IN ({status_list_sql})", name="ck_sales_orders_status")

    metadata = sa.MetaData()
    sa.Table("customers", metadata, autoload_with=bind)
    sa.Table("salespersons", metadata, autoload_with=bind)
    temp = sa.Table(temp_name, metadata, *columns, check)
    temp.create(bind)

    live_columns = _columns(bind, "sales_orders")
    copy_names = [name for name in (column.name for column in columns) if name in live_columns]
    preparer = bind.dialect.identifier_preparer
    quoted = ", ".join(preparer.quote(name) for name in copy_names)
    # Data-convert the retired 'ready_to_ship' value to its closest equivalent
    # in the new state machine ('paid': confirmed, not yet shipped) so no row
    # is left holding a status the new CHECK constraint would reject.
    select_names = [
        "CASE WHEN status='ready_to_ship' THEN 'paid' ELSE status END AS status" if name == "status" else preparer.quote(name)
        for name in copy_names
    ]
    select_sql = ", ".join(select_names)
    op.execute(sa.text(
        f"INSERT INTO {preparer.quote(temp_name)} ({quoted}) "
        f"SELECT {select_sql} FROM {preparer.quote('sales_orders')}"
    ))
    op.drop_table("sales_orders")
    op.rename_table(temp_name, "sales_orders")
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")

    existing_index_names = {index["name"] for index in sa.inspect(bind).get_indexes("sales_orders")}
    for name, index_columns in (
        ("ix_sales_orders_customer_id", ["customer_id"]),
        ("ix_sales_orders_salesperson_id", ["salesperson_id"]),
        ("ix_sales_orders_status", ["status"]),
        ("ix_sales_orders_created_at", ["created_at"]),
        ("ix_sales_orders_status_created", ["status", "created_at"]),
    ):
        if name not in existing_index_names:
            op.create_index(name, "sales_orders", index_columns)


def _sales_orders_already_migrated(bind) -> bool:
    return "partially_shipped" in _table_sql(bind, "sales_orders")


def _sales_order_items_already_migrated(bind) -> bool:
    return "manual_image_relative_path" in _columns(bind, "sales_order_items")


def upgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in _tables(bind):
        # Fresh installs run this migration as part of the same head chain
        # that first creates sales_orders (0038); if it isn't there yet,
        # there is nothing for this migration to touch.
        return

    _create_customer_addresses(bind)
    _create_sales_shipments(bind)
    _add_shipping_label_shipment_id(bind)

    if not _sales_order_items_already_migrated(bind):
        _rebuild_sales_order_items(bind, with_manual_image=True, name_nullable=True)

    if not _sales_orders_already_migrated(bind):
        _rebuild_sales_orders_status(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if "sales_orders" not in _tables(bind):
        return

    if _sales_orders_already_migrated(bind):
        blocking = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM sales_orders WHERE status IN ('paid','partially_shipped')"
        ).scalar()
        if blocking:
            raise RuntimeError(
                "无法降级到 20260828_0044：存在状态为 paid/partially_shipped 的销售订单，"
                "这两个状态在旧版本中不存在，请先人工处理这些订单后再降级"
                "（不允许自动批量修改历史订单状态）",
            )
        _rebuild_sales_orders_status_down(bind)

    if _sales_order_items_already_migrated(bind):
        blocking_items = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM sales_order_items WHERE product_name_snapshot IS NULL "
            "OR manual_image_relative_path IS NOT NULL"
        ).scalar()
        if blocking_items:
            raise RuntimeError(
                "无法降级到 20260828_0044：存在无商品名快照或带手工图片的订单行，"
                "旧版本的 sales_order_items 要求商品名快照必填，"
                "请先人工处理这些订单行后再降级",
            )
        _rebuild_sales_order_items(bind, with_manual_image=False, name_nullable=False)

    if "sales_order_shipping_labels" in _tables(bind) and "shipment_id" in _columns(bind, "sales_order_shipping_labels"):
        blocking_labels = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM sales_order_shipping_labels WHERE shipment_id IS NOT NULL"
        ).scalar()
        if blocking_labels:
            raise RuntimeError(
                "无法降级到 20260828_0044：存在已关联发货单(shipment)的发货面单图片，"
                "请先人工处理后再降级",
            )
        op.drop_index("ix_sales_order_shipping_labels_shipment_id", table_name="sales_order_shipping_labels")
        # Mirrors the raw-SQL ADD COLUMN above; modern SQLite (3.35+) supports
        # DROP COLUMN natively, avoiding the same Alembic/batch-mode FK issue.
        bind.exec_driver_sql("ALTER TABLE sales_order_shipping_labels DROP COLUMN shipment_id")

    if "sales_shipment_items" in _tables(bind):
        op.drop_table("sales_shipment_items")
    if "sales_shipments" in _tables(bind):
        op.drop_table("sales_shipments")
    if "customer_addresses" in _tables(bind):
        op.drop_table("customer_addresses")


def _rebuild_sales_orders_status_down(bind) -> None:
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    temp_name = "_sales_orders_rebuild_0045_down"
    columns = _sales_orders_columns()
    status_list_sql = ",".join(f"'{value}'" for value in OLD_SALES_ORDER_STATUS_VALUES)
    check = sa.CheckConstraint(f"status IN ({status_list_sql})", name="ck_sales_orders_status")

    metadata = sa.MetaData()
    sa.Table("customers", metadata, autoload_with=bind)
    sa.Table("salespersons", metadata, autoload_with=bind)
    temp = sa.Table(temp_name, metadata, *columns, check)
    temp.create(bind)

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
    for name, index_columns in (
        ("ix_sales_orders_customer_id", ["customer_id"]),
        ("ix_sales_orders_salesperson_id", ["salesperson_id"]),
        ("ix_sales_orders_status", ["status"]),
        ("ix_sales_orders_created_at", ["created_at"]),
        ("ix_sales_orders_status_created", ["status", "created_at"]),
    ):
        if name not in existing_index_names:
            op.create_index(name, "sales_orders", index_columns)
