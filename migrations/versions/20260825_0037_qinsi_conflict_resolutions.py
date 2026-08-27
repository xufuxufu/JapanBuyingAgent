"""QinSi conflict resolutions"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_0037"
down_revision = "20260821_0036"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "qinsi_conflict_resolutions" not in _tables(bind):
        op.create_table(
            "qinsi_conflict_resolutions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conflict_key", sa.String(255), nullable=False),
            sa.Column("barcode", sa.String(100), nullable=True),
            sa.Column("qinsi_product_codes", sa.Text(), nullable=False),
            sa.Column("resolution_type", sa.String(40), nullable=False),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    indexes = _indexes(bind, "qinsi_conflict_resolutions")
    if "uq_qinsi_conflict_resolutions_key" not in indexes:
        op.create_index("uq_qinsi_conflict_resolutions_key", "qinsi_conflict_resolutions", ["conflict_key"], unique=True)
    if "ix_qinsi_conflict_resolutions_barcode" not in indexes:
        op.create_index("ix_qinsi_conflict_resolutions_barcode", "qinsi_conflict_resolutions", ["barcode"])
    if "ix_qinsi_conflict_resolutions_auto" not in indexes:
        op.create_index("ix_qinsi_conflict_resolutions_auto", "qinsi_conflict_resolutions", ["auto_apply"])

    if "product_barcodes" in _tables(bind):
        indexes = _indexes(bind, "product_barcodes")
        if "uq_product_barcodes_barcode" in indexes:
            op.drop_index("uq_product_barcodes_barcode", table_name="product_barcodes")
        if "ix_product_barcodes_barcode" not in indexes:
            op.create_index("ix_product_barcodes_barcode", "product_barcodes", ["barcode"])


def downgrade() -> None:
    bind = op.get_bind()
    if "product_barcodes" in _tables(bind):
        indexes = _indexes(bind, "product_barcodes")
        if "ix_product_barcodes_barcode" in indexes:
            op.drop_index("ix_product_barcodes_barcode", table_name="product_barcodes")
        if "uq_product_barcodes_barcode" not in indexes:
            op.create_index("uq_product_barcodes_barcode", "product_barcodes", ["barcode"], unique=True)
    if "qinsi_conflict_resolutions" in _tables(bind):
        op.drop_table("qinsi_conflict_resolutions")
