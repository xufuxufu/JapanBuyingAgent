"""product status and qinsi confirmation audit

Revision ID: 20260730_0025
Revises: 20260722_0024
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_0025"
down_revision = "20260722_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("qinsi_export_jobs"):
        product_export_columns = {column["name"] for column in inspector.get_columns("qinsi_export_jobs")}
        with op.batch_alter_table("qinsi_export_jobs") as batch:
            if "file_content" not in product_export_columns:
                batch.add_column(sa.Column("file_content", sa.LargeBinary()))
            if "confirmed_by" not in product_export_columns:
                batch.add_column(sa.Column("confirmed_by", sa.String(128)))
            if "cancelled_at" not in product_export_columns:
                batch.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True)))
            if "cancelled_by" not in product_export_columns:
                batch.add_column(sa.Column("cancelled_by", sa.String(128)))
    if not inspector.has_table("qinsi_purchase_export_jobs"):
        return
    export_columns = {column["name"] for column in inspector.get_columns("qinsi_purchase_export_jobs")}
    with op.batch_alter_table("qinsi_purchase_export_jobs") as batch:
        if "confirmed_by" not in export_columns:
            batch.add_column(sa.Column("confirmed_by", sa.String(128)))
        if "confirmation_note" not in export_columns:
            batch.add_column(sa.Column("confirmation_note", sa.Text()))
        if "cancelled_at" not in export_columns:
            batch.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True)))
        if "cancelled_by" not in export_columns:
            batch.add_column(sa.Column("cancelled_by", sa.String(128)))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("qinsi_purchase_export_jobs"):
        export_columns = set()
    else:
        export_columns = {column["name"] for column in inspector.get_columns("qinsi_purchase_export_jobs")}
        with op.batch_alter_table("qinsi_purchase_export_jobs") as batch:
            if "cancelled_by" in export_columns:
                batch.drop_column("cancelled_by")
            if "cancelled_at" in export_columns:
                batch.drop_column("cancelled_at")
            if "confirmation_note" in export_columns:
                batch.drop_column("confirmation_note")
            if "confirmed_by" in export_columns:
                batch.drop_column("confirmed_by")
    if inspector.has_table("qinsi_export_jobs"):
        product_export_columns = {column["name"] for column in inspector.get_columns("qinsi_export_jobs")}
        with op.batch_alter_table("qinsi_export_jobs") as batch:
            if "cancelled_by" in product_export_columns:
                batch.drop_column("cancelled_by")
            if "cancelled_at" in product_export_columns:
                batch.drop_column("cancelled_at")
            if "confirmed_by" in product_export_columns:
                batch.drop_column("confirmed_by")
            if "file_content" in product_export_columns:
                batch.drop_column("file_content")
