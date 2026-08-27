"""Product image quality metadata"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_0035"
down_revision = "20260820_0034"
branch_labels = None
depends_on = None


def _product_columns(bind) -> set[str]:
    if "products" not in set(sa.inspect(bind).get_table_names()):
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns("products")}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _product_columns(bind)
    if not columns:
        return
    additions = (
        ("image_width", sa.Integer()),
        ("image_height", sa.Integer()),
        ("image_quality", sa.String(20)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column("products", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = _product_columns(bind)
    for name in ("image_quality", "image_height", "image_width"):
        if name in columns:
            op.drop_column("products", name)
