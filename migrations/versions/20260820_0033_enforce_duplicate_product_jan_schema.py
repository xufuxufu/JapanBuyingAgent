"""Enforce duplicate product JAN schema"""

from alembic import op
import sqlalchemy as sa


revision = "20260820_0033"
down_revision = "20260820_0032"
branch_labels = None
depends_on = None


JAN_INDEX_NAME = "ix_products_jan_not_null"
QINSI_CODE_INDEX_NAME = "uq_products_qinsi_code_not_null"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _index_columns(bind, index_name: str) -> list[str]:
    return [row[2] for row in bind.exec_driver_sql(f'PRAGMA index_info("{index_name}")')]


def _product_indexes(bind) -> list[dict]:
    if bind.dialect.name != "sqlite":
        return []
    return [dict(row._mapping) for row in bind.exec_driver_sql('PRAGMA index_list("products")')]


def _drop_single_jan_unique_indexes(bind) -> None:
    if bind.dialect.name != "sqlite":
        index_names = {index["name"] for index in sa.inspect(bind).get_indexes("products")}
        if "uq_products_jan_not_null" in index_names:
            op.drop_index("uq_products_jan_not_null", table_name="products")
        return

    for index in _product_indexes(bind):
        name = index["name"]
        if not index["unique"] or _index_columns(bind, name) != ["jan"]:
            continue
        if index.get("origin") == "c":
            op.drop_index(name, table_name="products")


def _drop_single_jan_table_uniques(bind) -> None:
    constraints = [
        constraint for constraint in sa.inspect(bind).get_unique_constraints("products")
        if constraint.get("column_names") == ["jan"]
    ]
    sqlite_auto_uniques = [
        index for index in _product_indexes(bind)
        if index["unique"] and index.get("origin") == "u" and _index_columns(bind, index["name"]) == ["jan"]
    ]
    if not constraints and not sqlite_auto_uniques:
        return
    _rebuild_products_without_jan_unique(bind)


def _is_single_jan_unique_constraint(constraint: sa.Constraint) -> bool:
    return (
        isinstance(constraint, sa.UniqueConstraint)
        and [column.name for column in constraint.columns] == ["jan"]
    )


def _preserved_indexes(bind) -> list[dict]:
    indexes: list[dict] = []
    for index in sa.inspect(bind).get_indexes("products"):
        if index.get("unique") and index.get("column_names") == ["jan"]:
            continue
        indexes.append(index)
    return indexes


def _recreate_indexes(indexes: list[dict]) -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("products")}
    for index in indexes:
        name = index["name"]
        if not name or name in existing:
            continue
        dialect_options = index.get("dialect_options") or {}
        op.create_index(
            name,
            "products",
            index.get("column_names") or [],
            unique=bool(index.get("unique")),
            sqlite_where=dialect_options.get("sqlite_where"),
        )
        existing.add(name)


def _rebuild_products_without_jan_unique(bind) -> None:
    metadata = sa.MetaData()
    old = sa.Table("products", metadata, autoload_with=bind)
    temp_name = "_products_jan_unique_fix"
    indexes = _preserved_indexes(bind)
    temp_metadata = sa.MetaData()
    columns = [column.copy() for column in old.columns]
    constraints = [
        constraint.copy()
        for constraint in old.constraints
        if not _is_single_jan_unique_constraint(constraint)
    ]
    temp = sa.Table(temp_name, temp_metadata, *columns, *constraints)
    temp.create(bind)
    preparer = bind.dialect.identifier_preparer
    column_names = [column.name for column in old.columns]
    quoted_columns = ", ".join(preparer.quote(name) for name in column_names)
    op.execute(sa.text(
        f"INSERT INTO {preparer.quote(temp_name)} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {preparer.quote('products')}"
    ))
    op.drop_table("products")
    op.rename_table(temp_name, "products")
    _recreate_indexes(indexes)


def _create_expected_indexes(bind) -> None:
    indexes = {index["name"]: index for index in sa.inspect(bind).get_indexes("products")}
    columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
    if "jan" in columns and JAN_INDEX_NAME not in indexes:
        op.create_index(
            JAN_INDEX_NAME,
            "products",
            ["jan"],
            unique=False,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )
    if "qinsi_product_code" in columns and QINSI_CODE_INDEX_NAME not in indexes:
        op.create_index(
            QINSI_CODE_INDEX_NAME,
            "products",
            ["qinsi_product_code"],
            unique=True,
            sqlite_where=sa.text("qinsi_product_code IS NOT NULL"),
        )


def upgrade() -> None:
    bind = op.get_bind()
    if "products" not in _tables(bind):
        return
    _drop_single_jan_unique_indexes(bind)
    _drop_single_jan_table_uniques(bind)
    _drop_single_jan_unique_indexes(bind)
    _create_expected_indexes(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if "products" not in _tables(bind):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("products")}
    if JAN_INDEX_NAME in indexes:
        op.drop_index(JAN_INDEX_NAME, table_name="products")
    if "uq_products_jan_not_null" not in {index["name"] for index in sa.inspect(bind).get_indexes("products")}:
        op.create_index(
            "uq_products_jan_not_null",
            "products",
            ["jan"],
            unique=True,
            sqlite_where=sa.text("jan IS NOT NULL"),
        )
