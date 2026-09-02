"""Procurement demands: manual-image support for unidentified restock requests (Phase 8)"""

from alembic import op
import sqlalchemy as sa


revision = "20260901_0046"
down_revision = "20260831_0045"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "procurement_demands" not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind, "procurement_demands")
    if "manual_image_relative_path" not in columns:
        op.add_column("procurement_demands", sa.Column("manual_image_relative_path", sa.Text(), nullable=True))
    if "manual_image_original_filename" not in columns:
        op.add_column("procurement_demands", sa.Column("manual_image_original_filename", sa.String(255), nullable=True))
    if "manual_image_content_type" not in columns:
        op.add_column("procurement_demands", sa.Column("manual_image_content_type", sa.String(100), nullable=True))
    if "manual_image_file_size" not in columns:
        op.add_column("procurement_demands", sa.Column("manual_image_file_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "procurement_demands" not in sa.inspect(bind).get_table_names():
        return
    columns = _columns(bind, "procurement_demands")
    for column in (
        "manual_image_file_size", "manual_image_content_type",
        "manual_image_original_filename", "manual_image_relative_path",
    ):
        if column in columns:
            op.drop_column("procurement_demands", column)
