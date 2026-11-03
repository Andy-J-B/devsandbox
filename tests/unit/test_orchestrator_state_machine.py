"""Exhaustive tests for the orchestrator task/project state machines."""

from __future__ import annotations

import pytest

from devsandbox.common.errors import InvalidTransition
from devsandbox.common.types import ProjectStatus, TaskStatus
from devsandbox.orchestrator.state_machine import (
    PROJECT_TRANSITIONS,
    TASK_TRANSITIONS,
    ModeChangeDirection,
    TransitionContext,
    assert_transition,
    can_transition,
    derive_project_status,
    guard_allows,
    is_legal,
    is_legal_project,
    is_terminal,
    is_terminal_project,
    legal_targets,
    mode_change_direction,
    unreachable_task_states,
)

# The authoritative expectations from plan 00 §7 plus the documented recovery
# edge SCHEDULED -> READY (recovery table row 1).
EXPECTED_LEGAL: frozenset[tuple[TaskStatus, TaskStatus]] = frozenset(
    {
        (TaskStatus.PENDING, TaskStatus.READY),
        (TaskStatus.PENDING, TaskStatus.BLOCKED),
        (TaskStatus.PENDING, TaskStatus.FAILED),
        (TaskStatus.PENDING, TaskStatus.CANCELLED),
        (TaskStatus.READY, TaskStatus.SCHEDULED),
        (TaskStatus.READY, TaskStatus.BLOCKED),
        (TaskStatus.READY, TaskStatus.FAILED),
        (TaskStatus.READY, TaskStatus.CANCELLED),
        (TaskStatus.SCHEDULED, TaskStatus.RUNNING),
        (TaskStatus.SCHEDULED, TaskStatus.READY),
        (TaskStatus.SCHEDULED, TaskStatus.FAILED),
        (TaskStatus.SCHEDULED, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
        (TaskStatus.RUNNING, TaskStatus.RETRY_WAIT),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
        (TaskStatus.VALIDATING, TaskStatus.VALIDATED),
        (TaskStatus.VALIDATING, TaskStatus.RETRY_WAIT),
        (TaskStatus.VALIDATING, TaskStatus.FAILED),
        (TaskStatus.VALIDATING, TaskStatus.CANCELLED),
        (TaskStatus.VALIDATED, TaskStatus.INTEGRATING),
        (TaskStatus.VALIDATED, TaskStatus.FAILED),
        (TaskStatus.VALIDATED, TaskStatus.CANCELLED),
        (TaskStatus.INTEGRATING, TaskStatus.COMPLETED),
        (TaskStatus.INTEGRATING, TaskStatus.RETRY_WAIT),
        (TaskStatus.INTEGRATING, TaskStatus.FAILED),
        (TaskStatus.INTEGRATING, TaskStatus.CANCELLED),
        (TaskStatus.RETRY_WAIT, TaskStatus.READY),
        (TaskStatus.RETRY_WAIT, TaskStatus.FAILED),
        (TaskStatus.RETRY_WAIT, TaskStatus.CANCELLED),
        (TaskStatus.BLOCKED, TaskStatus.PENDING),
    }
)


def test_legal_pairs_match_authoritative_expectations() -> None:
    actual = {
        (source, target) for source, targets in TASK_TRANSITIONS.items() for target in targets
    }
    assert actual == EXPECTED_LEGAL


def test_every_illegal_pair_is_rejected() -> None:
    for source in TaskStatus:
        for target in TaskStatus:
            if (source, target) in EXPECTED_LEGAL:
                assert is_legal(source, target), (source, target)
                assert can_transition(source, target), (source, target)
            else:
                assert not is_legal(source, target), (source, target)
                assert not can_transition(source, target), (source, target)


def test_every_legal_pair_has_a_test_row() -> None:
    """Guard against an edge being added without updating this exhaustive test."""
    assert len(EXPECTED_LEGAL) == len(
        {(source, target) for source, targets in TASK_TRANSITIONS.items() for target in targets}
    )


def test_terminal_states_have_no_automatic_outgoing_edges() -> None:
    terminals = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
    }
    for state in terminals:
        assert is_terminal(state)
        expected = {TaskStatus.PENDING} if state is TaskStatus.BLOCKED else set()
        assert legal_targets(state) == expected


