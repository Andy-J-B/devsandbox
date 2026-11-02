"""Shared domain types: resource profiles, network policies, checks, repo sources, task specs."""

from __future__ import annotations

from datetime import datetime
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
    path: str  # bare "canonical" repo path on disk
    url: str | None = None
    ref: str = "main"
    main_branch: str = "main"
    staging_branch: str = ""
    base_commit: str | None = None  # resolved base (main / staging head) commit sha


class TokenUsage(BaseModel):
    """Token / cost accounting for a single agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = "opencode"


class CommandConfidence(StrEnum):
    """Confidence attached to a detected build/test/lint command."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DetectedCommand(BaseModel):
    """A command discovered by a repository detector.

    ``source`` records *where* the command came from (for example
    ``package.json scripts.test``) and ``confidence`` records how certain the
    detector is.  Commands are never executed during analysis.
    """

    model_config = ConfigDict(frozen=True)

    command: str
    source: str
    confidence: CommandConfidence = CommandConfidence.MEDIUM


class ContextBundle(BaseModel):
    """Context assembled for a task execution.

    Context is revision-stamped so consumers can detect a stale bundle.  The
    ``truncation_report`` lists sections/files that did not fit the configured
    budget (it is always deterministic for the same inputs).
    """

    task_id: str
    task: TaskSpec
    context_files: list[str] = Field(default_factory=list)
    instructions: str = ""
    revision: str = ""
    built_at: datetime | None = None
    summary: str = ""
    conventions: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    file_contents: dict[str, str] = Field(default_factory=dict)
    truncation_report: list[str] = Field(default_factory=list)


class RepoProfile(BaseModel):
    """Aggregated repository profile produced by the context manager.

    The legacy fields (``languages``, ``test_commands``, ...) remain for
    backwards compatibility with the planner.  The richer fields carry the
    full plan 05 analysis, including command provenance/confidence.
    """

    name: str = "unknown"
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    estimated_files: int = 0

    revision: str = ""
    file_count: int = 0
    total_loc_estimate: int = 0
    tree_summary: list[str] = Field(default_factory=list)
    primary_language: str | None = None
    language_share: dict[str, float] = Field(default_factory=dict)
    package_managers: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    build_commands: list[str] = Field(default_factory=list)
    lint_commands: list[str] = Field(default_factory=list)
    build_command_specs: list[DetectedCommand] = Field(default_factory=list)
    test_command_specs: list[DetectedCommand] = Field(default_factory=list)
    lint_command_specs: list[DetectedCommand] = Field(default_factory=list)
    spec_commands: list[DetectedCommand] = Field(default_factory=list)
    effective_test_commands: list[str] = Field(default_factory=list)
    test_layout: list[str] = Field(default_factory=list)
    ci_files: list[str] = Field(default_factory=list)
    conventions: list[str] = Field(default_factory=list)
    docs_found: list[str] = Field(default_factory=list)
    readme_excerpt: str = ""
    environment_hints: dict[str, str] = Field(default_factory=dict)
    suggested_validation_image: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TaskOutcomeNote(BaseModel):
    """Structured outcome recorded after a task attempt cycle.

    ``decisions``/``agent_summary`` come from the agent's self-report and are
    therefore untrusted; completion is always based on independent validation.
    """

    task_id: str
    outcome: TaskOutcome = TaskOutcome.SUCCESS
    revision: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commit_ids: list[str] = Field(default_factory=list)
    validation_summary: str = ""
    decisions: list[str] = Field(default_factory=list)
    failure_learnings: list[str] = Field(default_factory=list)
    agent_summary: str = ""
    untrusted: bool = True
    recorded_at: datetime | None = None


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
    """Model selection / budget for an agent run.

    ``provider`` and ``model`` identify the model in OpenCode's
    ``provider/model`` form.  ``name`` is kept as a backwards-compatible
    fallback (and as the runtime label); when ``model`` is omitted the
    adapter uses ``name`` as the model id.
    """

    name: str = "opencode"
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    secret_ref: str | None = None
    max_tokens: int | None = None
    max_requests: int | None = None

    @property
    def model_id(self) -> str:
        """Return the resolved model id (falls back to ``name``)."""
        return self.model or self.name

    @property
    def provider_id(self) -> str:
        """Return the resolved provider id (falls back to ``opencode``)."""
        return self.provider or "opencode"

    @property
    def reference(self) -> str:
        """Return OpenCode's ``provider/model`` reference."""
        return f"{self.provider_id}/{self.model_id}"


__all__ = [
    "AttemptId",
    "AttemptOutcome",
    "CheckSpec",
    "CommandConfidence",
    "ContextBundle",
    "DetectedCommand",
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
    "TaskOutcomeNote",
    "TaskSpec",
    "TaskStatus",
    "TokenUsage",
]
