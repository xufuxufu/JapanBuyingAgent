"""product watch recommendations and bulk enable MVP"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0015"
down_revision = "20260716_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "product_watch_configs" not in tables:
        op.create_table(
            "product_watch_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("user_target_price", sa.Integer()),
            sa.Column("recommended_target_price", sa.Integer()),
            sa.Column("effective_target_price", sa.Integer()),
            sa.Column("recommended_price_source", sa.String(40)),
            sa.Column("recommended_calculated_at", sa.DateTime(timezone=True)),
            sa.Column("monitor_restock", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("frequency_tier", sa.String(20), server_default="normal", nullable=False),
            sa.Column("source", sa.String(40), server_default="manual", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("last_target_reached_at", sa.DateTime(timezone=True)),
            sa.Column("pause_reason", sa.String(100)),
            sa.CheckConstraint("user_target_price IS NULL OR user_target_price > 0", name="ck_product_watch_user_price"),
            sa.CheckConstraint("recommended_target_price IS NULL OR recommended_target_price > 0", name="ck_product_watch_recommended_price"),
            sa.CheckConstraint("effective_target_price IS NULL OR effective_target_price > 0", name="ck_product_watch_effective_price"),
            sa.CheckConstraint("frequency_tier IN ('low','normal','high','urgent')", name="ck_product_watch_frequency"),
            sa.CheckConstraint(
                "source IN ('manual','purchase_recommendation','scan_recommendation','enrichment_recommendation')",
                name="ck_product_watch_source",
            ),
        )
        op.create_index("uq_product_watch_configs_product", "product_watch_configs", ["product_id"], unique=True)
        op.create_index("ix_product_watch_configs_enabled", "product_watch_configs", ["enabled"])

    tables = set(sa.inspect(bind).get_table_names())
    if "product_watch_recommendations" not in tables:
        op.create_table(
            "product_watch_recommendations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("reason", sa.String(50), nullable=False),
            sa.Column("recommended_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("ignored", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("accepted", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.CheckConstraint(
                "reason IN ('history_purchase_count','cumulative_purchase_quantity','scan_count','restock_purchase','purchase_enrichment')",
                name="ck_product_watch_recommendation_reason",
            ),
            sa.CheckConstraint("NOT (ignored = 1 AND accepted = 1)", name="ck_product_watch_recommendation_state"),
        )
        op.create_index("ix_product_watch_recommendations_product", "product_watch_recommendations", ["product_id"])
        op.create_index(
            "ix_product_watch_recommendations_pending",
            "product_watch_recommendations",
            ["accepted", "ignored", "recommended_at"],
        )
        op.create_index(
            "uq_product_watch_recommendations_pending_reason",
            "product_watch_recommendations",
            ["product_id", "reason"],
            unique=True,
            sqlite_where=sa.text("accepted = 0 AND ignored = 0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "product_watch_recommendations" in tables:
        op.drop_table("product_watch_recommendations")
    if "product_watch_configs" in tables:
        op.drop_table("product_watch_configs")
