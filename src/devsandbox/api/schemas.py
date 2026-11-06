"""Pydantic request/response models for the HTTP API (plan 10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..common.types import CheckSpec, ProjectMode, RepoSource, ResourceProfile
from ..reporting.models import ReportCounts


class ApiErrorBody(BaseModel):
    """One machine-readable error."""

    code: str
    message: str
    details: Any = None


class ApiErrorEnvelope(BaseModel):
    """Stable error envelope returned by every failing endpoint."""

    error: ApiErrorBody
    request_id: str | None = None


class SubmitProjectRequest(BaseModel):
    """JSON submission form; raw YAML bodies are also accepted."""

    spec_yaml: str = Field(min_length=1)
    repo: RepoSource | str | None = None


class ProjectSummary(BaseModel):
    """Project list/detail row."""

    id: str
    name: str
    mode: ProjectMode
    status: str
    spec_hash: str | None = None
    repo_source: str | None = None
    repo_path: str | None = None
    main_branch: str
    staging_branch: str
    active_plan_id: str | None = None
    counts: ReportCounts = Field(default_factory=ReportCounts)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ModeChangeRequest(BaseModel):
    """Explicit autonomous/supervised mode change."""

    mode: ProjectMode


class ApproveRequest(BaseModel):
    """Plan approval metadata."""

    approver: str = "user"


class RejectRequest(BaseModel):
    """Plan rejection metadata."""

    reason: str = "rejected by user"


class RetryRequest(BaseModel):
    """Manual retry request; budget changes require an explicit flag."""

    reset_budget: bool = False


class RetryResponse(BaseModel):
    """Result of a manual retry."""

    task_id: str
    status: str
    reset_budget: bool = False


class AttemptSummary(BaseModel):
    """One attempt of a task."""

    id: str
    task_id: str
    number: int
    status: str
    exit_status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    base_commit: str | None = None
    head_commit: str | None = None
    branch: str | None = None
    workspace_path: str | None = None
    container_id: str | None = None
    agent_name: str | None = None
    agent_log_path: str | None = None
    validation_status: str | None = None
    validation_summary: str = ""
    failure: dict[str, Any] | None = None
    token_usage: dict[str, Any] | None = None
    peak_memory_mb: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AttemptDetail(AttemptSummary):
    """Attempt details plus the full independent validation report."""

    validation_report: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    failure: dict[str, Any] | None = None


class TaskSummary(BaseModel):
    """Task list row including attempt/failure information."""

    id: str
    project_id: str
    plan_id: str
    key: str
    title: str
    status: str
    priority: int = 0
    attempt_count: int = 0
    max_retries: int = 0
    current_attempt_id: str | None = None
    blocked_reason: str | None = None
    failure: dict[str, Any] | None = None
    attempts: list[AttemptSummary] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TaskDetail(TaskSummary):
    """Full task detail used by the task page / CLI."""

    description: str = ""
    instructions: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    restricted_files: list[str] = Field(default_factory=list)
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    timeout_seconds: int = 1800
    retry_not_before: datetime | None = None
    result: dict[str, Any] | None = None


class EventRecord(BaseModel):
    """One audit event."""

    id: str
    project_id: str
    task_id: str | None = None
    attempt_id: str | None = None
    type: str
    from_state: str | None = None
    to_state: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime


class LogChunk(BaseModel):
    """Redacted, size-capped log content."""

    attempt_id: str
    stream: Literal["agent", "stdout", "stderr"]
    content: str
    lines: int
    truncated: bool
    path: str


class HostResourcesView(BaseModel):
    """Host metric sample used by ``/system/status``."""

    cpu_percent: float = 0.0
    cpu_count: int = 1
    memory_total_mb: float = 0.0
    memory_available_mb: float = 0.0
    memory_used_percent: float = 0.0
    disk_free_mb: int = 0
    docker_disk_free_mb: int | None = None
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    process_count: int = 0
    sampled_at: datetime | None = None


class EngineStatusView(BaseModel):
    """Observable engine state."""

    running: list[str] = Field(default_factory=list)
    ready_queue: list[str] = Field(default_factory=list)
    deferred: dict[str, str] = Field(default_factory=dict)
    pressure: str = "ok"
    budget_usage: dict[str, int] = Field(default_factory=dict)
    attempts_total: int = 0
    paused: bool = False
    lease_holder: str | None = None
    last_round_at: datetime | None = None


class SystemStatus(BaseModel):
    """Engine, host, budget, and API status."""

    version: str
    engine: EngineStatusView
    resources: HostResourcesView | None = None
    pressure: str = "unknown"
    budget_usage: dict[str, int] = Field(default_factory=dict)
    projects: int = 0
    api: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Liveness/readiness response."""

    status: Literal["ok", "ready", "not_ready"]
    checks: dict[str, bool] = Field(default_factory=dict)
    version: str = "0.1.0"


class SystemBootstrap(BaseModel):
    """Small helper used by CLI/status when no project is supplied."""

    projects: int = 0


__all__ = [
    "ApiErrorBody",
    "ApiErrorEnvelope",
    "ApproveRequest",
    "AttemptDetail",
    "AttemptSummary",
    "EngineStatusView",
    "EventRecord",
    "HealthResponse",
    "HostResourcesView",
    "LogChunk",
    "ModeChangeRequest",
    "ProjectSummary",
    "RejectRequest",
    "RetryRequest",
    "RetryResponse",
    "SubmitProjectRequest",
    "SystemStatus",
    "TaskDetail",
    "TaskSummary",
]
