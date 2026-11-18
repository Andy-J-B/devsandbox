"""0007_task_paused

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

Add a reversible per-task hold. Paused tasks keep their state-machine status
but are excluded from readiness promotion and the scheduler's ready set.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tasks", "paused")
