"""0005_orchestrator_retry_state

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01

Adds the orchestrator retry deadline used by the scheduler snapshot (plan 08).
A task in ``retry_wait`` becomes schedulable again once ``retry_not_before``
has passed.  ``integrations.conflict_files_json`` now stores a list of paths
(same JSON column type, so no structural change is required).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_project_status_retry",
        "tasks",
        ["project_id", "status", "retry_not_before"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_project_status_retry", table_name="tasks")
    op.drop_column("tasks", "retry_not_before")
