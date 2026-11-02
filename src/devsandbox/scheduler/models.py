"""Scheduler data models.

Everything in this module is plain Pydantic data: the scheduler decision logic
is a pure function of these models and performs no I/O.  The models are frozen
so a snapshot cannot be mutated while a decision round is in progress.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common.types import ProjectMode, ResourceProfile


class PressureState(StrEnum):
    """Host resource pressure level."""

    OK = "ok"
    ELEVATED = "elevated"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Return a comparable rank (higher is more severe)."""
        return _PRESSURE_RANK[self]


_PRESSURE_RANK: dict[PressureState, int] = {
    PressureState.OK: 0,
    PressureState.ELEVATED: 1,
    PressureState.CRITICAL: 2,
}


class HostResources(BaseModel):
    """A point-in-time sample of host resources plus committed reservations.

    ``available_*`` properties follow plan 07: the usable amount is
    ``min(measured available, total - committed)``.  The scheduler additionally
    applies configured headroom on top of the value returned here.
    """

    model_config = ConfigDict(frozen=True)

    cpu_percent: float = 0.0
    cpu_count: int = 1
    memory_total_mb: float = 0.0
    memory_available_mb: float = 0.0
    disk_free_mb: int = 0
    docker_disk_free_mb: int | None = None
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    process_count: int = 0
    committed_cpu_cores: float = 0.0
    committed_memory_mb: float = 0.0
    committed_disk_mb: int = 0
    sampled_at: datetime | None = None

    @property
    def memory_used_percent(self) -> float:
        """Return the measured memory utilization percentage."""
        if self.memory_total_mb <= 0:
            return 0.0
        used = max(0.0, self.memory_total_mb - self.memory_available_mb)
        return min(100.0, used / self.memory_total_mb * 100.0)

    @property
    def measured_cpu_available_cores(self) -> float:
        """Return CPU cores currently idle according to the last measurement."""
        if self.cpu_count <= 0:
            return 0.0
        return max(0.0, self.cpu_count * (1.0 - self.cpu_percent / 100.0))

    @property
    def available_cpu_cores(self) -> float:
        """Return usable CPU cores (measured and reservation-aware)."""
        if self.cpu_count <= 0:
            return 0.0
        uncommitted = max(0.0, self.cpu_count - self.committed_cpu_cores)
        return min(self.measured_cpu_available_cores, uncommitted)

    @property
    def available_memory_mb(self) -> float:
        """Return usable memory (measured and reservation-aware)."""
        if self.memory_total_mb <= 0:
            return max(0.0, self.memory_available_mb)
        uncommitted = max(0.0, self.memory_total_mb - self.committed_memory_mb)
        return min(self.memory_available_mb, uncommitted)

    @property
    def effective_disk_free_mb(self) -> int:
        """Return free disk space, taking the tighter of data/docker roots."""
        if self.docker_disk_free_mb is None:
            return self.disk_free_mb
        return min(self.disk_free_mb, self.docker_disk_free_mb)

    # Backwards-compatible read-only aliases for the plan-01 stub names.
    @property
    def memory_mb(self) -> float:
        """Alias for :attr:`memory_total_mb`."""
        return self.memory_total_mb


class CommittedResources(BaseModel):
    """Resources reserved by non-terminal attempts."""

    model_config = ConfigDict(frozen=True)

    cpu_cores: float = 0.0
    memory_mb: float = 0.0
    disk_mb: int = 0
    attempts: int = 0


class ReadyTask(BaseModel):
    """One schedulable task, as seen by the pure scheduler."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    project_id: str
    priority: int = 0
    created_order: int = 0
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    retry_not_before: datetime | None = None
    attempt_count: int = 0
    critical_path_length: int = 0
    consecutive_deferrals: int = 0
    model_provider: str | None = None
    estimated_tokens: int = 0
    estimated_requests: int = 0

    @model_validator(mode="after")
    def _non_negative(self) -> ReadyTask:
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be >= 0")
        if self.consecutive_deferrals < 0:
            raise ValueError("consecutive_deferrals must be >= 0")
        if self.critical_path_length < 0:
            raise ValueError("critical_path_length must be >= 0")
        if self.estimated_tokens < 0 or self.estimated_requests < 0:
            raise ValueError("estimated model usage must be >= 0")
        return self


class RunningTask(BaseModel):
    """One currently running task, used for concurrency accounting."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    project_id: str
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    priority: int = 0
    created_order: int = 0


class ModelBudgetState(BaseModel):
    """Remaining model budget for one provider (``None`` means unlimited)."""

    model_config = ConfigDict(frozen=True)

    remaining_tokens: int | None = None
    remaining_requests: int | None = None


