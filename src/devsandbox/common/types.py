"""Shared domain types: resource profiles, network policies, checks, repo sources, task specs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import AttemptId, PlanId, ProjectId


class TaskStatus(StrEnum):
    """Lifecycle states of a task."""

    PENDING = "pending"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    VALIDATING = "validating"
    VALIDATED = "validated"
    INTEGRATING = "integrating"
    COMPLETED = "completed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ProjectStatus(StrEnum):
    """Lifecycle states of a project."""

    LOADED = "loaded"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProjectMode(StrEnum):
    """Operating mode of a project."""

    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class AttemptOutcome(StrEnum):
    """Outcome of a single agent attempt."""

    EXITED_OK = "exited_ok"
    EXITED_ERROR = "exited_error"
    TIMEOUT = "timeout"
    OOM = "oom"
    CRASHED = "crashed"
    SANDBOX_ERROR = "sandbox_error"


class TaskOutcome(StrEnum):
    """Aggregated outcome note produced after a task attempt cycle."""

    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"


class ResourceProfile(BaseModel):
    """Compute / memory / disk / pid resource limits for a sandbox."""

    model_config = ConfigDict(frozen=True)

    cpu_cores: float = 1.0
    memory_mb: int = 2048
    disk_mb: int = 4096
    pids_limit: int = 512


class NetworkPolicy(BaseModel):
    """Network policy for a sandbox attempt."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["none", "allowlist", "full"] = "none"
    allowed_hosts: list[str] = Field(default_factory=list)


class CheckSpec(BaseModel):
    """A validation check executed inside a sandbox."""

    model_config = ConfigDict(frozen=True)

    name: str
    run: str
    required: bool = True
    timeout_seconds: int = 300


class RepoSource(BaseModel):
    """Source of a project repository (local path xor remote URL)."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    url: str | None = None
    ref: str = "main"

    @model_validator(mode="after")
    def _check_source(self) -> RepoSource:
        if (self.path is None) == (self.url is None):
            raise ValueError("exactly one of 'repo.path' or 'repo.url' must be set")
        return self


class RepoHandle(BaseModel):
    """A concrete repository handle bound to a project."""

    project_id: str
    path: str
    url: str | None = None
    ref: str = "main"
    main_branch: str = "main"
    staging_branch: str = ""


class TokenUsage(BaseModel):
    """Token / cost accounting for a single agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = "opencode"


class ContextBundle(BaseModel):
    """Context assembled for a task execution."""

    task_id: str
    task: TaskSpec
    context_files: list[str] = Field(default_factory=list)
    instructions: str = ""


class RepoProfile(BaseModel):
    """Aggregated repository profile produced by the context manager."""

    name: str = "unknown"
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    estimated_files: int = 0


class ProjectContext(BaseModel):
    """Goal / requirement / constraint context for planning."""

    goals: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class TaskSpec(BaseModel):
    """A single task within a plan."""

    id: str
    title: str
    description: str = ""
    instructions: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    restricted_files: list[str] = Field(default_factory=list)
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    timeout_seconds: int = 1800
    max_retries: int = 0


class ModelConfig(BaseModel):
    """Model selection / budget for an agent run."""

    name: str = "opencode"
    provider: str | None = None
    max_tokens: int | None = None
    max_requests: int | None = None


__all__ = [
    "AttemptId",
    "AttemptOutcome",
    "CheckSpec",
    "ContextBundle",
    "ModelConfig",
    "NetworkPolicy",
    "PlanId",
    "ProjectContext",
    "ProjectId",
    "ProjectMode",
    "ProjectStatus",
    "RepoHandle",
    "RepoProfile",
    "RepoSource",
    "ResourceProfile",
    "TaskOutcome",
    "TaskSpec",
    "TaskStatus",
    "TokenUsage",
]
