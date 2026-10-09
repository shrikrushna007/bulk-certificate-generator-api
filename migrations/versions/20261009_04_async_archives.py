"""Add asynchronous archive state and leases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_04"
down_revision: str | None = "20261008_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archive_status",
                sa.Enum(
                    "NOT_REQUESTED",
                    "PENDING",
                    "PROCESSING",
                    "COMPLETED",
                    "FAILED",
                    name="archivestatus",
                    native_enum=False,
                    length=32,
                ),
                nullable=False,
                server_default="NOT_REQUESTED",
            )
        )
        batch_op.add_column(sa.Column("archive_path", sa.String(500)))
        batch_op.add_column(sa.Column("archive_error", sa.Text()))
        batch_op.add_column(sa.Column("archive_claim_token", sa.String(36)))
        batch_op.add_column(sa.Column("archive_lease_expires_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.drop_column("archive_lease_expires_at")
        batch_op.drop_column("archive_claim_token")
        batch_op.drop_column("archive_error")
        batch_op.drop_column("archive_path")
        batch_op.drop_column("archive_status")
