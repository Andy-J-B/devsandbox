"""0004_attempt_resource_peaks

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01

Adds observed peak resource usage to attempts (plan 07).  The values are
recorded by the orchestrator after a sandbox finishes and are used to tune
future resource requests.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("peak_memory_mb", sa.Integer(), nullable=True))
    op.add_column("attempts", sa.Column("peak_cpu_percent", sa.Numeric(6, 2), nullable=True))
    op.create_index("ix_attempts_status", "attempts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_attempts_status", table_name="attempts")
    op.drop_column("attempts", "peak_cpu_percent")
    op.drop_column("attempts", "peak_memory_mb")
