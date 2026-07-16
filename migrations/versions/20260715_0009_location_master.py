"""location and warehouse master data"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260715_0009"
down_revision = "20260715_0008"
branch_labels = None
depends_on = None

DEFAULT_LOCATIONS = (
    ("QW-2025-QIANYU", "2025千羽", "qinsi_warehouse", True, 10),
    ("QW-2025-ZHAOCAIMAO", "2025招财猫", "qinsi_warehouse", True, 20),
    ("QW-NO-BARCODE", "无条码商品", "qinsi_warehouse", True, 30),
    ("QW-NEW-JAPAN", "新日本仓库", "qinsi_warehouse", True, 40),
    ("LOC-JP-HOME", "日本家里库存", "local_physical", True, 50),
    ("TRANSIT-INTERNATIONAL", "国际快递在途", "transit", False, 100),
    ("TRANSIT-HAND-CARRY", "人工带货在途", "transit", False, 110),
    ("STATUS-UNASSIGNED", "待分配", "system_status", False, 120),
    ("STATUS-QINSI-HANDOFF", "已交接秦丝", "system_status", False, 130),
)


def upgrade() -> None:
    bind = op.get_bind()
    if "locations" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "locations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("internal_code", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("location_type", sa.String(30), nullable=False),
            sa.Column("is_qinsi_warehouse", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("internal_code", name="uq_locations_internal_code"),
            sa.CheckConstraint(
                "location_type IN ('qinsi_warehouse','local_physical','transit','system_status')",
                name="ck_locations_type",
            ),
        )
        op.create_index("ix_locations_location_type", "locations", ["location_type"])

    insert = sa.text("""
        INSERT INTO locations
            (internal_code, display_name, location_type, is_qinsi_warehouse, is_active, sort_order, created_at, updated_at)
        SELECT :internal_code, :display_name, :location_type, :is_qinsi_warehouse, 1, :sort_order, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM locations WHERE internal_code=:internal_code)
    """)
    for code, name, location_type, is_qinsi, sort_order in DEFAULT_LOCATIONS:
        bind.execute(insert, {
            "internal_code": code, "display_name": name, "location_type": location_type,
            "is_qinsi_warehouse": is_qinsi, "sort_order": sort_order,
        })


def downgrade() -> None:
    if "locations" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("locations")
