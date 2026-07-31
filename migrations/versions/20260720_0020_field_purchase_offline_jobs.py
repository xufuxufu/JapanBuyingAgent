"""field purchase drafts, tag evidence, durable jobs, and provider status"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_0020"
down_revision = "20260720_0019"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "field_purchase_batches" not in tables:
        op.create_table(
            "field_purchase_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_no", sa.String(50), nullable=False),
            sa.Column("client_request_id", sa.String(100), nullable=False),
            sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("operator_name", sa.String(128), nullable=False),
            sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "status IN ('ACTIVE','COMPLETED','CANCELLED')",
                name="ck_field_purchase_batches_status",
            ),
        )
        op.create_index("uq_field_purchase_batches_batch_no", "field_purchase_batches", ["batch_no"], unique=True)
        op.create_index(
            "ix_field_purchase_batches_client_request_id",
            "field_purchase_batches",
            ["client_request_id"],
            unique=True,
        )
        op.create_index("ix_field_purchase_batches_store_id", "field_purchase_batches", ["store_id"])
        op.create_index("ix_field_purchase_batches_status", "field_purchase_batches", ["status"])

    tables = _tables(bind)
    if "field_purchase_items" not in tables:
        op.create_table(
            "field_purchase_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "batch_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT")),
            sa.Column(
                "enrichment_task_id",
                sa.Integer(),
                sa.ForeignKey("product_enrichment_tasks.id", ondelete="SET NULL"),
            ),
            sa.Column("jan", sa.String(32)),
            sa.Column("temporary_id", sa.String(80)),
            sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
            sa.Column("status", sa.String(30), server_default="LOCAL_DRAFT", nullable=False),
            sa.Column("name_cn", sa.String(128)),
            sa.Column("name_ja", sa.String(128)),
            sa.Column("brand", sa.String(128)),
            sa.Column("category", sa.String(128)),
            sa.Column("unit_name", sa.String(128)),
            sa.Column("unit_price", sa.Integer()),
            sa.Column("product_image_path", sa.Text()),
            sa.Column("captured_by", sa.String(128), nullable=False),
            sa.Column("first_scanned_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("last_scanned_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint("quantity > 0", name="ck_field_purchase_items_quantity_positive"),
            sa.CheckConstraint(
                "status IN ('LOCAL_DRAFT','UPLOAD_PENDING','ENRICHMENT_PENDING','ENRICHING',"
                "'NEEDS_REVIEW','READY','FAILED_RETRYABLE','FAILED_MANUAL','CONFIRMED')",
                name="ck_field_purchase_items_status",
            ),
            sa.CheckConstraint(
                "jan IS NOT NULL OR temporary_id IS NOT NULL",
                name="ck_field_purchase_items_identity",
            ),
        )
        op.create_index("ix_field_purchase_items_batch_id", "field_purchase_items", ["batch_id"])
        op.create_index("ix_field_purchase_items_product_id", "field_purchase_items", ["product_id"])
        op.create_index("ix_field_purchase_items_enrichment_task_id", "field_purchase_items", ["enrichment_task_id"])
        op.create_index("ix_field_purchase_items_jan", "field_purchase_items", ["jan"])
        op.create_index("ix_field_purchase_items_status", "field_purchase_items", ["status"])
        op.create_index("ix_field_purchase_items_temporary_id", "field_purchase_items", ["temporary_id"], unique=True)
        op.create_index(
            "uq_field_purchase_items_batch_product",
            "field_purchase_items",
            ["batch_id", "product_id"],
            unique=True,
            sqlite_where=sa.text("product_id IS NOT NULL"),
        )
        op.create_index(
            "uq_field_purchase_items_batch_unmatched_jan",
            "field_purchase_items",
            ["batch_id", "jan"],
            unique=True,
            sqlite_where=sa.text("product_id IS NULL AND jan IS NOT NULL"),
        )

    tables = _tables(bind)
    if "tag_evidence" not in tables:
        op.create_table(
            "tag_evidence",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "field_purchase_item_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("original_filename", sa.String(255), nullable=False),
            sa.Column("content_type", sa.String(100), nullable=False),
            sa.Column("file_path", sa.Text(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("ocr_status", sa.String(30), server_default="PENDING", nullable=False),
            sa.Column("ocr_text", sa.Text()),
            sa.Column("ocr_confidence", sa.Float()),
            sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "ocr_status IN ('PENDING','UNCONFIGURED','PROCESSING','COMPLETED','FAILED_RETRYABLE','FAILED_MANUAL')",
                name="ck_tag_evidence_ocr_status",
            ),
            sa.UniqueConstraint(
                "field_purchase_item_id", "sha256", name="uq_tag_evidence_item_hash",
            ),
        )
        op.create_index("ix_tag_evidence_field_purchase_item_id", "tag_evidence", ["field_purchase_item_id"])
        op.create_index("ix_tag_evidence_ocr_status", "tag_evidence", ["ocr_status"])

    tables = _tables(bind)
    if "product_serials" not in tables:
        op.create_table(
            "product_serials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("serial_value", sa.String(255), nullable=False),
            sa.Column("source", sa.String(50), server_default="manual", nullable=False),
            sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint("status IN ('ACTIVE','USED','VOID')", name="ck_product_serials_status"),
        )
        op.create_index("ix_product_serials_product_id", "product_serials", ["product_id"])
        op.create_index("uq_product_serials_serial_value", "product_serials", ["serial_value"], unique=True)

    tables = _tables(bind)
    if "field_purchase_sync_requests" not in tables:
        op.create_table(
            "field_purchase_sync_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("client_request_id", sa.String(100), nullable=False),
            sa.Column("request_type", sa.String(40), nullable=False),
            sa.Column(
                "batch_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "item_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_items.id", ondelete="SET NULL"),
            ),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        op.create_index(
            "ix_field_purchase_sync_requests_client_request_id",
            "field_purchase_sync_requests",
            ["client_request_id"],
            unique=True,
        )
        op.create_index("ix_field_purchase_sync_requests_batch_id", "field_purchase_sync_requests", ["batch_id"])
        op.create_index("ix_field_purchase_sync_requests_item_id", "field_purchase_sync_requests", ["item_id"])

    tables = _tables(bind)
    if "durable_background_jobs" not in tables:
        op.create_table(
            "durable_background_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("dedupe_key", sa.String(160), nullable=False),
            sa.Column("job_type", sa.String(60), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(30), server_default="PENDING", nullable=False),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
            sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("locked_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint(
                "status IN ('PENDING','RUNNING','COMPLETED','FAILED_RETRYABLE','FAILED_MANUAL')",
                name="ck_durable_background_jobs_status",
            ),
        )
        op.create_index(
            "ix_durable_background_jobs_dedupe_key",
            "durable_background_jobs",
            ["dedupe_key"],
            unique=True,
        )
        op.create_index("ix_durable_background_jobs_job_type", "durable_background_jobs", ["job_type"])
        op.create_index("ix_durable_background_jobs_status", "durable_background_jobs", ["status"])
        op.create_index("ix_durable_background_jobs_available_at", "durable_background_jobs", ["available_at"])
        op.create_index("ix_durable_background_jobs_locked_at", "durable_background_jobs", ["locked_at"])

    tables = _tables(bind)
    if "platform_provider_states" not in tables:
        op.create_table(
            "platform_provider_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_code", sa.String(50), nullable=False),
            sa.Column("configured", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("credentials_valid", sa.Boolean()),
            sa.Column("last_success_at", sa.DateTime(timezone=True)),
            sa.Column("last_tested_at", sa.DateTime(timezone=True)),
            sa.Column("recent_error", sa.Text()),
            sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        op.create_index(
            "ix_platform_provider_states_provider_code",
            "platform_provider_states",
            ["provider_code"],
            unique=True,
        )

    tables = _tables(bind)
    if "platform_lookup_results" not in tables:
        op.create_table(
            "platform_lookup_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "price_search_run_id",
                sa.Integer(),
                sa.ForeignKey("price_search_runs.id", ondelete="CASCADE"),
            ),
            sa.Column(
                "field_purchase_item_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_items.id", ondelete="SET NULL"),
            ),
            sa.Column("platform", sa.String(50), nullable=False),
            sa.Column("jan", sa.String(32)),
            sa.Column("title", sa.Text()),
            sa.Column("brand", sa.String(128)),
            sa.Column("price", sa.Integer()),
            sa.Column("shipping_fee", sa.Integer()),
            sa.Column("total_price", sa.Integer()),
            sa.Column("currency", sa.String(3), server_default="JPY", nullable=False),
            sa.Column("availability", sa.String(30)),
            sa.Column("seller", sa.String(255)),
            sa.Column("product_url", sa.Text()),
            sa.Column("image_url", sa.Text()),
            sa.Column("link_type", sa.String(20), server_default="product", nullable=False),
            sa.Column("jan_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("match_type", sa.String(30), server_default="UNVERIFIED", nullable=False),
            sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error_code", sa.String(60)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "link_type IN ('product','search')",
                name="ck_platform_lookup_results_link_type",
            ),
        )
        op.create_index(
            "ix_platform_lookup_results_price_search_run_id",
            "platform_lookup_results",
            ["price_search_run_id"],
        )
        op.create_index(
            "ix_platform_lookup_results_field_purchase_item_id",
            "platform_lookup_results",
            ["field_purchase_item_id"],
        )
        op.create_index("ix_platform_lookup_results_platform", "platform_lookup_results", ["platform"])
        op.create_index("ix_platform_lookup_results_jan", "platform_lookup_results", ["jan"])

    tables = _tables(bind)
    if "enrichment_audit_logs" not in tables:
        op.create_table(
            "enrichment_audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "field_purchase_item_id",
                sa.Integer(),
                sa.ForeignKey("field_purchase_items.id", ondelete="SET NULL"),
            ),
            sa.Column(
                "enrichment_task_id",
                sa.Integer(),
                sa.ForeignKey("product_enrichment_tasks.id", ondelete="SET NULL"),
            ),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("actor", sa.String(128), nullable=False),
            sa.Column("before_json", sa.Text()),
            sa.Column("after_json", sa.Text()),
            sa.Column("source", sa.String(50), server_default="web", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.CheckConstraint(
                "action IN ('SELECT_CANDIDATE','REJECT_CANDIDATE','MANUAL_EDIT','BULK_EDIT',"
                "'RETRY','CONFIRM','BIND_EXISTING')",
                name="ck_enrichment_audit_logs_action",
            ),
        )
        op.create_index(
            "ix_enrichment_audit_logs_field_purchase_item_id",
            "enrichment_audit_logs",
            ["field_purchase_item_id"],
        )
        op.create_index(
            "ix_enrichment_audit_logs_enrichment_task_id",
            "enrichment_audit_logs",
            ["enrichment_task_id"],
        )
        op.create_index("ix_enrichment_audit_logs_action", "enrichment_audit_logs", ["action"])


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "enrichment_audit_logs",
        "platform_lookup_results",
        "platform_provider_states",
        "durable_background_jobs",
        "field_purchase_sync_requests",
        "product_serials",
        "tag_evidence",
        "field_purchase_items",
        "field_purchase_batches",
    ):
        if table in _tables(bind):
            op.drop_table(table)
