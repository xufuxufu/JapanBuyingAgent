"""product import, JAN matching and review status synchronization"""
from alembic import op
import sqlalchemy as sa


revision = "20260715_0007"
down_revision = "20260715_0006"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    if table not in sa.inspect(op.get_bind()).get_table_names():
        return
    existing = _columns(table)
    missing = [column for column in columns if column.name not in existing]
    if missing:
        with op.batch_alter_table(table) as batch:
            for column in missing:
                batch.add_column(column)


def upgrade() -> None:
    _add_columns("receipts", [sa.Column("review_status", sa.String(20), server_default="pending", nullable=False)])
    _add_columns("receipt_items", [
        sa.Column("matched_at", sa.DateTime(timezone=True)),
        sa.Column("match_method", sa.String(50)),
        sa.Column("match_confidence", sa.Float()),
    ])
    _add_columns("products", [sa.Column("product_origin", sa.String(20), server_default="manual", nullable=False)])
    _add_columns("product_aliases", [
        sa.Column("normalized_alias", sa.String(255), server_default="", nullable=False),
        sa.Column("confirmed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_from_item_id", sa.Integer()),
    ])
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "product_aliases" in tables:
        alias_fks = {tuple(item.get("constrained_columns") or ()) for item in sa.inspect(op.get_bind()).get_foreign_keys("product_aliases")}
        if ("created_from_item_id",) not in alias_fks and "receipt_items" in tables:
            with op.batch_alter_table("product_aliases") as batch:
                batch.create_foreign_key("fk_product_aliases_receipt_item", "receipt_items", ["created_from_item_id"], ["id"], ondelete="SET NULL")
        op.execute("UPDATE product_aliases SET normalized_alias=lower(trim(alias)) WHERE normalized_alias='' OR normalized_alias IS NULL")
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("product_aliases")}
        if "uq_product_alias_product_normalized" not in indexes:
            op.create_index("uq_product_alias_product_normalized", "product_aliases", ["product_id", "normalized_alias"], unique=True)

    _add_columns("import_jobs", [
        sa.Column("original_filename", sa.String(255)),
        sa.Column("total_rows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("success_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("warning_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("summary_json", sa.Text()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    ])
    _add_columns("import_rows", [
        sa.Column("parsed_json", sa.Text()),
        sa.Column("warnings_json", sa.Text()),
        sa.Column("product_id", sa.Integer()),
    ])
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "import_rows" in tables:
        row_fks = {tuple(item.get("constrained_columns") or ()) for item in sa.inspect(op.get_bind()).get_foreign_keys("import_rows")}
        if ("product_id",) not in row_fks and "products" in tables:
            with op.batch_alter_table("import_rows") as batch:
                batch.create_foreign_key("fk_import_rows_product", "products", ["product_id"], ["id"], ondelete="SET NULL")

    if {"receipt_items", "products"} <= set(sa.inspect(op.get_bind()).get_table_names()) and "product_match_logs" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "product_match_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("receipt_items.id", ondelete="CASCADE"), nullable=False),
            sa.Column("old_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("new_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL")),
            sa.Column("method", sa.String(50), nullable=False),
            sa.Column("decision", sa.String(30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_product_match_logs_receipt_item_id", "product_match_logs", ["receipt_item_id"])


def downgrade() -> None:
    if "product_match_logs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("product_match_logs")
    for table, names in (
        ("import_rows", ("product_id", "warnings_json", "parsed_json")),
        ("import_jobs", ("confirmed_at", "summary_json", "warning_count", "error_count", "conflict_count", "skipped_count", "success_count", "total_rows", "original_filename")),
        ("product_aliases", ("created_from_item_id", "confirmed", "normalized_alias")),
        ("products", ("product_origin",)),
        ("receipt_items", ("match_confidence", "match_method", "matched_at")),
        ("receipts", ("review_status",)),
    ):
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            for name in names:
                if name in existing:
                    batch.drop_column(name)
