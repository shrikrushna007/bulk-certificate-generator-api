"""Add recipient leases for distributed processing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_03"
down_revision: str | None = "20261008_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.add_column(sa.Column("last_dispatched_at", sa.DateTime(timezone=True)))
        batch_op.create_index(
            "ix_certificate_jobs_dispatch",
            ["status", "last_dispatched_at"],
            unique=False,
        )

    with op.batch_alter_table("certificate_records") as batch_op:
        batch_op.add_column(sa.Column("claim_token", sa.String(36), nullable=True))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index(
            "ix_certificate_records_claimable",
            ["job_id", "status", "lease_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("certificate_records") as batch_op:
        batch_op.drop_index("ix_certificate_records_claimable")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("claim_token")

    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.drop_index("ix_certificate_jobs_dispatch")
        batch_op.drop_column("last_dispatched_at")
