"""Add archive progress and timing metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_05"
down_revision: str | None = "20261009_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_total_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "archive_processed_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("archive_started_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("archive_completed_at", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint(
            "ck_jobs_archive_processed_nonnegative",
            "archive_processed_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_jobs_archive_total_nonnegative",
            "archive_total_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_jobs_archive_counts_valid",
            "archive_processed_count <= archive_total_count",
        )


def downgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.drop_constraint("ck_jobs_archive_counts_valid", type_="check")
        batch_op.drop_constraint("ck_jobs_archive_total_nonnegative", type_="check")
        batch_op.drop_constraint("ck_jobs_archive_processed_nonnegative", type_="check")
        batch_op.drop_column("archive_completed_at")
        batch_op.drop_column("archive_started_at")
        batch_op.drop_column("archive_processed_count")
        batch_op.drop_column("archive_total_count")
