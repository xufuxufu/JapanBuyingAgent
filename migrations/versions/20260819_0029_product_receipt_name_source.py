from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260819_0029"
down_revision = "20260731_0028"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if table_name not in tables:
        return set()
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table_name})")}


def upgrade() -> None:
    columns = _columns("products")
    if not columns:
        return
    if "name_source" not in columns:
        op.add_column("products", sa.Column("name_source", sa.String(length=30), nullable=True))
    if "needs_review" not in columns:
        op.add_column(
            "products",
            sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    columns = _columns("products")
    if "needs_review" in columns:
        op.drop_column("products", "needs_review")
    if "name_source" in columns:
        op.drop_column("products", "name_source")
