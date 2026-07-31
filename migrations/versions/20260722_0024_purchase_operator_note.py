"""purchase operator and note

Revision ID: 20260722_0024
Revises: 20260721_0023
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0024"
down_revision = "20260721_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("purchase_batches")}
    with op.batch_alter_table("purchase_batches") as batch:
        if "operator_name" not in columns:
            batch.add_column(sa.Column("operator_name", sa.String(128)))
        if "note" not in columns:
            batch.add_column(sa.Column("note", sa.Text()))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("purchase_batches")}
    if "ix_purchase_batches_operator_name" not in indexes:
        op.create_index("ix_purchase_batches_operator_name", "purchase_batches", ["operator_name"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("purchase_batches")}
    if "ix_purchase_batches_operator_name" in indexes:
        op.drop_index("ix_purchase_batches_operator_name", table_name="purchase_batches")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("purchase_batches")}
    with op.batch_alter_table("purchase_batches") as batch:
        if "note" in columns:
            batch.drop_column("note")
        if "operator_name" in columns:
            batch.drop_column("operator_name")
