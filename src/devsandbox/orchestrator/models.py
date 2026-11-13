"""Orchestrator data models and configuration (plan 08)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common.types import ResourceProfile


class OrchestratorConfig(BaseModel):
    """Orchestrator-local tuning.

    These values deliberately live outside the project spec: neither a planner
    nor an agent can change concurrency, retry budgets, or circuit breakers.
    Values that overlap with plan-01 settings are derived by
    :meth:`from_settings`; the rest are safe local defaults.
    """

    model_config = ConfigDict(frozen=True)

    # Loop / lease ---------------------------------------------------------
    lease_name: str = "orchestrator"
    lease_ttl_seconds: int = 60
    tick_seconds: float = 1.0
    idle_sleep_seconds: float = 0.1
    shutdown_timeout_seconds: float = 30.0
    # Retry transient database disconnects (for example a PostgreSQL restart)
    # instead of terminating the engine loop.
    db_reconnect_base_seconds: float = 1.0
    db_reconnect_max_seconds: float = 30.0

    # Retries / circuit breakers ------------------------------------------
    max_infra_failures: int = 3
    max_total_attempts: int = 100
    max_replans: int = 2
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 300.0
    backoff_jitter_ratio: float = 0.2
    repeat_failure_threshold: int = 2

    # Retention / cleanup --------------------------------------------------
    keep_workspace_on_failure: bool = True
    workspace_retention_hours: float = 72.0

    # Cost ledger (local Docker is free by default) -----------------------
    infra_cost_per_second_usd: float = 0.0
    inference_cost_per_1k_tokens_usd: float = 0.0
    usage_window_seconds: int = 3600

    # Policy / request defaults -------------------------------------------
    default_resources: ResourceProfile = Field(default_factory=ResourceProfile)
    validation_resources: ResourceProfile = Field(
        default_factory=lambda: ResourceProfile(cpu_cores=1.0, memory_mb=2048, disk_mb=4096)
    )

    @model_validator(mode="after")
    def _validate(self) -> OrchestratorConfig:
        if self.lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds must be >= 1")
        if self.tick_seconds <= 0 or self.idle_sleep_seconds <= 0:
            raise ValueError("sleep intervals must be > 0")
        if self.db_reconnect_base_seconds < 0 or self.db_reconnect_max_seconds < 0:
            raise ValueError("database reconnect backoff must be >= 0")
        if self.db_reconnect_max_seconds < self.db_reconnect_base_seconds:
            raise ValueError("db_reconnect_max_seconds must be >= db_reconnect_base_seconds")
        if self.max_infra_failures < 0 or self.max_total_attempts < 1:
            raise ValueError("circuit breaker values must be positive")
        if self.max_replans < 0:
            raise ValueError("max_replans must be >= 0")
        if self.backoff_base_seconds < 0 or self.backoff_max_seconds < 0:
            raise ValueError("backoff values must be >= 0")
        if not 0.0 <= self.backoff_jitter_ratio <= 1.0:
            raise ValueError("backoff_jitter_ratio must be between 0 and 1")
        if self.repeat_failure_threshold < 2:
            raise ValueError("repeat_failure_threshold must be >= 2")
        if self.workspace_retention_hours < 0:
            raise ValueError("workspace_retention_hours must be >= 0")
        if self.usage_window_seconds < 1:
            raise ValueError("usage_window_seconds must be >= 1")
        return self

    @classmethod
    def from_settings(
        cls,
        settings: object | None = None,
        **overrides: object,
    ) -> OrchestratorConfig:
        """Derive orchestrator defaults from plan-01 settings.

        Unknown/missing settings attributes fall back to the model defaults so
        plan 08 stays usable with a partially configured environment.
        """
        values: dict[str, object] = {}
        retries = getattr(settings, "retries", None)
        if retries is not None:
            values["backoff_base_seconds"] = getattr(
                retries, "backoff_base_seconds", cls.model_fields["backoff_base_seconds"].default
            )
            values["backoff_max_seconds"] = getattr(
                retries, "backoff_max_seconds", cls.model_fields["backoff_max_seconds"].default
            )
        planner = getattr(settings, "planner", None)
        if planner is not None:
            values["max_replans"] = getattr(
                planner, "max_replans", cls.model_fields["max_replans"].default
            )
        values.update(overrides)
        return cls.model_validate(values)


class EngineStatus(BaseModel):
    """Point-in-time orchestrator status (observability)."""

    model_config = ConfigDict(frozen=True)

    running: list[str] = Field(default_factory=list)
    ready_queue: list[str] = Field(default_factory=list)
    deferred: dict[str, str] = Field(default_factory=dict)
    pressure: str = "ok"
    budget_usage: dict[str, int] = Field(default_factory=dict)
    attempts_total: int = 0
    paused: bool = False
    lease_holder: str | None = None
    last_round_at: datetime | None = None


class AttemptStage(StrEnum):
    """Persisted stage of an attempt; drives crash recovery."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    VALIDATING = "validating"
    VALIDATED = "validated"
    INTEGRATING = "integrating"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class RecoveryActionKind(StrEnum):
    """Action chosen by the recovery decision table."""

    REVERT_READY = "revert_ready"
    KILL_AND_RETRY = "kill_and_retry"
    RETRY_INFRA = "retry_infra"
    RESUME_COLLECT = "resume_collect"
    RERUN_VALIDATION = "rerun_validation"
    RESUME_VALIDATION_DECISION = "resume_validation_decision"
    RESUME_INTEGRATION = "resume_integration"
    RESET_AND_RETRY = "reset_and_retry"
    MARK_COMPLETED = "mark_completed"
    NONE = "none"


class RecoveryAction(BaseModel):
    """One recovery decision for a task/attempt."""

    kind: RecoveryActionKind
    task_id: str
    attempt_id: str | None = None
    detail: str = ""


class RecoveryReport(BaseModel):
    """Summary emitted at the end of a recovery pass."""

    actions: list[RecoveryAction] = Field(default_factory=list)
    containers_destroyed: list[str] = Field(default_factory=list)
    workspaces_removed: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def summary(self) -> dict[str, int]:
        """Return action counts keyed by action kind."""
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.kind.value] = counts.get(action.kind.value, 0) + 1
        return counts


__all__ = [
    "AttemptStage",
    "EngineStatus",
    "OrchestratorConfig",
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryReport",
]
