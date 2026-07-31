"""QinSi-derived JAN aliases and optional field-purchase store.

Revision ID: 20260720_0021
Revises: 20260720_0020
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = "20260720_0021"
down_revision = "20260720_0020"
branch_labels = None
depends_on = None

_DERIVED_PATTERN = re.compile(r"^/([0-9]{8}|[0-9]{13})$")


def _valid_jan(value: str) -> bool:
    if not value.isdigit() or len(value) not in {8, 13}:
        return False
    digits = [int(char) for char in value]
    weighted = sum(
        digit * (3 if (len(digits) - index) % 2 == 0 else 1)
        for index, digit in enumerate(digits[:-1])
    )
    return (10 - weighted % 10) % 10 == digits[-1]


def _derived_jan(value: str | None) -> str | None:
    match = _DERIVED_PATTERN.fullmatch((value or "").strip())
    if match is None:
        return None
    jan = match.group(1)
    return jan if _valid_jan(jan) else None


def _backfill_derived_barcodes() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "products" not in tables or "product_barcodes" not in tables:
        return
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    barcode_columns = {
        column["name"] for column in inspector.get_columns("product_barcodes")
    }
    if not {"jan", "qinsi_product_code"} <= product_columns or not {
        "product_id", "barcode", "source_system", "is_primary", "created_at", "updated_at",
    } <= barcode_columns:
        return
    product_rows = bind.execute(
        sa.text(
            "SELECT id, qinsi_product_code FROM products "
            "WHERE qinsi_product_code IS NOT NULL ORDER BY id"
        )
    ).mappings()
    grouped: dict[str, list[int]] = {}
    for row in product_rows:
        jan = _derived_jan(row["qinsi_product_code"])
        if jan:
            grouped.setdefault(jan, []).append(int(row["id"]))

    now = datetime.now(timezone.utc)
    for jan, product_ids in grouped.items():
        if len(product_ids) != 1:
            continue
        mapped_product_ids = {
            int(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT id FROM products "
                    "WHERE jan = :jan "
                    "OR qinsi_product_code = :jan "
                    "OR qinsi_product_code = :derived_code"
                ),
                {"jan": jan, "derived_code": f"/{jan}"},
            )
        }
        existing_ids = {
            int(row[0])
            for row in bind.execute(
                sa.text("SELECT product_id FROM product_barcodes WHERE barcode = :barcode"),
                {"barcode": jan},
            )
        }
        if mapped_product_ids | existing_ids != {product_ids[0]}:
            continue
        if existing_ids:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO product_barcodes "
                "(product_id, barcode, source_system, is_primary, created_at, updated_at) "
                "VALUES (:product_id, :barcode, 'qinsi_sku_derived', 0, :created_at, :updated_at)"
            ),
            {
                "product_id": product_ids[0],
                "barcode": jan,
                "created_at": now,
                "updated_at": now,
            },
        )


def _field_purchase_batches_table(
    *,
    store_nullable: bool,
    include_store_foreign_key: bool,
) -> sa.Table:
    metadata = sa.MetaData()
    if include_store_foreign_key:
        sa.Table(
            "stores",
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
        )
        store_column = sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="RESTRICT"),
            nullable=store_nullable,
        )
    else:
        # Old migration regression fixtures intentionally contain only a subset of
        # the historical schema.  Preserve the column there without manufacturing
        # a foreign key to a table that does not exist.
        store_column = sa.Column(
            "store_id",
            sa.Integer(),
            nullable=store_nullable,
        )
    return sa.Table(
        "field_purchase_batches",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_no", sa.String(50), nullable=False),
        sa.Column("client_request_id", sa.String(100), nullable=False),
        store_column,
        sa.Column("operator_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), server_default="ACTIVE", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','COMPLETED','CANCELLED')",
            name="ck_field_purchase_batches_status",
        ),
        sa.Index("uq_field_purchase_batches_batch_no", "batch_no", unique=True),
        sa.Index(
            "ix_field_purchase_batches_client_request_id",
            "client_request_id",
            unique=True,
        ),
        sa.Index("ix_field_purchase_batches_store_id", "store_id"),
        sa.Index("ix_field_purchase_batches_status", "status"),
    )


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    # Some historical migration regression fixtures are deliberately partial.
    # SQLite cannot rebuild the batch table while its child table references a
    # missing products/stores table.  A real 0020 application database always
    # has both tables, so only the unusable partial fixture skips this alteration.
    if {"field_purchase_batches", "products", "stores"} <= tables:
        with op.batch_alter_table(
            "field_purchase_batches",
            copy_from=_field_purchase_batches_table(
                store_nullable=False,
                include_store_foreign_key=True,
            ),
            recreate="always",
        ) as batch_op:
            batch_op.alter_column(
                "store_id",
                existing_type=sa.Integer(),
                nullable=True,
            )
    _backfill_derived_barcodes()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    can_rebuild_batches = {"field_purchase_batches", "products", "stores"} <= tables
    if can_rebuild_batches:
        null_count = bind.execute(
            sa.text("SELECT COUNT(*) FROM field_purchase_batches WHERE store_id IS NULL")
        ).scalar_one()
        if null_count:
            raise RuntimeError("存在未填写门店的现场采购批次，不能安全降级到 0020")
    if "product_barcodes" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM product_barcodes "
                "WHERE source_system = 'qinsi_sku_derived'"
            )
        )
    if can_rebuild_batches:
        with op.batch_alter_table(
            "field_purchase_batches",
            copy_from=_field_purchase_batches_table(
                store_nullable=True,
                include_store_foreign_key=True,
            ),
            recreate="always",
        ) as batch_op:
            batch_op.alter_column(
                "store_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
