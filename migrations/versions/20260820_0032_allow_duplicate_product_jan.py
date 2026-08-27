"""Allow duplicate product JAN values"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_0032"
down_revision = "20260819_0031"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set[str]:
    if table not in sa.inspect(bind).get_table_names():
        return set()
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _has_column(bind, table: str, column: str) -> bool:
    if table not in sa.inspect(bind).get_table_names():
        return False
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "products", "jan"):
        return
    names = _index_names(bind, "products")
    if "uq_products_jan_not_null" in names:
        op.drop_index("uq_products_jan_not_null", table_name="products")
        names.remove("uq_products_jan_not_null")
    if "ix_products_jan_not_null" not in names:
        op.create_index(
            "ix_products_jan_not_null",
            "products",
            ["jan"],
            unique=False,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "products", "jan"):
        return
    names = _index_names(bind, "products")
    if "ix_products_jan_not_null" in names:
        op.drop_index("ix_products_jan_not_null", table_name="products")
    if "uq_products_jan_not_null" not in _index_names(bind, "products"):
        op.create_index(
            "uq_products_jan_not_null",
            "products",
            ["jan"],
            unique=True,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )
