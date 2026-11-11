"""0006_task_allow_no_changes

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10

Persist ``TaskSpec.allow_no_changes`` so verification-only tasks (for example
the template planner's ``final-verification`` task) can complete without
inventing a code change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "allow_no_changes",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "allow_no_changes")
