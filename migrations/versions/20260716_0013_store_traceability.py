"""store master data and purchase traceability MVP"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0013"
down_revision = "20260716_0012"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _foreign_key_columns(bind, table: str) -> set[tuple[str, ...]]:
    return {tuple(item["constrained_columns"]) for item in sa.inspect(bind).get_foreign_keys(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "store_brands" not in tables:
        op.create_table(
            "store_brands",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name_cn", sa.String(128)),
            sa.Column("name_ja", sa.String(128)),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "stores" in tables:
        columns = _columns(bind, "stores")
        additions = (
            sa.Column("brand_id", sa.Integer()), sa.Column("name_cn", sa.String(128)),
            sa.Column("name_ja", sa.String(128)), sa.Column("raw_name", sa.String(255)),
            sa.Column("phone", sa.String(50)), sa.Column("normalized_phone", sa.String(32)),
            sa.Column("postal_code", sa.String(20)), sa.Column("normalized_postal_code", sa.String(20)),
            sa.Column("address", sa.Text()), sa.Column("normalized_address", sa.Text()),
            sa.Column("receipt_store_code", sa.String(100)),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("is_online", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        )
        with op.batch_alter_table("stores") as batch:
            for column in additions:
                if column.name not in columns:
                    batch.add_column(column)
            if ("brand_id",) not in _foreign_key_columns(bind, "stores"):
                batch.create_foreign_key("fk_stores_brand_id", "store_brands", ["brand_id"], ["id"], ondelete="SET NULL")
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("stores")}
        if "ix_stores_brand_id" not in indexes:
            op.create_index("ix_stores_brand_id", "stores", ["brand_id"])
        if "uq_stores_receipt_code_not_null" not in indexes:
            op.create_index("uq_stores_receipt_code_not_null", "stores", ["receipt_store_code"], unique=True, sqlite_where=sa.text("receipt_store_code IS NOT NULL"))
        if "uq_stores_phone_not_null" not in indexes:
            op.create_index("uq_stores_phone_not_null", "stores", ["normalized_phone"], unique=True, sqlite_where=sa.text("normalized_phone IS NOT NULL"))

    if "receipts" in tables:
        columns = _columns(bind, "receipts")
        additions = (
            sa.Column("raw_store_code", sa.String(100)), sa.Column("raw_store_phone", sa.String(50)),
            sa.Column("raw_store_postal_code", sa.String(20)), sa.Column("raw_store_address", sa.Text()),
            sa.Column("raw_store_branch_name", sa.String(255)), sa.Column("store_id", sa.Integer()),
            sa.Column("store_match_status", sa.String(30), server_default="pending", nullable=False),
            sa.Column("store_match_method", sa.String(30)), sa.Column("store_match_confidence", sa.Float()),
        )
        for column in additions:
            if column.name not in columns:
                op.add_column("receipts", column)
        if "stores" in tables and ("store_id",) not in _foreign_key_columns(bind, "receipts"):
            with op.batch_alter_table("receipts") as batch:
                batch.create_foreign_key("fk_receipts_store_id", "stores", ["store_id"], ["id"], ondelete="SET NULL")
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("receipts")}
        if "ix_receipts_store_id" not in indexes:
            op.create_index("ix_receipts_store_id", "receipts", ["store_id"])
        if "ix_receipts_store_match_status" not in indexes:
            op.create_index("ix_receipts_store_match_status", "receipts", ["store_match_status"])

    if "purchase_batches" in tables:
        columns = _columns(bind, "purchase_batches")
        if "store_id" not in columns:
            op.add_column("purchase_batches", sa.Column("store_id", sa.Integer()))
        if {"stores", "receipts"} <= tables and ("store_id",) not in _foreign_key_columns(bind, "purchase_batches"):
            with op.batch_alter_table("purchase_batches") as batch:
                batch.create_foreign_key("fk_purchase_batches_store_id", "stores", ["store_id"], ["id"], ondelete="SET NULL")
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("purchase_batches")}
        if "ix_purchase_batches_store_id" not in indexes:
            op.create_index("ix_purchase_batches_store_id", "purchase_batches", ["store_id"])

    tables = set(sa.inspect(bind).get_table_names())
    if {"stores", "receipts"} <= tables and "store_aliases" not in tables:
        op.create_table(
            "store_aliases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
            sa.Column("alias", sa.String(255), nullable=False),
            sa.Column("normalized_alias", sa.String(255), nullable=False),
            sa.Column("source_receipt_id", sa.Integer(), sa.ForeignKey("receipts.id", ondelete="SET NULL")),
            sa.Column("confirmed", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("normalized_alias", name="uq_store_aliases_normalized"),
        )
        for column in ("store_id", "normalized_alias", "source_receipt_id"):
            op.create_index(f"ix_store_aliases_{column}", "store_aliases", [column])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "store_aliases" in tables:
        op.drop_table("store_aliases")
    if "purchase_batches" in tables and "store_id" in _columns(bind, "purchase_batches"):
        with op.batch_alter_table("purchase_batches") as batch:
            batch.drop_column("store_id")
    if "receipts" in tables:
        columns = _columns(bind, "receipts")
        with op.batch_alter_table("receipts") as batch:
            for name in ("store_match_confidence", "store_match_method", "store_match_status", "store_id", "raw_store_branch_name", "raw_store_address", "raw_store_postal_code", "raw_store_phone", "raw_store_code"):
                if name in columns:
                    batch.drop_column(name)
    if "stores" in tables:
        columns = _columns(bind, "stores")
        with op.batch_alter_table("stores") as batch:
            for name in ("updated_at", "is_online", "is_active", "receipt_store_code", "normalized_address", "address", "normalized_postal_code", "postal_code", "normalized_phone", "phone", "raw_name", "name_ja", "name_cn", "brand_id"):
                if name in columns:
                    batch.drop_column(name)
    if "store_brands" in tables:
        op.drop_table("store_brands")
