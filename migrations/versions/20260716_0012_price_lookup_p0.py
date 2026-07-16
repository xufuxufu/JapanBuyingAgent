"""mobile JAN price lookup P0"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0012"
down_revision = "20260716_0011"
branch_labels = None
depends_on = None


PRODUCT_COLUMNS = (
    sa.Column("display_name", sa.String(257)),
    sa.Column("main_image_path", sa.Text()),
    sa.Column("main_image_source_url", sa.Text()),
    sa.Column("product_data_confirmed", sa.Boolean(), server_default=sa.false(), nullable=False),
)


def _column_map(bind, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "products" in tables:
        product_columns = _column_map(bind, "products")
        missing_product_columns = [column for column in PRODUCT_COLUMNS if column.name not in product_columns]
        if missing_product_columns:
            with op.batch_alter_table("products") as batch:
                for column in missing_product_columns:
                    batch.add_column(column)

    if "price_search_runs" in tables:
        run_columns = _column_map(bind, "price_search_runs")
        run_needs_change = not run_columns["product_id"]["nullable"] or any(
            name not in run_columns for name in ("is_new_candidate", "cache_expires_at", "provider_summary_json")
        )
        if run_needs_change:
            with op.batch_alter_table("price_search_runs") as batch:
                if not run_columns["product_id"]["nullable"]:
                    batch.alter_column("product_id", existing_type=sa.Integer(), nullable=True)
                if "is_new_candidate" not in run_columns:
                    batch.add_column(sa.Column("is_new_candidate", sa.Boolean(), server_default=sa.false(), nullable=False))
                if "cache_expires_at" not in run_columns:
                    batch.add_column(sa.Column("cache_expires_at", sa.DateTime(timezone=True)))
                if "provider_summary_json" not in run_columns:
                    batch.add_column(sa.Column("provider_summary_json", sa.Text()))
            indexes = {index["name"] for index in sa.inspect(bind).get_indexes("price_search_runs")}
            if "ix_price_search_runs_cache_expires_at" not in indexes:
                op.create_index("ix_price_search_runs_cache_expires_at", "price_search_runs", ["cache_expires_at"])

    offer_additions = (
        sa.Column("image_url", sa.Text()),
        sa.Column("shipping_known", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("jan_match_status", sa.String(30), server_default="unverified", nullable=False),
        sa.Column("spec_match_status", sa.String(30), server_default="unknown", nullable=False),
        sa.Column("is_subscription", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_trusted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("exclusion_reason", sa.Text()),
    )
    if "product_offers" in tables:
        offer_columns = _column_map(bind, "product_offers")
        offer_needs_change = not offer_columns["product_id"]["nullable"] or any(column.name not in offer_columns for column in offer_additions)
        if offer_needs_change:
            with op.batch_alter_table("product_offers") as batch:
                if not offer_columns["product_id"]["nullable"]:
                    batch.alter_column("product_id", existing_type=sa.Integer(), nullable=True)
                for column in offer_additions:
                    if column.name not in offer_columns:
                        batch.add_column(column)
            indexes = {index["name"] for index in sa.inspect(bind).get_indexes("product_offers")}
            if "ix_product_offers_is_trusted" not in indexes:
                op.create_index("ix_product_offers_is_trusted", "product_offers", ["is_trusted"])

    tables = set(sa.inspect(bind).get_table_names())
    price_prerequisites = {"price_search_runs", "marketplaces"} <= tables
    if price_prerequisites and "price_provider_attempts" not in tables:
        op.create_table(
            "price_provider_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("search_run_id", sa.Integer(), sa.ForeignKey("price_search_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("marketplace_id", sa.Integer(), sa.ForeignKey("marketplaces.id", ondelete="SET NULL")),
            sa.Column("provider_code", sa.String(50), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("message", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('success','empty','timeout','error','unconfigured','manual_only')",
                name="ck_price_provider_attempts_status",
            ),
        )
        op.create_index("ix_price_provider_attempts_search_run_id", "price_provider_attempts", ["search_run_id"])
        op.create_index("ix_price_provider_attempts_marketplace_id", "price_provider_attempts", ["marketplace_id"])
        op.create_index("ix_price_provider_attempts_status", "price_provider_attempts", ["status"])
    if price_prerequisites and "price_lookup_histories" not in tables:
        op.create_table(
            "price_lookup_histories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("search_run_id", sa.Integer(), sa.ForeignKey("price_search_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("jan", sa.String(32), nullable=False),
            sa.Column("current_store_price", sa.Integer()),
            sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint("current_store_price IS NULL OR current_store_price >= 0", name="ck_price_lookup_histories_store_price"),
        )
        for column in ("search_run_id", "product_id", "jan", "created_at"):
            op.create_index(f"ix_price_lookup_histories_{column}", "price_lookup_histories", [column])

    seed = sa.text("""
        INSERT INTO marketplaces (code,name,base_url,active,created_at)
        SELECT :code,:name,:base_url,1,CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM marketplaces WHERE code=:code)
    """)
    if "marketplaces" in tables:
        for code, name, base_url in (
            ("rakuten", "Rakuten", "https://www.rakuten.co.jp/"),
            ("yahoo_shopping", "Yahoo Shopping", "https://shopping.yahoo.co.jp/"),
            ("manual", "Manual/Fallback", None),
        ):
            bind.execute(seed, {"code": code, "name": name, "base_url": base_url})


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "price_lookup_histories" in tables:
        op.drop_table("price_lookup_histories")
    if "price_provider_attempts" in tables:
        op.drop_table("price_provider_attempts")
    offer_columns = _column_map(bind, "product_offers")
    with op.batch_alter_table("product_offers") as batch:
        for name in ("exclusion_reason", "is_trusted", "is_subscription", "spec_match_status", "jan_match_status", "shipping_known", "image_url"):
            if name in offer_columns:
                batch.drop_column(name)
    run_columns = _column_map(bind, "price_search_runs")
    with op.batch_alter_table("price_search_runs") as batch:
        for name in ("provider_summary_json", "cache_expires_at", "is_new_candidate"):
            if name in run_columns:
                batch.drop_column(name)
    product_columns = _column_map(bind, "products")
    with op.batch_alter_table("products") as batch:
        for name in ("product_data_confirmed", "main_image_source_url", "main_image_path", "display_name"):
            if name in product_columns:
                batch.drop_column(name)
