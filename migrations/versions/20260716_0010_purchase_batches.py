"""purchase batches and initial locations"""
from alembic import op
import sqlalchemy as sa


revision = "20260716_0010"
down_revision = "20260715_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "purchase_batches" not in tables:
        op.create_table(
            "purchase_batches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_no", sa.String(40), nullable=False),
            sa.Column("receipt_id", sa.Integer(), sa.ForeignKey("receipts.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("gpt_batch_id", sa.Integer(), sa.ForeignKey("receipt_batches.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("purchased_at", sa.DateTime(timezone=True)),
            sa.Column("store_name", sa.String(255)),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(30), server_default="confirmed", nullable=False),
            sa.Column("default_initial_location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("default_qinsi_warehouse_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("batch_no", name="uq_purchase_batches_batch_no"),
            sa.UniqueConstraint("receipt_id", name="uq_purchase_batches_receipt_id"),
            sa.CheckConstraint(
                "status IN ('confirmed','pending_qinsi_submission','cancelled')",
                name="ck_purchase_batches_status",
            ),
        )
        op.create_index("ix_purchase_batches_receipt_id", "purchase_batches", ["receipt_id"])
        op.create_index("ix_purchase_batches_gpt_batch_id", "purchase_batches", ["gpt_batch_id"])
        op.create_index("ix_purchase_batches_status", "purchase_batches", ["status"])
    tables = set(sa.inspect(bind).get_table_names())
    if "purchase_batch_items" not in tables:
        op.create_table(
            "purchase_batch_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("purchase_batch_id", sa.Integer(), sa.ForeignKey("purchase_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Integer()),
            sa.Column("discount_amount", sa.Integer(), server_default="0", nullable=False),
            sa.Column("actual_line_amount", sa.Integer()),
            sa.Column("initial_location_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("qinsi_target_warehouse_id", sa.Integer(), sa.ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("target_warehouse_overridden", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
            sa.UniqueConstraint("receipt_item_id", name="uq_purchase_batch_items_receipt_item_id"),
            sa.CheckConstraint("quantity > 0", name="ck_purchase_batch_items_quantity_positive"),
        )
        op.create_index("ix_purchase_batch_items_purchase_batch_id", "purchase_batch_items", ["purchase_batch_id"])
        op.create_index("ix_purchase_batch_items_product_id", "purchase_batch_items", ["product_id"])
        op.create_index("ix_purchase_batch_items_receipt_item_id", "purchase_batch_items", ["receipt_item_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "purchase_batch_items" in tables:
        op.drop_table("purchase_batch_items")
    if "purchase_batches" in tables:
        op.drop_table("purchase_batches")
