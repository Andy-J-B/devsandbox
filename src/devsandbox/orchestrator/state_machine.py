"""Authoritative task/project state machines (00 §7, plan 08).

The legal edges are plain data so they can be exhaustively tested and injected
into :class:`devsandbox.db.repositories.task_repo.TaskRepo`.  Guard conditions
are kept separate: structural legality is always enforced by the repository,
while guards describe *when* a legal edge may be taken (for example retrying
only while budget remains).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from ..common.errors import InvalidTransition
from ..common.types import ProjectStatus, TaskStatus

# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.READY: {
        TaskStatus.SCHEDULED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    # SCHEDULED -> READY is the documented recovery edge used when an attempt
    # crashed before any container/agent work happened (recovery table row 1).
    TaskStatus.SCHEDULED: {
        TaskStatus.RUNNING,
        TaskStatus.READY,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.VALIDATING,
        TaskStatus.RETRY_WAIT,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.VALIDATING: {
        TaskStatus.VALIDATED,
        TaskStatus.RETRY_WAIT,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.VALIDATED: {
        TaskStatus.INTEGRATING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.INTEGRATING: {
        TaskStatus.COMPLETED,
        TaskStatus.RETRY_WAIT,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RETRY_WAIT: {
        TaskStatus.READY,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    # BLOCKED is terminal for automatic execution but may be re-opened by an
    # explicit user action or a replan (00 §7).
    TaskStatus.BLOCKED: {TaskStatus.PENDING},
    TaskStatus.CANCELLED: set(),
}

TASK_TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    }
)

TASK_RETRY_EDGES: frozenset[tuple[TaskStatus, TaskStatus]] = frozenset(
    {
        (TaskStatus.RUNNING, TaskStatus.RETRY_WAIT),
        (TaskStatus.VALIDATING, TaskStatus.RETRY_WAIT),
        (TaskStatus.INTEGRATING, TaskStatus.RETRY_WAIT),
    }
)


@dataclass(frozen=True)
class TransitionContext:
    """Facts a guard may inspect when deciding whether an edge may be taken.

    ``None`` means "unknown"; guards are permissive for unknown facts so the
    repository's structural check remains the single hard gate.
    """

    retries_left: int | None = None
    infra_failures_left: int | None = None
    reason: str = ""


def _retry_budget_guard(context: TransitionContext) -> bool:
    return context.retries_left is None or context.retries_left > 0


def _infra_budget_guard(context: TransitionContext) -> bool:
    return context.infra_failures_left is None or context.infra_failures_left > 0


# Guards keyed by exact edge.  Absent edges are unguarded.
TASK_GUARDS: dict[tuple[TaskStatus, TaskStatus], Callable[[TransitionContext], bool]] = {
    (TaskStatus.RUNNING, TaskStatus.RETRY_WAIT): _retry_budget_guard,
    (TaskStatus.VALIDATING, TaskStatus.RETRY_WAIT): _retry_budget_guard,
    (TaskStatus.INTEGRATING, TaskStatus.RETRY_WAIT): _retry_budget_guard,
    (TaskStatus.SCHEDULED, TaskStatus.READY): _infra_budget_guard,
}


def is_legal(from_state: TaskStatus, to_state: TaskStatus) -> bool:
    """Return whether ``from_state -> to_state`` is structurally legal."""
    return to_state in TASK_TRANSITIONS.get(from_state, set())


def guard_allows(
    from_state: TaskStatus,
    to_state: TaskStatus,
    context: TransitionContext | None = None,
) -> bool:
    """Return whether the optional guard for an edge allows the transition."""
    guard = TASK_GUARDS.get((from_state, to_state))
    if guard is None:
        return True
    return guard(context or TransitionContext())


def can_transition(
    from_state: TaskStatus,
    to_state: TaskStatus,
    context: TransitionContext | None = None,
) -> bool:
    """Return whether an edge is both legal and allowed by its guard."""
    return is_legal(from_state, to_state) and guard_allows(from_state, to_state, context)


def assert_transition(
    from_state: TaskStatus,
    to_state: TaskStatus,
    context: TransitionContext | None = None,
) -> None:
    """Raise :class:`InvalidTransition` when an edge may not be taken."""
    if not is_legal(from_state, to_state):
        raise InvalidTransition(f"illegal task transition {from_state.value} -> {to_state.value}")
    if not guard_allows(from_state, to_state, context):
        raise InvalidTransition(
            f"guard rejected task transition {from_state.value} -> {to_state.value}"
        )


def is_terminal(state: TaskStatus) -> bool:
    """Return whether a task state is terminal for automatic execution."""
    return state in TASK_TERMINAL_STATES


def legal_targets(from_state: TaskStatus) -> set[TaskStatus]:
    """Return the legal targets for a task state."""
    return set(TASK_TRANSITIONS.get(from_state, set()))


def unreachable_task_states() -> set[TaskStatus]:
    """Return states that cannot be reached from PENDING.

    ``RETRY_WAIT`` is reachable from RUNNING/VALIDATING/INTEGRATING, so this
    should only ever be empty; the helper exists for the exhaustive test.
    """
    reachable: set[TaskStatus] = {TaskStatus.PENDING}
    frontier = [TaskStatus.PENDING]
    while frontier:
        current = frontier.pop()
        for target in TASK_TRANSITIONS[current]:
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    return set(TaskStatus) - reachable


# ---------------------------------------------------------------------------
# Project state machine
# ---------------------------------------------------------------------------

PROJECT_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.LOADED: {
        ProjectStatus.ANALYZING,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.ANALYZING: {
        ProjectStatus.PLANNING,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.PLANNING: {
        ProjectStatus.AWAITING_APPROVAL,
        ProjectStatus.EXECUTING,  # autonomous mode auto-approves
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.AWAITING_APPROVAL: {
        ProjectStatus.EXECUTING,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.EXECUTING: {
        ProjectStatus.PAUSED,
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.PAUSED: {
        ProjectStatus.EXECUTING,
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.COMPLETED: set(),
    ProjectStatus.FAILED: set(),
    ProjectStatus.CANCELLED: set(),
}

PROJECT_TERMINAL_STATES: frozenset[ProjectStatus] = frozenset(
    {
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    }
)


def is_legal_project(from_state: ProjectStatus, to_state: ProjectStatus) -> bool:
    """Return whether a project state transition is legal."""
    return to_state in PROJECT_TRANSITIONS.get(from_state, set())


def is_terminal_project(state: ProjectStatus) -> bool:
    """Return whether a project state is terminal."""
    return state in PROJECT_TERMINAL_STATES


def derive_project_status(
    tasks: Iterable[object],
) -> ProjectStatus:
    """Derive a terminal/executing project status from its tasks.

    Accepts task rows, ``TaskStatus`` values, or any object with a ``status``
    attribute (ORM rows included).  Paused/cancelled project states are an
    orchestration decision and are not derived here.

    Rules (documented in ``docs/orchestrator.md``):

    * no tasks -> ``COMPLETED`` (vacuously done)
    * any non-terminal task -> ``EXECUTING``
    * otherwise any FAILED/BLOCKED -> ``FAILED``
    * otherwise any CANCELLED -> ``CANCELLED``
    * otherwise -> ``COMPLETED``
    """
    statuses: list[TaskStatus] = []
    for task in tasks:
        status = task if isinstance(task, TaskStatus) else getattr(task, "status", task)
        statuses.append(status if isinstance(status, TaskStatus) else TaskStatus(str(status)))

    if not statuses:
        return ProjectStatus.COMPLETED
    if any(not is_terminal(status) for status in statuses):
        return ProjectStatus.EXECUTING
    if any(status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for status in statuses):
        return ProjectStatus.FAILED
    if any(status is TaskStatus.CANCELLED for status in statuses):
        return ProjectStatus.CANCELLED
    return ProjectStatus.COMPLETED


class ModeChangeDirection(StrEnum):
    """Privilege direction of a project mode change."""

    DOWNGRADE = "downgrade"
    UPGRADE = "upgrade"
    NONE = "none"


def mode_change_direction(current: str, requested: str) -> ModeChangeDirection:
    """Classify a supervised/autonomous mode change.

    Autonomous -> supervised may happen automatically (reduces privilege).
    Supervised -> autonomous is an upgrade and must be an explicit user call.
    """
    if current == requested:
        return ModeChangeDirection.NONE
    if current == "autonomous" and requested == "supervised":
        return ModeChangeDirection.DOWNGRADE
    return ModeChangeDirection.UPGRADE


__all__ = [
    "PROJECT_TERMINAL_STATES",
    "PROJECT_TRANSITIONS",
    "TASK_GUARDS",
    "TASK_RETRY_EDGES",
    "TASK_TERMINAL_STATES",
    "TASK_TRANSITIONS",
    "ModeChangeDirection",
    "TransitionContext",
    "assert_transition",
    "can_transition",
    "derive_project_status",
    "guard_allows",
    "is_legal",
    "is_legal_project",
    "is_terminal",
    "is_terminal_project",
    "legal_targets",
    "mode_change_direction",
    "unreachable_task_states",
]
