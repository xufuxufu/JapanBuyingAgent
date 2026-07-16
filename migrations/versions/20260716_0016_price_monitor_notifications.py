"""scheduled price monitoring and web notifications"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0016"
down_revision = "20260716_0015"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "price_lookup_histories" in tables and "lookup_source" not in _columns(bind, "price_lookup_histories"):
        op.add_column(
            "price_lookup_histories",
            sa.Column("lookup_source", sa.String(20), server_default="manual", nullable=False),
        )
        op.create_index("ix_price_lookup_histories_lookup_source", "price_lookup_histories", ["lookup_source"])

    if "product_watch_configs" in tables:
        columns = _columns(bind, "product_watch_configs")
        additions = (
            sa.Column("last_check_at", sa.DateTime(timezone=True)),
            sa.Column("next_check_at", sa.DateTime(timezone=True)),
            sa.Column("current_lowest_price", sa.Integer()),
            sa.Column("previous_lowest_price", sa.Integer()),
            sa.Column("historical_online_lowest_price", sa.Integer()),
            sa.Column("last_in_stock", sa.Boolean()),
            sa.Column("last_provider_codes", sa.String(255)),
            sa.Column("last_check_status", sa.String(30)),
            sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_error_summary", sa.Text()),
            sa.Column("failure_notification_sent", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
        for column in additions:
            if column.name not in columns:
                op.add_column("product_watch_configs", column)
        index_names = {index["name"] for index in sa.inspect(bind).get_indexes("product_watch_configs")}
        if "ix_product_watch_configs_due" not in index_names:
            op.create_index("ix_product_watch_configs_due", "product_watch_configs", ["enabled", "next_check_at"])

    tables = set(sa.inspect(bind).get_table_names())
    if "product_watch_snapshots" not in tables:
        op.create_table(
            "product_watch_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("watch_config_id", sa.Integer(), sa.ForeignKey("product_watch_configs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("price_lookup_history_id", sa.Integer(), sa.ForeignKey("price_lookup_histories.id", ondelete="SET NULL")),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lowest_item_price", sa.Integer()),
            sa.Column("shipping_price", sa.Integer()),
            sa.Column("total_price", sa.Integer()),
            sa.Column("marketplace", sa.String(100)),
            sa.Column("seller", sa.String(255)),
            sa.Column("url", sa.Text()),
            sa.Column("is_in_stock", sa.Boolean()),
            sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("provider_codes", sa.String(255)),
            sa.Column("error_summary", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint("status IN ('success','failed')", name="ck_product_watch_snapshots_status"),
        )
        op.create_index("ix_product_watch_snapshots_product_time", "product_watch_snapshots", ["product_id", "checked_at"])
        op.create_index("ix_product_watch_snapshots_config_time", "product_watch_snapshots", ["watch_config_id", "checked_at"])
        op.create_index("ix_product_watch_snapshots_price_lookup_history_id", "product_watch_snapshots", ["price_lookup_history_id"])

    tables = set(sa.inspect(bind).get_table_names())
    if "product_watch_notifications" not in tables:
        op.create_table(
            "product_watch_notifications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("watch_config_id", sa.Integer(), sa.ForeignKey("product_watch_configs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("product_watch_snapshots.id", ondelete="SET NULL")),
            sa.Column("event_type", sa.String(30), nullable=False),
            sa.Column("target_price", sa.Integer()),
            sa.Column("current_price", sa.Integer()),
            sa.Column("marketplace", sa.String(100)),
            sa.Column("seller", sa.String(255)),
            sa.Column("url", sa.Text()),
            sa.Column("dedupe_key", sa.String(255), nullable=False),
            sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("data_updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("is_read", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("read_at", sa.DateTime(timezone=True)),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "event_type IN ('target_reached','new_historical_low','restocked','monitor_failed')",
                name="ck_product_watch_notifications_type",
            ),
        )
        op.create_index("uq_product_watch_notifications_dedupe", "product_watch_notifications", ["dedupe_key"], unique=True)
        op.create_index("ix_product_watch_notifications_unread", "product_watch_notifications", ["is_read", "archived_at", "triggered_at"])
        op.create_index("ix_product_watch_notifications_product", "product_watch_notifications", ["product_id"])
        op.create_index("ix_product_watch_notifications_snapshot_id", "product_watch_notifications", ["snapshot_id"])

    tables = set(sa.inspect(bind).get_table_names())
    if "monitor_scheduler_states" not in tables:
        op.create_table(
            "monitor_scheduler_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(30), nullable=False, unique=True),
            sa.Column("last_scan_started_at", sa.DateTime(timezone=True)),
            sa.Column("last_scan_completed_at", sa.DateTime(timezone=True)),
            sa.Column("last_success_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_failure_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_error_summary", sa.Text()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("monitor_scheduler_states", "product_watch_notifications", "product_watch_snapshots"):
        if table in tables:
            op.drop_table(table)
    if "product_watch_configs" in tables:
        columns = _columns(bind, "product_watch_configs")
        with op.batch_alter_table("product_watch_configs") as batch:
            for name in (
                "failure_notification_sent", "last_error_summary", "consecutive_failures", "last_check_status",
                "last_provider_codes", "last_in_stock", "historical_online_lowest_price", "previous_lowest_price",
                "current_lowest_price", "next_check_at", "last_check_at",
            ):
                if name in columns:
                    batch.drop_column(name)
    if "price_lookup_histories" in tables and "lookup_source" in _columns(bind, "price_lookup_histories"):
        with op.batch_alter_table("price_lookup_histories") as batch:
            batch.drop_column("lookup_source")
