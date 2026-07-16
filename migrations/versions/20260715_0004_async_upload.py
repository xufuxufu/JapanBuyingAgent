"""asynchronous upload idempotency and processing status"""
from alembic import op
import sqlalchemy as sa

revision = "20260715_0004"
down_revision = "20260715_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("receipt_batches")}
    additions = [
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("current_stage", sa.String(length=30), server_default="complete", nullable=False),
        sa.Column("upload_errors_json", sa.Text(), nullable=True),
    ]
    missing = [column for column in additions if column.name not in columns]
    if missing:
        with op.batch_alter_table("receipt_batches") as batch:
            for column in missing:
                batch.add_column(column)
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("receipt_batches")}
    unique_columns = {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints("receipt_batches")}
    unique_columns |= {tuple(item.get("column_names") or ()) for item in inspector.get_indexes("receipt_batches") if item.get("unique")}
    if ("request_id",) not in unique_columns:
        op.create_index("uq_receipt_batches_request_id", "receipt_batches", ["request_id"], unique=True)
    elif "ix_receipt_batches_request_id" not in indexes and "uq_receipt_batches_request_id" not in indexes:
        op.create_index("ix_receipt_batches_request_id", "receipt_batches", ["request_id"], unique=False)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipt_batches")}
    with op.batch_alter_table("receipt_batches") as batch:
        for name in ("upload_errors_json", "current_stage", "request_id"):
            if name in columns:
                batch.drop_column(name)
