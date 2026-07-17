from alembic import op
import sqlalchemy as sa


revision = "20260717_0018"
down_revision = "20260716_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "restock_lists" not in tables:
        op.create_table(
            "restock_lists",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("status", sa.String(20), server_default="draft", nullable=False),
            sa.Column("source_type", sa.String(30), server_default="manual", nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint("status IN ('draft','active','completed','cancelled')", name="ck_restock_lists_status"),
            sa.CheckConstraint(
                "source_type IN ('manual','store_history','watched_products','purchase_analysis')",
                name="ck_restock_lists_source_type",
            ),
        )
        op.create_index("ix_restock_lists_store_id", "restock_lists", ["store_id"])
        op.create_index("ix_restock_lists_status", "restock_lists", ["status"])
        op.create_index("ix_restock_lists_created_at", "restock_lists", ["created_at"])
        op.create_index("ix_restock_lists_store_status", "restock_lists", ["store_id", "status"])
        op.create_index("ix_restock_lists_status_created", "restock_lists", ["status", "created_at"])

    tables = set(sa.inspect(bind).get_table_names())
    if "restock_list_items" not in tables:
        op.create_table(
            "restock_list_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("restock_list_id", sa.Integer(), sa.ForeignKey("restock_lists.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("added_source", sa.String(30), server_default="manual", nullable=False),
            sa.Column("sort_value", sa.Integer(), server_default="1000", nullable=False),
            sa.Column("planned_quantity", sa.Integer()),
            sa.Column("target_purchase_price_snapshot", sa.Integer()),
            sa.Column("latest_purchase_price_snapshot", sa.Integer()),
            sa.Column("historical_lowest_purchase_price_snapshot", sa.Integer()),
            sa.Column("latest_store_purchase_price_snapshot", sa.Integer()),
            sa.Column("store_lowest_purchase_price_snapshot", sa.Integer()),
            sa.Column("latest_store_purchase_at", sa.DateTime(timezone=True)),
            sa.Column("qinsi_quantity_snapshot", sa.Integer()),
            sa.Column("qinsi_snapshot_at", sa.DateTime(timezone=True)),
            sa.Column("online_lowest_price_snapshot", sa.Integer()),
            sa.Column("online_price_checked_at", sa.DateTime(timezone=True)),
            sa.Column("recommendation_reason", sa.Text()),
            sa.Column("status", sa.String(20), server_default="to_check", nullable=False),
            sa.Column("notes", sa.Text()),
            sa.Column("actual_purchase_quantity", sa.Integer()),
            sa.Column("actual_purchase_price", sa.Integer()),
            sa.Column("watch_config_id", sa.Integer(), sa.ForeignKey("product_watch_configs.id", ondelete="SET NULL")),
            sa.Column("online_snapshot_id", sa.Integer(), sa.ForeignKey("product_watch_snapshots.id", ondelete="SET NULL")),
            sa.Column("qinsi_snapshot_id", sa.Integer(), sa.ForeignKey("qinsi_inventory_snapshots.id", ondelete="SET NULL")),
            sa.Column("purchase_batch_item_id", sa.Integer(), sa.ForeignKey("purchase_batch_items.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("restock_list_id", "product_id", name="uq_restock_list_items_list_product"),
            sa.CheckConstraint("status IN ('to_check','found','not_found','purchased','skipped')", name="ck_restock_list_items_status"),
            sa.CheckConstraint("planned_quantity IS NULL OR planned_quantity > 0", name="ck_restock_items_planned_quantity"),
            sa.CheckConstraint("actual_purchase_quantity IS NULL OR actual_purchase_quantity > 0", name="ck_restock_items_actual_quantity"),
            sa.CheckConstraint("actual_purchase_price IS NULL OR actual_purchase_price > 0", name="ck_restock_items_actual_price"),
        )
        op.create_index("ix_restock_list_items_restock_list_id", "restock_list_items", ["restock_list_id"])
        op.create_index("ix_restock_list_items_product_id", "restock_list_items", ["product_id"])
        op.create_index("ix_restock_list_items_status", "restock_list_items", ["status"])
        op.create_index("ix_restock_list_items_watch_config_id", "restock_list_items", ["watch_config_id"])
        op.create_index("ix_restock_list_items_online_snapshot_id", "restock_list_items", ["online_snapshot_id"])
        op.create_index("ix_restock_list_items_qinsi_snapshot_id", "restock_list_items", ["qinsi_snapshot_id"])
        op.create_index("ix_restock_list_items_purchase_batch_item_id", "restock_list_items", ["purchase_batch_item_id"])
        op.create_index("ix_restock_list_items_product_status", "restock_list_items", ["product_id", "status"])
        op.create_index("ix_restock_list_items_list_sort", "restock_list_items", ["restock_list_id", "sort_value"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "restock_list_items" in tables:
        op.drop_table("restock_list_items")
    if "restock_lists" in tables:
        op.drop_table("restock_lists")
