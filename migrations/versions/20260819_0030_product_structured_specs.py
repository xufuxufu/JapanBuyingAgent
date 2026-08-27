from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260819_0030"
down_revision = "20260819_0029"
branch_labels = None
depends_on = None


SPEC_COLUMNS = (
    ("net_weight_g", sa.Numeric(18, 3)),
    ("volume_ml", sa.Numeric(18, 3)),
    ("length_mm", sa.Numeric(18, 3)),
    ("width_mm", sa.Numeric(18, 3)),
    ("height_mm", sa.Numeric(18, 3)),
    ("depth_mm", sa.Numeric(18, 3)),
    ("pack_quantity", sa.Integer()),
    ("spec_text", sa.Text()),
)


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if table_name not in tables:
        return set()
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table_name})")}


def upgrade() -> None:
    for table_name in ("products", "product_enrichment_candidates"):
        columns = _columns(table_name)
        if not columns:
            continue
        for name, column_type in SPEC_COLUMNS:
            if name not in columns:
                op.add_column(table_name, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for table_name in ("product_enrichment_candidates", "products"):
        columns = _columns(table_name)
        for name, _column_type in reversed(SPEC_COLUMNS):
            if name in columns:
                op.drop_column(table_name, name)
