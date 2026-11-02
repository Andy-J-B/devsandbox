"""0003_plan_metadata

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01

Adds planner provenance and validation issues to plans (plan 06).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("planner_version", sa.String(length=32), nullable=True))
    op.add_column("plans", sa.Column("source_revision", sa.String(length=64), nullable=True))
    op.add_column("plans", sa.Column("assumptions_json", sa.JSON(), nullable=True))
    op.add_column("plans", sa.Column("risks_json", sa.JSON(), nullable=True))
    op.add_column("plans", sa.Column("issues_json", sa.JSON(), nullable=True))
    op.add_column("plans", sa.Column("rejection_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "rejection_reason")
    op.drop_column("plans", "issues_json")
    op.drop_column("plans", "risks_json")
    op.drop_column("plans", "assumptions_json")
    op.drop_column("plans", "source_revision")
    op.drop_column("plans", "planner_version")
