"""workflow states, duplicate tracking and recognition ZIP audit"""
from alembic import op
import sqlalchemy as sa

revision = "20260715_0005"
down_revision = "20260715_0004"
branch_labels = None
depends_on = None


def _add_missing_columns(table: str, columns: list[sa.Column]) -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
    missing = [column for column in columns if column.name not in existing]
    if missing:
        with op.batch_alter_table(table) as batch:
            for column in missing:
                batch.add_column(column)


def _create_index(table: str, name: str, columns: list[str], unique: bool = False) -> None:
    names = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in names:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing_columns("receipt_batches", [
        sa.Column("image_status", sa.String(20), server_default="uploaded", nullable=False),
        sa.Column("gpt_status", sa.String(30), server_default="not_packaged", nullable=False),
        sa.Column("product_status", sa.String(30), server_default="not_matched", nullable=False),
        sa.Column("qinsi_status", sa.String(30), server_default="not_exported", nullable=False),
        sa.Column("zip_first_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zip_last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zip_download_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gpt_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("json_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    ])
    _add_missing_columns("receipt_images", [
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("perceptual_hash", sa.String(100), nullable=True),
        sa.Column("normalized_image_hash", sa.String(64), nullable=True),
        sa.Column("duplicate_of_image_id", sa.Integer(), nullable=True),
        sa.Column("duplicate_score", sa.Float(), nullable=True),
        sa.Column("duplicate_status", sa.String(30), server_default="none", nullable=False),
    ])
    _add_missing_columns("receipts", [
        sa.Column("business_fingerprint", sa.String(64), nullable=True),
        sa.Column("duplicate_of_receipt_id", sa.Integer(), nullable=True),
        sa.Column("duplicate_score", sa.Float(), nullable=True),
        sa.Column("duplicate_status", sa.String(30), server_default="none", nullable=False),
        sa.Column("duplicate_reason", sa.Text(), nullable=True),
    ])
    image_fk_columns = {tuple(item.get("constrained_columns") or ()) for item in sa.inspect(bind).get_foreign_keys("receipt_images")}
    if ("duplicate_of_image_id",) not in image_fk_columns:
        with op.batch_alter_table("receipt_images") as batch:
            batch.create_foreign_key("fk_receipt_images_duplicate_of", "receipt_images", ["duplicate_of_image_id"], ["id"], ondelete="SET NULL")
    receipt_fk_columns = {tuple(item.get("constrained_columns") or ()) for item in sa.inspect(bind).get_foreign_keys("receipts")}
    if ("duplicate_of_receipt_id",) not in receipt_fk_columns:
        with op.batch_alter_table("receipts") as batch:
            batch.create_foreign_key("fk_receipts_duplicate_of", "receipts", ["duplicate_of_receipt_id"], ["id"], ondelete="SET NULL")

    bind.execute(sa.text("UPDATE receipt_images SET sha256=file_hash WHERE sha256 IS NULL"))
    bind.execute(sa.text("UPDATE receipt_batches SET image_status=CASE WHEN status='processing' THEN 'processing' WHEN status='failed' THEN 'failed' ELSE 'ready' END WHERE image_status='uploaded'"))
    bind.execute(sa.text("UPDATE receipt_batches SET gpt_status=CASE WHEN status='confirmed' THEN 'reviewed' WHEN status='review' THEN 'json_imported' ELSE gpt_status END"))

    _create_index("receipt_images", "ix_receipt_images_sha256", ["sha256"])
    _create_index("receipt_images", "ix_receipt_images_perceptual_hash", ["perceptual_hash"])
    _create_index("receipt_images", "ix_receipt_images_normalized_image_hash", ["normalized_image_hash"])
    _create_index("receipt_images", "ix_receipt_images_duplicate_of_image_id", ["duplicate_of_image_id"])
    _create_index("receipts", "ix_receipts_business_fingerprint", ["business_fingerprint"])
    _create_index("receipts", "ix_receipts_duplicate_of_receipt_id", ["duplicate_of_receipt_id"])
    _create_index("receipts", "ix_receipts_duplicate_status", ["duplicate_status"])

    tables = set(sa.inspect(bind).get_table_names())
    if "duplicate_detection_logs" not in tables:
        op.create_table(
            "duplicate_detection_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("entity_type", sa.String(20), nullable=False),
            sa.Column("new_entity_id", sa.Integer(), nullable=True),
            sa.Column("matched_entity_id", sa.Integer(), nullable=True),
            sa.Column("algorithm_version", sa.String(30), nullable=False),
            sa.Column("sha_match", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("perceptual_distance", sa.Integer(), nullable=True),
            sa.Column("business_score", sa.Float(), nullable=True),
            sa.Column("decision", sa.String(30), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        for name, column in (("entity_type", "entity_type"), ("new_entity_id", "new_entity_id"), ("matched_entity_id", "matched_entity_id"), ("decision", "decision")):
            op.create_index(f"ix_duplicate_detection_logs_{name}", "duplicate_detection_logs", [column])

    tables = set(sa.inspect(bind).get_table_names())
    if "zip_package_jobs" not in tables:
        op.create_table(
            "zip_package_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_no", sa.String(50), nullable=False, unique=True),
            sa.Column("selection_key", sa.String(64), nullable=False, unique=True),
            sa.Column("batch_count", sa.Integer(), nullable=False),
            sa.Column("image_count", sa.Integer(), nullable=False),
            sa.Column("excluded_duplicate_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("first_downloaded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_downloaded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("download_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    tables = set(sa.inspect(bind).get_table_names())
    if "zip_package_items" not in tables:
        op.create_table(
            "zip_package_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("zip_package_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("receipt_batches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("image_id", sa.Integer(), sa.ForeignKey("receipt_images.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("recognition_filename", sa.String(100), nullable=False),
            sa.Column("excluded", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("exclusion_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "image_id", name="uq_zip_package_item_job_image"),
        )
        op.create_index("ix_zip_package_items_job_id", "zip_package_items", ["job_id"])
        op.create_index("ix_zip_package_items_batch_id", "zip_package_items", ["batch_id"])
        op.create_index("ix_zip_package_items_image_id", "zip_package_items", ["image_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "zip_package_items" in tables:
        op.drop_table("zip_package_items")
    if "zip_package_jobs" in tables:
        op.drop_table("zip_package_jobs")
    if "duplicate_detection_logs" in tables:
        op.drop_table("duplicate_detection_logs")
    for table, names in (
        ("receipts", ("duplicate_reason", "duplicate_status", "duplicate_score", "duplicate_of_receipt_id", "business_fingerprint")),
        ("receipt_images", ("duplicate_status", "duplicate_score", "duplicate_of_image_id", "normalized_image_hash", "perceptual_hash", "sha256")),
        ("receipt_batches", ("reviewed_at", "json_imported_at", "gpt_sent_at", "zip_download_count", "zip_last_downloaded_at", "zip_first_downloaded_at", "qinsi_status", "product_status", "gpt_status", "image_status")),
    ):
        existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name in names:
                if name in existing:
                    batch.drop_column(name)
