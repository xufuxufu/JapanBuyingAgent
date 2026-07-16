"""receipt source tracking and reserved QinSi/price snapshot schema"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import Base
from app import models  # noqa: F401

revision = "20260715_0003"
down_revision = "20260714_0002"
branch_labels = None
depends_on = None

RESERVED_TABLES = (
    "qinsi_export_jobs", "qinsi_export_lines", "qinsi_export_line_sources",
    "marketplaces", "price_search_runs", "product_offers", "price_watch_rules", "price_alerts",
)


def _legacy_recognition_name(batch_no: str, created_at, batch_id: int, page_no: int) -> str:
    if re.fullmatch(r"RCPT-\d{8}-\d{4}-[A-Z0-9]{4}", batch_no or ""):
        stem = batch_no
    else:
        try:
            instant = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            instant = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        local = instant.astimezone(timezone(timedelta(hours=9), "Asia/Tokyo"))
        stem = f"RCPT-{local:%Y%m%d-%H%M}-{batch_id % 10000:04d}"
    return f"{stem}_P{page_no:02d}.jpg"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "receipt_images" in tables:
        image_columns = {column["name"] for column in inspector.get_columns("receipt_images")}
        added_recognition_filename = "recognition_filename" not in image_columns
        if added_recognition_filename:
            with op.batch_alter_table("receipt_images") as batch:
                batch.add_column(sa.Column("recognition_filename", sa.String(length=100), nullable=True))
            rows = bind.execute(sa.text("""
                SELECT i.id, i.page_no, b.id AS batch_id, b.batch_no, b.created_at
                FROM receipt_images i JOIN receipt_batches b ON b.id = i.batch_id
                ORDER BY i.id
            """)).mappings()
            for row in rows:
                name = _legacy_recognition_name(row["batch_no"], row["created_at"], row["batch_id"], row["page_no"])
                bind.execute(sa.text("UPDATE receipt_images SET recognition_filename=:name WHERE id=:id"), {"name": name, "id": row["id"]})
            with op.batch_alter_table("receipt_images") as batch:
                batch.alter_column("recognition_filename", existing_type=sa.String(length=100), nullable=False)
        inspector = sa.inspect(bind)
        unique_columns = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints("receipt_images")}
        unique_columns |= {tuple(item.get("column_names") or ()) for item in inspector.get_indexes("receipt_images") if item.get("unique")}
        if ("recognition_filename",) not in unique_columns:
            op.create_index("uq_receipt_images_recognition_filename", "receipt_images", ["recognition_filename"], unique=True)
        if "recognition_source" in {column["name"] for column in sa.inspect(bind).get_columns("receipt_images")}:
            bind.execute(sa.text("""
                UPDATE receipt_images SET recognition_source='original'
                WHERE processing_method IS NULL OR processing_method NOT LIKE '%auto_crop%'
            """))

    if "receipts" in tables:
        receipt_columns = {column["name"] for column in sa.inspect(bind).get_columns("receipts")}
        if "source_image_id" not in receipt_columns:
            with op.batch_alter_table("receipts") as batch:
                batch.add_column(sa.Column("source_image_id", sa.Integer(), nullable=True))
                batch.create_foreign_key("fk_receipts_source_image_id", "receipt_images", ["source_image_id"], ["id"], ondelete="SET NULL")
                batch.create_index("ix_receipts_source_image_id", ["source_image_id"])

    tables = set(sa.inspect(bind).get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name in RESERVED_TABLES and table.name not in tables:
            op.execute(CreateTable(table))
            for index in sorted(table.indexes, key=lambda item: item.name or ""):
                op.execute(CreateIndex(index))
            tables.add(table.name)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(RESERVED_TABLES):
        if name in tables:
            op.drop_table(name)
    if "receipts" in tables and "source_image_id" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipts")}:
        with op.batch_alter_table("receipts") as batch:
            batch.drop_column("source_image_id")
    if "receipt_images" in tables and "recognition_filename" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipt_images")}:
        with op.batch_alter_table("receipt_images") as batch:
            batch.drop_column("recognition_filename")
