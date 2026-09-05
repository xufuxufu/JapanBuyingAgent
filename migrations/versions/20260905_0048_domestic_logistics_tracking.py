"""Domestic (中通-only) logistics tracking fields and events (Phase 10A)"""
from alembic import op
import sqlalchemy as sa


revision = "20260905_0048"
down_revision = "20260904_0047"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "sales_shipments" in tables:
        columns = _columns(bind, "sales_shipments")
        additions = (
            sa.Column("tracking_status", sa.String(20)),
            sa.Column("tracking_terminal", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("tracking_last_checked_at", sa.DateTime(timezone=True)),
            sa.Column("tracking_last_event_at", sa.DateTime(timezone=True)),
            sa.Column("tracking_next_check_at", sa.DateTime(timezone=True)),
            sa.Column("tracking_error", sa.Text()),
        )
        for column in additions:
            if column.name not in columns:
                op.add_column("sales_shipments", column)
        index_names = {index["name"] for index in sa.inspect(bind).get_indexes("sales_shipments")}
        if "ix_sales_shipments_tracking_due" not in index_names:
            op.create_index(
                "ix_sales_shipments_tracking_due", "sales_shipments", ["tracking_terminal", "tracking_next_check_at"],
            )

    tables = set(sa.inspect(bind).get_table_names())
    if "shipment_tracking_events" not in tables:
        op.create_table(
            "shipment_tracking_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "shipment_id", sa.Integer(),
                sa.ForeignKey("sales_shipments.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("area_code", sa.String(30)),
            sa.Column("area_name", sa.String(100)),
            sa.Column("status", sa.String(50)),
            sa.Column("event_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        op.create_index(
            "uq_shipment_tracking_events_dedupe", "shipment_tracking_events", ["shipment_id", "event_hash"], unique=True,
        )
        op.create_index(
            "ix_shipment_tracking_events_shipment_time", "shipment_tracking_events", ["shipment_id", "event_time"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "shipment_tracking_events" in tables:
        op.drop_table("shipment_tracking_events")
    if "sales_shipments" in tables:
        columns = _columns(bind, "sales_shipments")
        with op.batch_alter_table("sales_shipments") as batch:
            index_names = {index["name"] for index in sa.inspect(bind).get_indexes("sales_shipments")}
            if "ix_sales_shipments_tracking_due" in index_names:
                batch.drop_index("ix_sales_shipments_tracking_due")
            for name in (
                "tracking_error", "tracking_next_check_at", "tracking_last_event_at",
                "tracking_last_checked_at", "tracking_terminal", "tracking_status",
            ):
                if name in columns:
                    batch.drop_column(name)
