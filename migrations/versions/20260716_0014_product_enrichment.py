"""automatic product enrichment MVP"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0014"
down_revision = "20260716_0013"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "products" in tables:
        columns = _columns(bind, "products")
        additions = (
            sa.Column("name_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("main_image_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("main_image_source_platform", sa.String(50)),
            sa.Column("main_image_downloaded_at", sa.DateTime(timezone=True)),
            sa.Column("main_image_hash", sa.String(64)),
            sa.Column("brand", sa.String(128)),
            sa.Column("manufacturer", sa.String(128)),
            sa.Column("category", sa.String(128)),
            sa.Column("capacity", sa.String(64)),
            sa.Column("color", sa.String(64)),
            sa.Column("model_number", sa.String(128)),
            sa.Column("package_count", sa.String(64)),
        )
        for column in additions:
            if column.name not in columns:
                op.add_column("products", column)

    tables = set(sa.inspect(bind).get_table_names())
    if "product_enrichment_tasks" not in tables:
        op.create_table(
            "product_enrichment_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("jan", sa.String(32), nullable=False),
            sa.Column("status", sa.String(30), server_default="pending", nullable=False),
            sa.Column("trigger_source", sa.String(50), nullable=False),
            sa.Column("provider_codes_json", sa.Text()),
            sa.Column("selected_data_json", sa.Text()),
            sa.Column("deepseek_status", sa.String(30), server_default="pending", nullable=False),
            sa.Column("deepseek_name_key", sa.String(64)),
            sa.Column("image_status", sa.String(30), server_default="pending", nullable=False),
            sa.Column("confidence", sa.Float()),
            sa.Column("warnings_json", sa.Text()),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint(
                "status IN ('pending','running','completed','completed_with_warnings','needs_review','failed')",
                name="ck_product_enrichment_tasks_status",
            ),
        )
        op.create_index("ix_product_enrichment_tasks_jan_status", "product_enrichment_tasks", ["jan", "status"])
        op.create_index("ix_product_enrichment_tasks_trigger_source", "product_enrichment_tasks", ["trigger_source"])
        op.create_index("ix_product_enrichment_tasks_product_id", "product_enrichment_tasks", ["product_id"])
        op.create_index("ix_product_enrichment_tasks_deepseek_name_key", "product_enrichment_tasks", ["deepseek_name_key"])
        op.create_index(
            "uq_product_enrichment_tasks_jan_open_or_success", "product_enrichment_tasks", ["jan"], unique=True,
            sqlite_where=sa.text("status IN ('pending','running','completed','completed_with_warnings','needs_review')"),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "product_enrichment_candidates" not in tables:
        op.create_table(
            "product_enrichment_candidates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("product_enrichment_tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("jan", sa.String(32), nullable=False),
            sa.Column("name_ja", sa.Text()), sa.Column("brand", sa.String(128)),
            sa.Column("manufacturer", sa.String(128)), sa.Column("category", sa.String(128)),
            sa.Column("specification", sa.String(255)), sa.Column("capacity", sa.String(64)),
            sa.Column("color", sa.String(64)), sa.Column("model_number", sa.String(128)),
            sa.Column("package_count", sa.String(64)), sa.Column("image_url", sa.Text()),
            sa.Column("source_url", sa.Text(), nullable=False), sa.Column("platform", sa.String(50), nullable=False),
            sa.Column("item_price", sa.Integer()), sa.Column("shipping_price", sa.Integer()),
            sa.Column("total_price", sa.Integer()), sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("score", sa.Float(), server_default="0", nullable=False),
            sa.Column("warnings_json", sa.Text()), sa.Column("provider_summary_json", sa.Text()),
            sa.Column("selected", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.UniqueConstraint("task_id", "platform", "source_url", name="uq_product_enrichment_candidate_source"),
        )
        op.create_index("ix_product_enrichment_candidates_task_id", "product_enrichment_candidates", ["task_id"])
        op.create_index("ix_product_enrichment_candidates_jan", "product_enrichment_candidates", ["jan"])

    if "product_enrichment_sources" not in tables:
        op.create_table(
            "product_enrichment_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("product_enrichment_tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_type", sa.String(30), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("task_id", "source_type", "source_id", name="uq_product_enrichment_task_source"),
        )
        op.create_index("ix_product_enrichment_sources_task_id", "product_enrichment_sources", ["task_id"])

    if "product_translation_cache" not in tables:
        op.create_table(
            "product_translation_cache",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("jan", sa.String(32), nullable=False),
            sa.Column("name_ja_hash", sa.String(64), nullable=False),
            sa.Column("name_ja", sa.Text(), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("jan", "name_ja_hash", name="uq_product_translation_jan_name"),
        )
        op.create_index("ix_product_translation_cache_jan", "product_translation_cache", ["jan"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("product_translation_cache", "product_enrichment_sources", "product_enrichment_candidates", "product_enrichment_tasks"):
        if table in tables:
            op.drop_table(table)
    if "products" in tables:
        columns = _columns(bind, "products")
        with op.batch_alter_table("products") as batch:
            for name in (
                "package_count", "model_number", "color", "capacity", "category", "manufacturer", "brand",
                "main_image_hash", "main_image_downloaded_at", "main_image_source_platform", "main_image_locked", "name_locked",
            ):
                if name in columns:
                    batch.drop_column(name)
