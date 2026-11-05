"""Stable report models rendered by the API, CLI, and report builder (plan 10).

The models are deliberately presentation-oriented: they contain no ORM rows or
paths that could leak host details, and every free-text field is expected to
have passed through :func:`devsandbox.common.redaction.redact` before it is
populated by :func:`devsandbox.reporting.builder.build_report`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportHeader(BaseModel):
    """Project / revision header for a report."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    name: str
    spec_hash: str | None = None
    mode: str
    status: str
    repo_source: str | None = None
    main_branch: str = "main"
    base_commit: str | None = None
    staging_branch: str = ""
    staging_head: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    generated_at: datetime


class ReportCounts(BaseModel):
    """Task counts by lifecycle state."""

    model_config = ConfigDict(frozen=True)

    total: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    cancelled: int = 0
    pending: int = 0
    ready: int = 0
    running: int = 0
    retry_wait: int = 0
    other: int = 0


class ReportCommit(BaseModel):
    """A commit associated with a task or attempt."""

    model_config = ConfigDict(frozen=True)

    sha: str
    subject: str = ""


class ReportCheckResult(BaseModel):
    """One validation check result inside an attempt."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str
    passed: bool = False
    required: bool = True
    duration_seconds: float = 0.0
    exit_code: int | None = None
    tests_total: int | None = None
    tests_failed: int | None = None
    tests_skipped: int | None = None
    tests_errors: int | None = None
    failure_kind: str | None = None
    summary: str = ""


class ReportFailure(BaseModel):
    """Failure diagnostics attached to a task or attempt."""

    model_config = ConfigDict(frozen=True)

    kind: str = "unknown"
    summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    log_tail_pointer: str | None = None


class ReportAttempt(BaseModel):
    """One execution attempt of a task."""

    model_config = ConfigDict(frozen=True)

    number: int
    status: str
    outcome: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    exit_status: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    commits: list[ReportCommit] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    checks: list[ReportCheckResult] = Field(default_factory=list)
    validation_status: str | None = None
    validation_summary: str = ""
    failure: ReportFailure | None = None
    token_usage: dict[str, Any] | None = None
    peak_memory_mb: int | None = None
    log_pointer: str | None = None


class ReportTask(BaseModel):
    """Per-task report section."""

    model_config = ConfigDict(frozen=True)

    id: str
    key: str
    title: str
    status: str
    priority: int = 0
    attempt_count: int = 0
    max_retries: int = 0
    duration_seconds: float = 0.0
    attempts: list[ReportAttempt] = Field(default_factory=list)
    commits: list[ReportCommit] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    checks: list[ReportCheckResult] = Field(default_factory=list)
    failure: ReportFailure | None = None
    blocked_by: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class ReportIntegration(BaseModel):
    """One staging integration record."""

    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    attempt_id: str
    status: str
    staging_before: str | None = None
    staging_after: str | None = None
    conflicting_files: list[str] = Field(default_factory=list)
    detail: str = ""
    at: datetime | None = None


class ReportTestAggregate(BaseModel):
    """Aggregated validation-check outcome across attempts."""

    model_config = ConfigDict(frozen=True)

    check: str
    passed: int = 0
    failed: int = 0
    timeout: int = 0
    error: int = 0
    skipped: int = 0
    total: int = 0


class ReportCosts(BaseModel):
    """Infra and inference cost ledgers, kept separate as required by 00 §8.10."""

    model_config = ConfigDict(frozen=True)

    infra_usd: float = 0.0
    inference_usd: float = 0.0
    total_usd: float = 0.0
    tokens: int = 0
    requests: int = 0
    budget_tokens_remaining: int | None = None
    budget_requests_remaining: int | None = None


class ReportResourceEvent(BaseModel):
    """Pressure, circuit-breaker, policy, timeout, or OOM event."""

    model_config = ConfigDict(frozen=True)

    type: str
    task_id: str | None = None
    attempt_id: str | None = None
    at: datetime | None = None
    detail: str = ""


class ReportOutstanding(BaseModel):
    """Unresolved work and warnings for the human reviewer."""

    model_config = ConfigDict(frozen=True)

    unresolved_failures: list[str] = Field(default_factory=list)
    blocked_tasks: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProjectReport(BaseModel):
    """Deterministic report for one project at a point in time."""

    model_config = ConfigDict(frozen=True)

    header: ReportHeader
    counts: ReportCounts
    tasks: list[ReportTask] = Field(default_factory=list)
    integrations: list[ReportIntegration] = Field(default_factory=list)
    test_results: list[ReportTestAggregate] = Field(default_factory=list)
    costs: ReportCosts = Field(default_factory=ReportCosts)
    resource_events: list[ReportResourceEvent] = Field(default_factory=list)
    outstanding: ReportOutstanding = Field(default_factory=ReportOutstanding)
    staging_instructions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ProjectReport",
    "ReportAttempt",
    "ReportCheckResult",
    "ReportCommit",
    "ReportCosts",
    "ReportCounts",
    "ReportFailure",
    "ReportHeader",
    "ReportIntegration",
    "ReportOutstanding",
    "ReportResourceEvent",
    "ReportTask",
    "ReportTestAggregate",
]
