"""QinSi product master raw fields"""

from alembic import op
import sqlalchemy as sa


revision = "20260819_0031"
down_revision = "20260819_0030"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    if table not in _tables(bind):
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "products" not in _tables(bind):
        return
    existing = _columns(bind, "products")
    additions = [
        sa.Column("qinsi_product_barcode", sa.String(100)),
        sa.Column("qinsi_unit_barcode", sa.String(100)),
        sa.Column("qinsi_name", sa.String(255)),
        sa.Column("qinsi_image_url", sa.Text()),
        sa.Column("qinsi_brand", sa.String(128)),
        sa.Column("qinsi_category", sa.String(128)),
        sa.Column("qinsi_unit", sa.String(128)),
        sa.Column("qinsi_status", sa.String(50)),
        sa.Column("qinsi_remark", sa.Text()),
        sa.Column("qinsi_synced_at", sa.DateTime(timezone=True)),
        sa.Column("has_jan", sa.Boolean(), server_default=sa.true(), nullable=False),
    ]
    for column in additions:
        if column.name not in existing:
            op.add_column("products", column)
    if "has_jan" in _columns(bind, "products"):
        op.execute(sa.text("UPDATE products SET has_jan = CASE WHEN jan IS NULL OR trim(jan) = '' THEN 0 ELSE 1 END"))


def downgrade() -> None:
    bind = op.get_bind()
    if "products" not in _tables(bind):
        return
    existing = _columns(bind, "products")
    with op.batch_alter_table("products") as batch:
        for name in (
            "has_jan", "qinsi_synced_at", "qinsi_remark", "qinsi_status",
            "qinsi_unit", "qinsi_category", "qinsi_brand", "qinsi_image_url",
            "qinsi_name", "qinsi_unit_barcode", "qinsi_product_barcode",
        ):
            if name in existing:
                batch.drop_column(name)
