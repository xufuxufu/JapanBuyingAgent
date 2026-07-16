"""GPT recognition jobs and schema 1.1 batch import audit"""
from alembic import op
import sqlalchemy as sa


revision = "20260715_0006"
down_revision = "20260715_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "zip_package_jobs" in tables:
        columns = {column["name"] for column in inspector.get_columns("zip_package_jobs")}
        additions = [
            sa.Column("gpt_status", sa.String(30), server_default="zip_ready", nullable=False),
            sa.Column("gpt_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("json_imported_at", sa.DateTime(timezone=True), nullable=True),
        ]
        missing = [column for column in additions if column.name not in columns]
        if missing:
            with op.batch_alter_table("zip_package_jobs") as batch:
                for column in missing:
                    batch.add_column(column)
        op.execute("UPDATE zip_package_jobs SET gpt_status=CASE WHEN download_count > 0 THEN 'zip_downloaded' ELSE 'zip_ready' END WHERE gpt_status='zip_ready'")

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "ai_recognition_runs" not in tables:
        op.create_table(
            "ai_recognition_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("receipt_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("image_id", sa.Integer(), sa.ForeignKey("receipt_images.id", ondelete="SET NULL"), nullable=True),
            sa.Column("zip_job_id", sa.Integer(), sa.ForeignKey("zip_package_jobs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("provider", sa.String(50), nullable=False),
            sa.Column("model_name", sa.String(100), nullable=True),
            sa.Column("prompt_version", sa.String(50), nullable=True),
            sa.Column("raw_response_json", sa.Text(), nullable=False),
            sa.Column("normalized_json", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ai_recognition_runs_zip_job_id", "ai_recognition_runs", ["zip_job_id"])
    else:
        columns = {column["name"] for column in inspector.get_columns("ai_recognition_runs")}
        foreign_columns = {tuple(item.get("constrained_columns") or ()) for item in inspector.get_foreign_keys("ai_recognition_runs")}
        indexes = {item["name"] for item in inspector.get_indexes("ai_recognition_runs")}
        with op.batch_alter_table("ai_recognition_runs") as batch:
            if "zip_job_id" not in columns:
                batch.add_column(sa.Column("zip_job_id", sa.Integer(), nullable=True))
            if ("zip_job_id",) not in foreign_columns:
                batch.create_foreign_key("fk_ai_recognition_runs_zip_job", "zip_package_jobs", ["zip_job_id"], ["id"], ondelete="SET NULL")
            if "ix_ai_recognition_runs_zip_job_id" not in indexes:
                batch.create_index("ix_ai_recognition_runs_zip_job_id", ["zip_job_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "ai_recognition_runs" in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("ai_recognition_runs")}
        if "zip_job_id" in columns:
            with op.batch_alter_table("ai_recognition_runs") as batch:
                batch.drop_column("zip_job_id")
    if "zip_package_jobs" in tables:
        columns = {column["name"] for column in sa.inspect(bind).get_columns("zip_package_jobs")}
        with op.batch_alter_table("zip_package_jobs") as batch:
            for name in ("json_imported_at", "gpt_sent_at", "gpt_status"):
                if name in columns:
                    batch.drop_column(name)
