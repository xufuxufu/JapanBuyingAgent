"""receipt processing and review fields"""
from alembic import op
import sqlalchemy as sa

revision = "20260714_0002"
down_revision = "20260714_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    image_columns = {column["name"] for column in inspector.get_columns("receipt_images")}
    additions = [
        sa.Column("processing_method", sa.String(length=100), nullable=True),
        sa.Column("processing_warning", sa.Text(), nullable=True),
        sa.Column("processed_width", sa.Integer(), nullable=True),
        sa.Column("processed_height", sa.Integer(), nullable=True),
        sa.Column("recognition_source", sa.String(length=20), server_default="processed", nullable=False),
        sa.Column("rotation_degrees", sa.Integer(), server_default="0", nullable=False),
    ]
    missing = [column for column in additions if column.name not in image_columns]
    if missing:
        with op.batch_alter_table("receipt_images") as batch:
            for column in missing:
                batch.add_column(column)
    receipt_columns = {column["name"] for column in inspector.get_columns("receipts")}
    if "confirmation_warning" not in receipt_columns:
        with op.batch_alter_table("receipts") as batch:
            batch.add_column(sa.Column("confirmation_warning", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch:
        batch.drop_column("confirmation_warning")
    with op.batch_alter_table("receipt_images") as batch:
        batch.drop_column("rotation_degrees")
        batch.drop_column("recognition_source")
        batch.drop_column("processed_height")
        batch.drop_column("processed_width")
        batch.drop_column("processing_warning")
        batch.drop_column("processing_method")
