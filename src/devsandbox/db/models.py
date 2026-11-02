"""SQLAlchemy 2.x async ORM models for DevSandbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonDict = dict[str, Any]


def _uuid_str() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    from ..common.time import utcnow

    return utcnow()


class Base(DeclarativeBase):
    """Declarative base for DevSandbox models."""


class ProjectModel(Base):
    """A DevSandbox project: one spec plus one repository."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="supervised")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="loaded")
    spec_yaml: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_source_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    main_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    staging_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    active_plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    settings_override_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlanModel(Base):
    """A versioned task graph for a project."""

    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_plans_project_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    generated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaskModel(Base):
    """A task node in a plan."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_project_status", "project_id", "status"),
        Index("ix_tasks_plan_id", "plan_id"),
        UniqueConstraint("plan_id", "key", name="uq_tasks_plan_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    success_criteria_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checks_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    expected_files_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    restricted_files_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resource_profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TaskDependencyModel(Base):
    """A task dependency edge (task depends on depends_on_task_id)."""

    __tablename__ = "task_dependencies"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    depends_on_task_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)


class AttemptModel(Base):
    """One execution attempt of a task."""

    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    validation_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    token_usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_attempts_task_id", "task_id"),)


class EventModel(Base):
    """Append-only audit event."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IntegrationModel(Base):
    """Integration of a validated change set into the staging branch."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    staging_commit_before: Mapped[str | None] = mapped_column(String(64), nullable=True)
    staging_commit_after: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conflict_files_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContextArtifactModel(Base):
    """A versioned context artifact."""

    __tablename__ = "context_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depends_on_paths_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CostEntryModel(Base):
    """Infra / inference cost ledger entry."""

    __tablename__ = "cost_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid_str, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_usd: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, default=0.0)
    units_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UsageCounterModel(Base):
    """Token / request usage counters per provider window."""

    __tablename__ = "usage_counters"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LeaseModel(Base):
    """Single-orchestrator lease guard."""

    __tablename__ = "leases"

    name: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    holder: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