def test_no_unreachable_states() -> None:
    assert unreachable_task_states() == set()


def test_retry_guards_respect_budget() -> None:
    assert guard_allows(
        TaskStatus.RUNNING, TaskStatus.RETRY_WAIT, TransitionContext(retries_left=1)
    )
    assert not guard_allows(
        TaskStatus.RUNNING, TaskStatus.RETRY_WAIT, TransitionContext(retries_left=0)
    )
    # Unknown facts are permissive; the repository remains the hard gate.
    assert guard_allows(TaskStatus.RUNNING, TaskStatus.RETRY_WAIT, None)
    assert guard_allows(
        TaskStatus.SCHEDULED,
        TaskStatus.READY,
        TransitionContext(infra_failures_left=1),
    )
    assert not guard_allows(
        TaskStatus.SCHEDULED,
        TaskStatus.READY,
        TransitionContext(infra_failures_left=0),
    )


def test_assert_transition_raises_for_illegal_and_guarded_edges() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        assert_transition(
            TaskStatus.RUNNING,
            TaskStatus.RETRY_WAIT,
            TransitionContext(retries_left=0),
        )


def test_project_transitions_and_terminals() -> None:
    assert is_legal_project(ProjectStatus.LOADED, ProjectStatus.ANALYZING)
    assert is_legal_project(ProjectStatus.PLANNING, ProjectStatus.EXECUTING)
    assert is_legal_project(ProjectStatus.AWAITING_APPROVAL, ProjectStatus.EXECUTING)
    assert is_legal_project(ProjectStatus.EXECUTING, ProjectStatus.PAUSED)
    assert is_legal_project(ProjectStatus.PAUSED, ProjectStatus.EXECUTING)
    assert not is_legal_project(ProjectStatus.LOADED, ProjectStatus.EXECUTING)
    for state in (
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    ):
        assert is_terminal_project(state)
        assert PROJECT_TRANSITIONS[state] == set()


def test_derive_project_status() -> None:
    assert derive_project_status([]) is ProjectStatus.COMPLETED
    assert derive_project_status([TaskStatus.PENDING]) is ProjectStatus.EXECUTING
    assert derive_project_status([TaskStatus.RUNNING, TaskStatus.COMPLETED]) is (
        ProjectStatus.EXECUTING
    )
    assert derive_project_status([TaskStatus.COMPLETED, TaskStatus.COMPLETED]) is (
        ProjectStatus.COMPLETED
    )
    assert derive_project_status([TaskStatus.COMPLETED, TaskStatus.FAILED]) is (
        ProjectStatus.FAILED
    )
    assert derive_project_status([TaskStatus.COMPLETED, TaskStatus.BLOCKED]) is (
        ProjectStatus.FAILED
    )
    assert derive_project_status([TaskStatus.COMPLETED, TaskStatus.CANCELLED]) is (
        ProjectStatus.CANCELLED
    )
    assert derive_project_status([TaskStatus.CANCELLED]) is ProjectStatus.CANCELLED


def test_derive_project_status_accepts_orm_like_rows() -> None:
    class Row:
        def __init__(self, status: str) -> None:
            self.status = status

    assert derive_project_status([Row("completed")]) is ProjectStatus.COMPLETED
    assert derive_project_status([Row("pending")]) is ProjectStatus.EXECUTING


def test_mode_change_direction() -> None:
    assert mode_change_direction("autonomous", "supervised") is ModeChangeDirection.DOWNGRADE
    assert mode_change_direction("supervised", "autonomous") is ModeChangeDirection.UPGRADE
    assert mode_change_direction("supervised", "supervised") is ModeChangeDirection.NONE
