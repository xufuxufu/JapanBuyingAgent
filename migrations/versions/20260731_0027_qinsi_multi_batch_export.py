"""record all purchase batch ids for merged qinsi purchase exports

Revision ID: 20260731_0027
Revises: 20260731_0026
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_0027"
down_revision = "20260731_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("qinsi_purchase_export_jobs"):
        return
    columns = {column["name"] for column in inspector.get_columns("qinsi_purchase_export_jobs")}
    if "selected_batch_ids_json" not in columns:
        with op.batch_alter_table("qinsi_purchase_export_jobs") as batch:
            batch.add_column(sa.Column("selected_batch_ids_json", sa.Text()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("qinsi_purchase_export_jobs"):
        return
    columns = {column["name"] for column in inspector.get_columns("qinsi_purchase_export_jobs")}
    if "selected_batch_ids_json" in columns:
        with op.batch_alter_table("qinsi_purchase_export_jobs") as batch:
            batch.drop_column("selected_batch_ids_json")
