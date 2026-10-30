"""0001_initial

Revision ID: 0001
Revises:
Create Date: 2026-10-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("spec_yaml", sa.Text(), nullable=True),
        sa.Column("spec_hash", sa.String(length=64), nullable=True),
        sa.Column("spec_json", sa.Text(), nullable=True),
        sa.Column("repo_source_json", sa.Text(), nullable=True),
        sa.Column("repo_path", sa.String(length=1024), nullable=True),
        sa.Column("main_branch", sa.String(length=255), nullable=False),
        sa.Column("staging_branch", sa.String(length=255), nullable=False),
        sa.Column("active_plan_id", sa.String(length=64), nullable=True),
        sa.Column("settings_override_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generated_by", sa.String(length=255), nullable=True),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_plans_project"),
        sa.UniqueConstraint("project_id", "version", name="uq_plans_project_version"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("success_criteria_json", sa.JSON(), nullable=True),
        sa.Column("checks_json", sa.JSON(), nullable=True),
        sa.Column("expected_files_json", sa.JSON(), nullable=True),
        sa.Column("restricted_files_json", sa.JSON(), nullable=True),
        sa.Column("resource_profile_json", sa.JSON(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("current_attempt_id", sa.String(length=64), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("failure_json", sa.JSON(), nullable=True),
        sa.Column("blocked_reason", sa.String(length=1024), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_tasks_plan"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_tasks_project"),
        sa.UniqueConstraint("plan_id", "key", name="uq_tasks_plan_key"),
    )
    op.create_index("ix_tasks_project_status", "tasks", ["project_id", "status"])
    op.create_index("ix_tasks_plan_id", "tasks", ["plan_id"])
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("depends_on_task_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_task_dependencies_task"),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"], ["tasks.id"], name="fk_task_dependencies_dep"
        ),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id"),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workspace_path", sa.String(length=1024), nullable=True),
        sa.Column("branch", sa.String(length=512), nullable=True),
        sa.Column("base_commit", sa.String(length=64), nullable=True),
        sa.Column("head_commit", sa.String(length=64), nullable=True),
        sa.Column("container_id", sa.String(length=64), nullable=True),
        sa.Column("agent_name", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_status", sa.String(length=32), nullable=True),
        sa.Column("agent_log_path", sa.String(length=1024), nullable=True),
        sa.Column("validation_report_json", sa.JSON(), nullable=True),
        sa.Column("failure_json", sa.JSON(), nullable=True),
        sa.Column("token_usage_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_attempts_task"),
    )
    op.create_index("ix_attempts_task_id", "attempts", ["task_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_project_at", "events", ["project_id", "at"])
    op.create_table(
        "integrations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("staging_commit_before", sa.String(length=64), nullable=True),
        sa.Column("staging_commit_after", sa.String(length=64), nullable=True),
        sa.Column("conflict_files_json", sa.JSON(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "context_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cost_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("amount_usd", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("units_json", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "usage_counters",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "provider", "window_start"),
    )
    op.create_table(
        "leases",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("leases")
    op.drop_table("usage_counters")
    op.drop_table("cost_entries")
    op.drop_table("context_artifacts")
    op.drop_table("integrations")
    op.drop_table("events")
    op.drop_table("attempts")
    op.drop_table("task_dependencies")
    op.drop_table("tasks")
    op.drop_table("plans")
    op.drop_table("projects")
