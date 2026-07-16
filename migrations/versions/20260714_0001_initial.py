"""initial receipt MVP schema"""
from alembic import op
from sqlalchemy.schema import CreateIndex, CreateTable, DropTable
from app.db import Base
from app import models  # noqa: F401

revision = "20260714_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in Base.metadata.sorted_tables:
        op.execute(CreateTable(table))
    for table in Base.metadata.sorted_tables:
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            op.execute(CreateIndex(index))


def downgrade() -> None:
    for table in reversed(Base.metadata.sorted_tables):
        op.execute(DropTable(table))
