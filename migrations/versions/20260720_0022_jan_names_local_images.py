"""Unified JAN governance, Japanese-name migration, and local product images.

Revision ID: 20260720_0022
Revises: 20260720_0021
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = "20260720_0022"
down_revision = "20260720_0021"
branch_labels = None
depends_on = None

_ACTOR = "migration:20260720_0022"


def _trimmed(value: str | None) -> str | None:
    trimmed = (value or "").strip()
    return trimmed or None


def _display_name(name_cn: str | None, name_ja: str | None) -> str:
    cn = _trimmed(name_cn) or "中文名待补"
    ja = _trimmed(name_ja) or "日文名待补"
    raw = f"{cn}｜{ja}"
    if len(raw) <= 128:
        return raw
    cn_budget = 63
    ja_budget = 64
    return f"{cn[:cn_budget]}｜{ja[:ja_budget]}"


def _audit_exists(bind) -> bool:
    return "enrichment_audit_logs" in set(sa.inspect(bind).get_table_names())


def _write_audit(bind, *, before: dict, after: dict) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO enrichment_audit_logs "
            "(field_purchase_item_id, enrichment_task_id, action, actor, before_json, "
            "after_json, source, created_at) "
            "VALUES (NULL, NULL, 'BULK_EDIT', :actor, :before_json, :after_json, "
            "'migration', :created_at)"
        ),
        {
            "actor": _ACTOR,
            "before_json": json.dumps(before, ensure_ascii=False),
            "after_json": json.dumps(after, ensure_ascii=False),
            "created_at": datetime.now(timezone.utc),
        },
    )


def _migrate_product_note_to_name_ja() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("products")}
    if not {"id", "name_cn", "name_ja", "display_name", "product_note"} <= columns:
        return
    has_audit = _audit_exists(bind)
    if has_audit:
        already_done = bind.execute(
            sa.text(
                "SELECT 1 FROM enrichment_audit_logs "
                "WHERE actor = :actor AND source = 'migration' LIMIT 1"
            ),
            {"actor": _ACTOR},
        ).first()
        if already_done:
            return

    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, name_cn, name_ja, display_name, product_note "
                "FROM products WHERE product_note IS NOT NULL ORDER BY id"
            )
        ).mappings()
    )
    before_nonempty = sum(_trimmed(row["product_note"]) is not None for row in rows)
    empty_to_null = 0
    migrated = 0
    conflicts = 0
    for row in rows:
        note = _trimmed(row["product_note"])
        if note is None:
            empty_to_null += 1
            bind.execute(
                sa.text("UPDATE products SET product_note = NULL WHERE id = :product_id"),
                {"product_id": row["id"]},
            )
            continue
        current_name_ja = _trimmed(row["name_ja"])
        conflict = current_name_ja is not None and current_name_ja != note
        if conflict:
            conflicts += 1
            if has_audit:
                _write_audit(
                    bind,
                    before={
                        "scope": "product_note_to_name_ja_conflict",
                        "product_id": row["id"],
                        "name_ja": current_name_ja,
                        "product_note": note,
                    },
                    after={
                        "product_id": row["id"],
                        "name_ja": note,
                        "product_note": None,
                        "decision": "product_note_is_japanese_name_source",
                    },
                )
        bind.execute(
            sa.text(
                "UPDATE products SET name_ja = :name_ja, display_name = :display_name, "
                "product_note = NULL WHERE id = :product_id"
            ),
            {
                "name_ja": note,
                "display_name": _display_name(row["name_cn"], note),
                "product_id": row["id"],
            },
        )
        migrated += 1

    if has_audit:
        _write_audit(
            bind,
            before={
                "scope": "product_note_to_name_ja_summary",
                "nonempty_product_note_count": before_nonempty,
                "rows_with_product_note": len(rows),
            },
            after={
                "migrated_count": migrated,
                "conflict_count": conflicts,
                "empty_to_null_count": empty_to_null,
                "remaining_nonempty_product_note_count": 0,
            },
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "products" in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
        additions = (
            ("display_image_url", sa.Text()),
            ("local_image_path", sa.Text()),
            ("image_sha256", sa.String(64)),
            ("image_localization_status", sa.String(30)),
            ("image_localization_source_url", sa.Text()),
            ("image_localized_at", sa.DateTime(timezone=True)),
            ("image_localization_error", sa.Text()),
        )
        for name, column_type in additions:
            if name not in columns:
                op.add_column("products", sa.Column(name, column_type, nullable=True))
        op.create_index(
            "ix_products_image_sha256", "products", ["image_sha256"], unique=False,
            if_not_exists=True,
        )
        op.create_index(
            "ix_products_image_localization_status", "products",
            ["image_localization_status"], unique=False, if_not_exists=True,
        )
        if "main_image_path" in columns:
            bind.execute(
                sa.text(
                    "UPDATE products SET display_image_url = '/product-images/' || id "
                    "WHERE display_image_url IS NULL AND main_image_path IS NOT NULL"
                )
            )
    _migrate_product_note_to_name_ja()


def downgrade() -> None:
    bind = op.get_bind()
    if "products" not in set(sa.inspect(bind).get_table_names()):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("products")}
    if "ix_products_image_localization_status" in indexes:
        op.drop_index("ix_products_image_localization_status", table_name="products")
    if "ix_products_image_sha256" in indexes:
        op.drop_index("ix_products_image_sha256", table_name="products")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
    for name in (
        "image_localization_error",
        "image_localized_at",
        "image_localization_source_url",
        "image_localization_status",
        "image_sha256",
        "local_image_path",
        "display_image_url",
    ):
        if name in columns:
            op.drop_column("products", name)
