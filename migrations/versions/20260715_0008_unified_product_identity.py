"""unified stable product identity"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re

from alembic import op
import sqlalchemy as sa


revision = "20260715_0008"
down_revision = "20260715_0007"
branch_labels = None
depends_on = None

TOKYO = timezone(timedelta(hours=9), "Asia/Tokyo")
SKU_PATTERN = re.compile(r"^NJ-(\d{8})-(\d{6})$")


def _day(value) -> str:
    if isinstance(value, datetime):
        instant = value
    else:
        try:
            instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            instant = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(TOKYO).strftime("%Y%m%d")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in inspector.get_table_names():
        return
    columns = {column["name"]: column for column in inspector.get_columns("products")}
    if "internal_sku" not in columns:
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column("internal_sku", sa.String(32), nullable=True))

    rows = list(bind.execute(sa.text("SELECT id, internal_sku, created_at FROM products ORDER BY id")).mappings())
    used = {row["internal_sku"] for row in rows if row["internal_sku"]}
    sequences: dict[str, int] = defaultdict(int)
    for sku in used:
        match = SKU_PATTERN.fullmatch(sku)
        if match:
            sequences[match.group(1)] = max(sequences[match.group(1)], int(match.group(2)))
    for row in rows:
        if row["internal_sku"]:
            continue
        day = _day(row["created_at"])
        while True:
            sequences[day] += 1
            sku = f"NJ-{day}-{sequences[day]:06d}"
            if sku not in used:
                break
        used.add(sku)
        bind.execute(sa.text("UPDATE products SET internal_sku=:sku WHERE id=:id"), {"sku": sku, "id": row["id"]})

    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("products")}
    unique_constraints = {item["name"] for item in inspector.get_unique_constraints("products")}
    if "uq_products_internal_sku" not in indexes | unique_constraints:
        op.create_index("uq_products_internal_sku", "products", ["internal_sku"], unique=True)
    column = next(item for item in sa.inspect(bind).get_columns("products") if item["name"] == "internal_sku")
    if column.get("nullable", True):
        with op.batch_alter_table("products") as batch:
            batch.alter_column("internal_sku", existing_type=sa.String(32), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    if "products" not in sa.inspect(bind).get_table_names():
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("products")}
    if "internal_sku" not in columns:
        return
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("products")}
    if "uq_products_internal_sku" in indexes:
        op.drop_index("uq_products_internal_sku", table_name="products")
    with op.batch_alter_table("products") as batch:
        batch.drop_column("internal_sku")
