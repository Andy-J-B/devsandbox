"""Deterministic, pure scheduling decisions.

:func:`select_decisions` is the single source of truth.  It depends only on the
``SchedulingSnapshot`` it receives (no clock, no DB, no Docker), which makes it
fully deterministic and trivially unit-testable.  :class:`DeterministicScheduler`
is the async protocol adapter used by the orchestrator.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import SchedulerConfig
from .models import (
    DecisionAction,
    DeferReason,
    ModelBudgetState,
    PressureState,
    ReadyTask,
    ScheduleDecision,
    SchedulingLimits,
    SchedulingSnapshot,
)

_DEFAULT_BUDGET = ModelBudgetState()

# Only resource-ish defers build up starvation-aging credit.  Backoff and a
# paused project are not resource starvation, so their counters stay put.
_AGING_REASONS: frozenset[DeferReason] = frozenset(
    {
        DeferReason.CONCURRENCY_GLOBAL,
        DeferReason.CONCURRENCY_PROJECT,
        DeferReason.INSUFFICIENT_CPU,
        DeferReason.INSUFFICIENT_MEMORY,
        DeferReason.INSUFFICIENT_DISK,
        DeferReason.PRESSURE,
        DeferReason.MODEL_BUDGET,
    }
)


@dataclass(frozen=True)
class _Budgets:
    cpu_cores: float
    memory_mb: float
    disk_mb: float


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _in_future(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    return _as_utc(value) > _as_utc(now)


def effective_priority(task: ReadyTask, limits: SchedulingLimits) -> int:
    """Return the task priority including deterministic aging."""
    aging = min(task.consecutive_deferrals * limits.aging_per_round, limits.aging_cap)
    return task.priority + aging


def ordering_key(task: ReadyTask, snapshot: SchedulingSnapshot) -> tuple[int, int, int, str]:
    """Return the total order used by the scheduler (deterministic on ties)."""
    return (
        -effective_priority(task, snapshot.limits),
        -task.critical_path_length,
        task.created_order,
        task.task_id,
    )


def ordered_tasks(snapshot: SchedulingSnapshot) -> list[ReadyTask]:
    """Return ready tasks in canonical scheduling order."""
    return sorted(snapshot.ready_tasks, key=lambda task: ordering_key(task, snapshot))


def _project_limit(snapshot: SchedulingSnapshot, project_id: str) -> int:
    override = snapshot.limits.project_max_concurrency.get(project_id)
    if override is None:
        return snapshot.limits.max_project_concurrency
    return min(snapshot.limits.max_project_concurrency, override)


def _resource_budgets(snapshot: SchedulingSnapshot) -> _Budgets:
    host = snapshot.host
    # Host samples normally already include reservations.  Taking the maximum
    # with the running list makes manually constructed snapshots safe too and
    # guarantees we never over-admit if a caller forgets to fill committed_*.
    committed_cpu = max(
        host.committed_cpu_cores,
        math.fsum(running.resources.cpu_cores for running in snapshot.running),
    )
    committed_mem = max(
        host.committed_memory_mb,
        math.fsum(float(running.resources.memory_mb) for running in snapshot.running),
    )
    if host.cpu_count > 0:
        measured_cpu = host.cpu_count * (1.0 - min(100.0, max(0.0, host.cpu_percent)) / 100.0)
        uncommitted_cpu = host.cpu_count - committed_cpu
        cpu = max(0.0, min(measured_cpu, uncommitted_cpu))
        cpu *= 1.0 - snapshot.limits.cpu_headroom_pct / 100.0
    else:
        cpu = math.inf

    if host.memory_total_mb > 0:
        uncommitted_mem = host.memory_total_mb - committed_mem
        memory = max(0.0, min(host.memory_available_mb, uncommitted_mem))
        memory *= 1.0 - snapshot.limits.mem_headroom_pct / 100.0
    else:
        memory = math.inf

    disk = float(max(0, host.effective_disk_free_mb - snapshot.limits.disk_min_free_mb))
    return _Budgets(cpu_cores=cpu, memory_mb=memory, disk_mb=disk)


def _budget_exhausted(task: ReadyTask, budget: ModelBudgetState) -> bool:
    if task.model_provider is None:
        return False
    over_tokens = (
        budget.remaining_tokens is not None and task.estimated_tokens > budget.remaining_tokens
    )
    over_requests = (
        budget.remaining_requests is not None
        and task.estimated_requests > budget.remaining_requests
    )
    return over_tokens or over_requests


def _consume_budget(task: ReadyTask, budget: ModelBudgetState) -> ModelBudgetState:
    if task.model_provider is None:
        return budget
    tokens = budget.remaining_tokens
    requests = budget.remaining_requests
    if tokens is not None:
        tokens = max(0, tokens - task.estimated_tokens)
    if requests is not None:
        requests = max(0, requests - task.estimated_requests)
    return ModelBudgetState(remaining_tokens=tokens, remaining_requests=requests)


def select_decisions(snapshot: SchedulingSnapshot) -> list[ScheduleDecision]:
    """Decide which ready tasks may launch now.

    The returned list is in canonical order and contains exactly one decision
    per ready task.  Every defer carries a :class:`DeferReason`.
    """
    tasks = ordered_tasks(snapshot)
    if not tasks:
        return []

    # 1. Pressure gate.  Critical blocks everything.  Elevated blocks when
    #    something is already running (or when configured to always block).
    elevated_blocks = snapshot.pressure is PressureState.ELEVATED and (
        snapshot.limits.pressure_blocks_elevated or bool(snapshot.running)
    )
    if snapshot.pressure is PressureState.CRITICAL or elevated_blocks:
        return [
            ScheduleDecision.defer(
                task.task_id,
                DeferReason.PRESSURE,
                f"host pressure is {snapshot.pressure.value}",
            )
            for task in tasks
        ]

    paused = set(snapshot.paused_projects)
    global_limit = snapshot.limits.max_global_concurrency
    global_running = len(snapshot.running)
    project_running: Counter[str] = Counter(running.project_id for running in snapshot.running)
    project_admitted: Counter[str] = Counter()

    budgets = _resource_budgets(snapshot)
    remaining = _Budgets(
        cpu_cores=budgets.cpu_cores,
        memory_mb=budgets.memory_mb,
        disk_mb=budgets.disk_mb,
    )
    model_budgets: dict[str, ModelBudgetState] = {
        provider: state.model_copy() for provider, state in snapshot.model_budget.items()
    }

    decisions: list[ScheduleDecision] = []
    # When pressure is elevated and nothing is running we allow exactly one
    # launch; admitting several at once would ignore the pressure signal.
    elevated_single_slot = snapshot.pressure is PressureState.ELEVATED
    admitted = 0

    for task in tasks:
        # 2. Project / retry filters.
        if task.project_id in paused:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.PROJECT_PAUSED,
                    f"project {task.project_id} is paused",
                )
            )
            continue
        retry_at = task.retry_not_before
        if retry_at is not None and _in_future(retry_at, snapshot.now):
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.BACKOFF,
                    f"retry not before {_as_utc(retry_at).isoformat()}",
                )
            )
            continue

        # 3. Concurrency limits.
        if global_running + admitted >= global_limit:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.CONCURRENCY_GLOBAL,
                    f"global concurrency limit {global_limit} reached",
                )
            )
            continue
        project_limit = _project_limit(snapshot, task.project_id)
        if project_running[task.project_id] + project_admitted[task.project_id] >= project_limit:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.CONCURRENCY_PROJECT,
                    f"project {task.project_id} concurrency limit {project_limit} reached",
                )
            )
            continue
        if elevated_single_slot and admitted >= 1:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.PRESSURE,
                    "host pressure is elevated; only one task may start",
                )
            )
            continue

        # 4. Resource admission.
        if task.resources.cpu_cores > remaining.cpu_cores:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.INSUFFICIENT_CPU,
                    f"needs {task.resources.cpu_cores} cores, {remaining.cpu_cores:.2f} available",
                )
            )
            continue
        if task.resources.memory_mb > remaining.memory_mb:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.INSUFFICIENT_MEMORY,
                    f"needs {task.resources.memory_mb} MB, {remaining.memory_mb:.0f} MB available",
                )
            )
            continue
        if task.resources.disk_mb > remaining.disk_mb:
            decisions.append(
                ScheduleDecision.defer(
                    task.task_id,
                    DeferReason.INSUFFICIENT_DISK,
                    f"needs {task.resources.disk_mb} MB disk, "
                    f"{remaining.disk_mb:.0f} MB above reserve",
                )
            )
            continue
        provider = task.model_provider
        if provider is not None:
            budget = model_budgets.get(provider, _DEFAULT_BUDGET)
            if _budget_exhausted(task, budget):
                decisions.append(
                    ScheduleDecision.defer(
                        task.task_id,
                        DeferReason.MODEL_BUDGET,
                        f"model budget exhausted for provider {provider}",
                    )
                )
                continue
            model_budgets[provider] = _consume_budget(task, budget)

        # 5. Admit.
        decisions.append(ScheduleDecision.launch(task.task_id))
        admitted += 1
        project_admitted[task.project_id] += 1
        remaining = _Budgets(
            cpu_cores=max(0.0, remaining.cpu_cores - task.resources.cpu_cores),
            memory_mb=max(0.0, remaining.memory_mb - task.resources.memory_mb),
            disk_mb=max(0.0, remaining.disk_mb - task.resources.disk_mb),
        )

    return decisions


def update_deferral_counts(
    snapshot: SchedulingSnapshot,
    decisions: list[ScheduleDecision],
) -> dict[str, int]:
    """Return the next ``consecutive_deferrals`` value for every ready task.

    Launches reset the counter; resource defers increment it.  Backoff and
    paused-project defers leave it unchanged because they are not starvation.
    The orchestrator persists the returned values and feeds them back into the
    next snapshot.
    """
    by_task = {decision.task_id: decision for decision in decisions}
    result: dict[str, int] = {}
    for task in snapshot.ready_tasks:
        decision = by_task.get(task.task_id)
        if decision is None:
            result[task.task_id] = task.consecutive_deferrals
        elif decision.action is DecisionAction.LAUNCH:
            result[task.task_id] = 0
        elif decision.reason_code in _AGING_REASONS:
            result[task.task_id] = task.consecutive_deferrals + 1
        else:
            result[task.task_id] = task.consecutive_deferrals
    return result


class DeterministicScheduler:
    """Async adapter implementing the plan-01 ``Scheduler`` protocol."""

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()

    async def select(self, snapshot: SchedulingSnapshot) -> list[ScheduleDecision]:
        """Return one deterministic decision per ready task."""
        return select_decisions(snapshot)


# Plan-01 / plan-00 vocabulary aliases.
SchedulerImpl = DeterministicScheduler
