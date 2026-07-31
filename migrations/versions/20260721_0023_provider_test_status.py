"""provider connection status details

Revision ID: 20260721_0023
Revises: 20260720_0022
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_0023"
down_revision = "20260720_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("platform_provider_states")}
    additions = (
        ("last_test_status", sa.String(30)),
        ("last_http_status", sa.Integer()),
        ("last_result_count", sa.Integer(), "0"),
    )
    with op.batch_alter_table("platform_provider_states") as batch:
        for name, column_type, *default in additions:
            if name not in columns:
                batch.add_column(
                    sa.Column(
                        name,
                        column_type,
                        nullable=False if default else True,
                        server_default=default[0] if default else None,
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("platform_provider_states")}
    with op.batch_alter_table("platform_provider_states") as batch:
        for name in ("last_result_count", "last_http_status", "last_test_status"):
            if name in columns:
                batch.drop_column(name)
