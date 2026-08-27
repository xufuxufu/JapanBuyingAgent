"""restore unique product JAN index after duplicate merge"""

from alembic import op
import sqlalchemy as sa


revision = "20260821_0036"
down_revision = "20260820_0035"
branch_labels = None
depends_on = None


def _index_names(bind) -> set[str]:
    if "products" not in sa.inspect(bind).get_table_names():
        return set()
    return {index["name"] for index in sa.inspect(bind).get_indexes("products")}


def _duplicate_jans(bind) -> list[str]:
    rows = bind.exec_driver_sql(
        """
        SELECT jan
        FROM products
        WHERE jan IS NOT NULL
        GROUP BY jan
        HAVING COUNT(*) > 1
        LIMIT 5
        """
    )
    return [row[0] for row in rows]


def upgrade() -> None:
    bind = op.get_bind()
    if "products" not in sa.inspect(bind).get_table_names():
        return
    duplicates = _duplicate_jans(bind)
    if duplicates:
        raise RuntimeError(f"products.jan still has duplicates; merge before migration: {', '.join(duplicates)}")
    names = _index_names(bind)
    if "ix_products_jan_not_null" in names:
        op.drop_index("ix_products_jan_not_null", table_name="products")
        names.remove("ix_products_jan_not_null")
    if "uq_products_jan_not_null" not in names:
        op.create_index(
            "uq_products_jan_not_null",
            "products",
            ["jan"],
            unique=True,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "products" not in sa.inspect(bind).get_table_names():
        return
    names = _index_names(bind)
    if "uq_products_jan_not_null" in names:
        op.drop_index("uq_products_jan_not_null", table_name="products")
    if "ix_products_jan_not_null" not in _index_names(bind):
        op.create_index(
            "ix_products_jan_not_null",
            "products",
            ["jan"],
            unique=False,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )
