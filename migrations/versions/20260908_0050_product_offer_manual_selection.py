"""Manual online-offer selection per JAN (image/price/source taken as one bundle)"""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0050"
down_revision = "20260908_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "product_offer_manual_selections" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "product_offer_manual_selections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jan", sa.String(length=32), nullable=False),
        sa.Column("provider_code", sa.String(length=50), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("item_price", sa.Integer(), nullable=True),
        sa.Column("shipping_price", sa.Integer(), nullable=True),
        sa.Column("total_price", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="JPY"),
        sa.Column("source_offer_id", sa.Integer(), sa.ForeignKey("product_offers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_product_offer_manual_selection_jan", "product_offer_manual_selections", ["jan"], unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "product_offer_manual_selections" not in sa.inspect(bind).get_table_names():
        return
    op.drop_table("product_offer_manual_selections")
