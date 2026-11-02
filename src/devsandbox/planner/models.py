"""Planner data models: task drafts, proposed/accepted plans, graph issues.

The types here are deliberately free of database and network dependencies so
they can be used by pure validation code, planner implementations, the API, and
tests alike.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..common.types import CheckSpec, ResourceProfile, TokenUsage

PLANNER_VERSION = "1.0"


class GraphSeverity(StrEnum):
    """Severity of a graph validation issue."""

    ERROR = "error"
    WARNING = "warning"


class GraphIssue(BaseModel):
    """A structural issue detected in a task graph."""

    model_config = ConfigDict(frozen=True)

    code: str = "graph"
    severity: GraphSeverity = GraphSeverity.ERROR
    task_key: str | None = None
    issue: str
    path: list[str] = Field(default_factory=list)

    @property
    def is_error(self) -> bool:
        """Return whether this issue blocks plan activation."""
        return self.severity == GraphSeverity.ERROR


class TaskDraft(BaseModel):
    """A task draft produced by a planner before it is persisted."""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    description: str = ""
    instructions: str = ""
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 50
    success_criteria: list[str] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    restricted_files: list[str] = Field(default_factory=list)
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    timeout_seconds: int = 1800
    max_retries: int = 0


class ProposedPlan(BaseModel):
    """A proposed task graph produced by a planner implementation."""

    tasks: list[TaskDraft] = Field(default_factory=list)
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    generated_by: str = "unknown"
    planner_version: str = PLANNER_VERSION
    source_revision: str = ""


class PlanStatus(StrEnum):
    """Lifecycle states of a persisted plan."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Plan(BaseModel):
    """A persisted, versioned task graph."""

    id: str
    project_id: str = ""
    version: int
    status: PlanStatus = PlanStatus.PROPOSED
    tasks: list[TaskDraft] = Field(default_factory=list)
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    generated_by: str = ""
    planner_version: str = PLANNER_VERSION
    source_revision: str = ""
    issues: list[GraphIssue] = Field(default_factory=list)
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None

    @property
    def errors(self) -> list[GraphIssue]:
        """Return only the blocking issues."""
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[GraphIssue]:
        """Return only the non-blocking issues."""
        return [issue for issue in self.issues if not issue.is_error]


class FailureRecord(BaseModel):
    """Record of a failed task attempt, used for replanning."""

    task_id: str
    task_key: str | None = None
    attempt_id: str | None = None
    summary: str | None = None
    error: str = ""
    validation_summary: str = ""
    files_changed: list[str] = Field(default_factory=list)


class ValidationLimits(BaseModel):
    """Limits and policy ceilings applied while validating a task graph."""

    max_tasks: int = 100
    max_depth: int = 12
    max_timeout_seconds: int = 7200
    max_retries: int = 2
    min_priority: int = 0
    max_priority: int = 100
    max_expected_files: int = 200
    require_instructions: bool = True
    require_success_criteria: bool = True
    require_required_check: bool = True
    baseline_restricted_paths: list[str] = Field(default_factory=list)
    policy_max_resources: ResourceProfile | None = None


class TaskDiff(BaseModel):
    """Difference for a single task key between two plans."""

    key: str
    change: Literal["added", "removed", "changed"]
    fields: list[str] = Field(default_factory=list)


class PlanDiff(BaseModel):
    """Human-readable difference between two plans."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    task_diffs: list[TaskDiff] = Field(default_factory=list)
    rationale_changed: bool = False
    version_from: int | None = None
    version_to: int | None = None


class PlanPatch(BaseModel):
    """User edit applied to a plan before approval.

    Supplying ``tasks`` replaces the whole graph.  Otherwise ``upsert_tasks``
    are inserted/updated by key and ``remove_keys`` are deleted.
    """

    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskDraft] | None = None
    upsert_tasks: list[TaskDraft] = Field(default_factory=list)
    remove_keys: list[str] = Field(default_factory=list)
    rationale: str | None = None
    assumptions: list[str] | None = None
    risks: list[str] | None = None


class LLMMessage(BaseModel):
    """One chat message sent to an LLM."""

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    """A structured request to an OpenAI-compatible LLM."""

    model_config = ConfigDict(frozen=True)

    model: str
    messages: list[LLMMessage]
    max_tokens: int | None = None
    temperature: float = 0.0


class LLMResponse(BaseModel):
    """A completion returned by an :class:`LLMClient`."""

    model_config = ConfigDict(frozen=True)

    content: str
    token_usage: TokenUsage | None = None


__all__ = [
    "PLANNER_VERSION",
    "FailureRecord",
    "GraphIssue",
    "GraphSeverity",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "Plan",
    "PlanDiff",
    "PlanPatch",
    "PlanStatus",
    "ProposedPlan",
    "TaskDiff",
    "TaskDraft",
    "ValidationLimits",
]