class SchedulingLimits(BaseModel):
    """All limits needed to make a scheduling decision."""

    model_config = ConfigDict(frozen=True)

    max_global_concurrency: int = 1
    max_project_concurrency: int = 1
    cpu_headroom_pct: float = 10.0
    mem_headroom_pct: float = 10.0
    disk_min_free_mb: int = 1024
    pressure_blocks_elevated: bool = False
    aging_per_round: int = 1
    aging_cap: int = 5
    project_max_concurrency: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_limits(self) -> SchedulingLimits:
        if self.max_global_concurrency < 1:
            raise ValueError("max_global_concurrency must be >= 1")
        if self.max_project_concurrency < 1:
            raise ValueError("max_project_concurrency must be >= 1")
        if not 0.0 <= self.cpu_headroom_pct <= 100.0:
            raise ValueError("cpu_headroom_pct must be between 0 and 100")
        if not 0.0 <= self.mem_headroom_pct <= 100.0:
            raise ValueError("mem_headroom_pct must be between 0 and 100")
        if self.disk_min_free_mb < 0:
            raise ValueError("disk_min_free_mb must be >= 0")
        if self.aging_per_round < 0 or self.aging_cap < 0:
            raise ValueError("aging values must be >= 0")
        if any(value < 1 for value in self.project_max_concurrency.values()):
            raise ValueError("project concurrency overrides must be >= 1")
        return self


class SchedulingSnapshot(BaseModel):
    """Immutable input to one scheduling round.  No I/O happens after this."""

    model_config = ConfigDict(frozen=True)

    now: datetime
    host: HostResources = Field(default_factory=HostResources)
    pressure: PressureState = PressureState.OK
    ready_tasks: list[ReadyTask] = Field(default_factory=list)
    running: list[RunningTask] = Field(default_factory=list)
    limits: SchedulingLimits = Field(default_factory=SchedulingLimits)
    model_budget: dict[str, ModelBudgetState] = Field(default_factory=dict)
    paused_projects: list[str] = Field(default_factory=list)
    project_modes: dict[str, ProjectMode] = Field(default_factory=dict)


class DecisionAction(StrEnum):
    """Action taken for a task in a scheduling round."""

    LAUNCH = "launch"
    DEFER = "defer"


class DeferReason(StrEnum):
    """Explainable reason a task was not launched."""

    CONCURRENCY_GLOBAL = "CONCURRENCY_GLOBAL"
    CONCURRENCY_PROJECT = "CONCURRENCY_PROJECT"
    INSUFFICIENT_CPU = "INSUFFICIENT_CPU"
    INSUFFICIENT_MEMORY = "INSUFFICIENT_MEMORY"
    INSUFFICIENT_DISK = "INSUFFICIENT_DISK"
    PRESSURE = "PRESSURE"
    MODEL_BUDGET = "MODEL_BUDGET"
    BACKOFF = "BACKOFF"
    PROJECT_PAUSED = "PROJECT_PAUSED"


class ScheduleDecision(BaseModel):
    """Decision for one task: launch it or defer it with a reason."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    action: DecisionAction
    reason_code: DeferReason | None = None
    reason: str = ""

    @property
    def launched(self) -> bool:
        """Return whether this decision launches the task."""
        return self.action is DecisionAction.LAUNCH

    @classmethod
    def launch(cls, task_id: str, reason: str = "ready") -> ScheduleDecision:
        """Build a launch decision."""
        return cls(task_id=task_id, action=DecisionAction.LAUNCH, reason=reason)

    @classmethod
    def defer(cls, task_id: str, reason_code: DeferReason, reason: str = "") -> ScheduleDecision:
        """Build a defer decision with a mandatory reason code."""
        return cls(
            task_id=task_id,
            action=DecisionAction.DEFER,
            reason_code=reason_code,
            reason=reason or reason_code.value,
        )


class GuardAction(StrEnum):
    """Instruction produced by :class:`~devsandbox.scheduler.guard.ResourceGuard`."""

    CONTINUE = "continue"
    STOP_LAUNCHING = "stop_launching"
    PREEMPT = "preempt"


class GuardDecision(BaseModel):
    """The resource guard's recommendation to the orchestrator."""

    model_config = ConfigDict(frozen=True)

    action: GuardAction = GuardAction.CONTINUE
    pressure: PressureState = PressureState.OK
    reason: str = ""
    event_type: str = "RESOURCE_PRESSURE"
    victim_task_id: str | None = None

    @property
    def should_stop_launching(self) -> bool:
        """Return whether new launches should be paused."""
        return self.action in (GuardAction.STOP_LAUNCHING, GuardAction.PREEMPT)
