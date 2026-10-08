"""Add idempotency and certificate integrity metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_02"
down_revision: str | None = "20261008_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.add_column(sa.Column("idempotency_key_hash", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("request_hash", sa.String(64), nullable=True))
        batch_op.create_index(
            "ux_certificate_jobs_idempotency_key_hash",
            ["idempotency_key_hash"],
            unique=True,
        )

    with op.batch_alter_table("certificate_records") as batch_op:
        batch_op.add_column(sa.Column("sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("certificate_records") as batch_op:
        batch_op.drop_column("sha256")

    with op.batch_alter_table("certificate_jobs") as batch_op:
        batch_op.drop_index("ux_certificate_jobs_idempotency_key_hash")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key_hash")
