"""0002_context_artifact_metadata

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31

Adds content hashing, generator provenance, and staleness dependency paths to
context artifacts (plan 05).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_artifacts", sa.Column("content_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "context_artifacts", sa.Column("generated_by", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "context_artifacts",
        sa.Column("depends_on_paths_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("context_artifacts", "depends_on_paths_json")
    op.drop_column("context_artifacts", "generated_by")
    op.drop_column("context_artifacts", "content_hash")
