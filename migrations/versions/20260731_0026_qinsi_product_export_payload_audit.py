"""ensure qinsi product export payload and confirmation audit columns

Revision ID: 20260731_0026
Revises: 20260730_0025
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0026"
down_revision = "20260730_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("qinsi_export_jobs"):
        return
    columns = {column["name"] for column in inspector.get_columns("qinsi_export_jobs")}
    with op.batch_alter_table("qinsi_export_jobs") as batch:
        if "file_content" not in columns:
            batch.add_column(sa.Column("file_content", sa.LargeBinary()))
        if "confirmed_by" not in columns:
            batch.add_column(sa.Column("confirmed_by", sa.String(128)))
        if "cancelled_at" not in columns:
            batch.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True)))
        if "cancelled_by" not in columns:
            batch.add_column(sa.Column("cancelled_by", sa.String(128)))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("qinsi_export_jobs"):
        return
    columns = {column["name"] for column in inspector.get_columns("qinsi_export_jobs")}
    with op.batch_alter_table("qinsi_export_jobs") as batch:
        if "cancelled_by" in columns:
            batch.drop_column("cancelled_by")
        if "cancelled_at" in columns:
            batch.drop_column("cancelled_at")
        if "confirmed_by" in columns:
            batch.drop_column("confirmed_by")
        if "file_content" in columns:
            batch.drop_column("file_content")
