"""Create certificate job and record tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "certificate_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "PROCESSING",
                "COMPLETED",
                "PARTIALLY_COMPLETED",
                "FAILED",
                name="jobstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("certificate_title", sa.String(length=200), nullable=False),
        sa.Column("issuing_organization", sa.String(length=200), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("failure_count >= 0", name="ck_jobs_failure_nonnegative"),
        sa.CheckConstraint(
            "success_count + failure_count <= total_count", name="ck_jobs_counts_valid"
        ),
        sa.CheckConstraint("success_count >= 0", name="ck_jobs_success_nonnegative"),
        sa.CheckConstraint("total_count >= 1", name="ck_jobs_total_positive"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_certificate_jobs_created_at", "certificate_jobs", ["created_at"])
    op.create_index("ix_certificate_jobs_status", "certificate_jobs", ["status"])
    op.create_table(
        "certificate_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_index", sa.Integer(), nullable=False),
        sa.Column("recipient_name", sa.String(length=200), nullable=True),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "COMPLETED",
                "FAILED",
                name="certificatestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["certificate_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "recipient_index", name="uq_certificate_job_recipient_index"),
    )
    op.create_index("ix_certificate_records_job_id", "certificate_records", ["job_id"])
    op.create_index(
        "ix_certificate_records_job_status", "certificate_records", ["job_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_certificate_records_job_status", table_name="certificate_records")
    op.drop_index("ix_certificate_records_job_id", table_name="certificate_records")
    op.drop_table("certificate_records")
    op.drop_index("ix_certificate_jobs_status", table_name="certificate_jobs")
    op.drop_index("ix_certificate_jobs_created_at", table_name="certificate_jobs")
    op.drop_table("certificate_jobs")
