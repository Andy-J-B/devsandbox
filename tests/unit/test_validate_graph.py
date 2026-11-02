"""Unit tests for pure task-graph validation and graph algorithms."""

from __future__ import annotations

import random

import pytest
from tests.planner_helpers import make_draft

from devsandbox.common.types import CheckSpec, ResourceProfile
from devsandbox.planner.models import ValidationLimits
from devsandbox.planner.validate import (
    GraphCycleError,
    critical_path,
    ready_set,
    topological_order,
    validate_graph,
)


def _errors(issues):
    return [issue for issue in issues if issue.is_error]


def test_valid_graph_has_no_issues() -> None:
    issues = validate_graph([make_draft("task-a"), make_draft("task-b", depends_on=["task-a"])])
    assert issues == []


def test_duplicate_keys_are_errors() -> None:
    issues = validate_graph([make_draft("task-a"), make_draft("task-a", title="Other")])
    assert any(issue.code == "task.duplicate_key" for issue in _errors(issues))


def test_empty_title_instructions_criteria_and_checks() -> None:
    task = make_draft(
        "task-a",
        title="   ",
        instructions="",
        success_criteria=[],
        checks=[],
    )
    codes = {issue.code for issue in _errors(validate_graph([task]))}
    assert {
        "task.empty_title",
        "task.empty_instructions",
        "task.no_success_criteria",
        "task.no_required_check",
    } <= codes


def test_optional_only_checks_are_not_enough() -> None:
    task = make_draft(
        "task-a",
        checks=[CheckSpec(name="lint", run="ruff check .", required=False)],
    )
    assert any(issue.code == "task.no_required_check" for issue in _errors(validate_graph([task])))


def test_unknown_and_self_dependencies() -> None:
    unknown = validate_graph([make_draft("task-a", depends_on=["missing"])])
    assert any(issue.code == "task.unknown_dependency" for issue in unknown)
    self_dep = validate_graph([make_draft("task-a", depends_on=["task-a"])])
    assert any(issue.code == "task.self_dependency" for issue in self_dep)


def test_cycle_reports_path() -> None:
    tasks = [
        make_draft("task-a", depends_on=["task-c"]),
        make_draft("task-b", depends_on=["task-a"]),
        make_draft("task-c", depends_on=["task-b"]),
    ]
    cycle_issues = [issue for issue in validate_graph(tasks) if issue.code == "graph.cycle"]
    assert cycle_issues
    assert cycle_issues[0].path[0] == cycle_issues[0].path[-1]
    assert set(cycle_issues[0].path) == {"task-a", "task-b", "task-c"}


def test_max_tasks_and_depth() -> None:
    tasks = [make_draft(f"task-{index}") for index in range(4)]
    issues = validate_graph(tasks, limits=ValidationLimits(max_tasks=3))
    assert any(issue.code == "graph.max_tasks" for issue in _errors(issues))

    chain = [
        make_draft("task-0"),
        make_draft("task-1", depends_on=["task-0"]),
        make_draft("task-2", depends_on=["task-1"]),
    ]
    issues = validate_graph(chain, limits=ValidationLimits(max_depth=2))
    assert any(issue.code == "graph.too_deep" for issue in _errors(issues))


def test_priority_timeout_and_retry_limits() -> None:
    task = make_draft("task-a", priority=500, timeout_seconds=0, max_retries=99)
    codes = {issue.code for issue in _errors(validate_graph([task]))}
    assert {"task.priority_out_of_range", "task.invalid_timeout", "task.retries_too_large"} <= codes

    high_timeout = validate_graph(
        [make_draft("task-a", timeout_seconds=9999)],
        limits=ValidationLimits(max_timeout_seconds=100),
    )
    assert any(issue.code == "task.timeout_too_large" for issue in high_timeout)


def test_resources_are_validated_and_policy_overage_is_a_warning() -> None:
    invalid = make_draft(
        "task-a",
        resources=ResourceProfile(cpu_cores=0, memory_mb=-1, disk_mb=0, pids_limit=0),
    )
    assert any(
        issue.code == "task.invalid_resources" for issue in _errors(validate_graph([invalid]))
    )

    big = make_draft(
        "task-a",
        resources=ResourceProfile(cpu_cores=16, memory_mb=65536, disk_mb=99999, pids_limit=9999),
    )
    issues = validate_graph(
        [big],
        limits=ValidationLimits(
            policy_max_resources=ResourceProfile(
                cpu_cores=4, memory_mb=8192, disk_mb=16384, pids_limit=1024
            )
        ),
    )
    assert any(issue.code == "policy.resources_clamped" for issue in issues)
    assert not any(issue.is_error for issue in issues)


def test_expected_files_cannot_overlap_restricted_files() -> None:
    task = make_draft(
        "task-a",
        expected_files=["LICENSE"],
        restricted_files=["LICENSE"],
    )
    assert any(
        issue.code == "task.expected_is_restricted" for issue in _errors(validate_graph([task]))
    )


def test_baseline_restricted_paths_must_be_present() -> None:
    task = make_draft("task-a", restricted_files=[])
    issues = validate_graph(
        [task],
        limits=ValidationLimits(baseline_restricted_paths=["LICENSE", ".github/**"]),
    )
    assert any(issue.code == "task.missing_baseline_restrictions" for issue in _errors(issues))


def test_file_overlap_warning_between_unrelated_tasks() -> None:
    tasks = [
        make_draft("task-a", expected_files=["src/core.py"]),
        make_draft("task-b", expected_files=["src/core.py"]),
    ]
    issues = validate_graph(tasks)
    assert any(issue.code == "graph.file_overlap" for issue in issues)
    assert not any(issue.is_error for issue in issues)

    dependent = [
        make_draft("task-a", expected_files=["src/core.py"]),
        make_draft("task-b", depends_on=["task-a"], expected_files=["src/core.py"]),
    ]
    assert not any(issue.code == "graph.file_overlap" for issue in validate_graph(dependent))


def test_isolated_task_warning() -> None:
    tasks = [
        make_draft("task-a"),
        make_draft("task-b", depends_on=["task-a"]),
        make_draft("task-orphan"),
    ]
    issues = validate_graph(tasks)
    assert any(issue.code == "graph.isolated_task" for issue in issues)


def test_duplicate_dependency_warning() -> None:
    task = make_draft("task-b", depends_on=["task-a", "task-a"])
    issues = validate_graph([make_draft("task-a"), task])
    assert any(issue.code == "task.duplicate_dependency" for issue in issues)


def test_topological_order_respects_dependencies() -> None:
    tasks = [
        make_draft("task-c", depends_on=["task-b"]),
        make_draft("task-a"),
        make_draft("task-b", depends_on=["task-a"]),
    ]
    order = topological_order(tasks)
    positions = {key: index for index, key in enumerate(order)}
    assert positions["task-a"] < positions["task-b"] < positions["task-c"]


def test_topological_order_raises_on_cycle() -> None:
    tasks = [
        make_draft("task-a", depends_on=["task-b"]),
        make_draft("task-b", depends_on=["task-a"]),
    ]
    with pytest.raises(GraphCycleError):
        topological_order(tasks)


def test_critical_path_uses_timeout_weights() -> None:
    tasks = [
        make_draft("task-a", timeout_seconds=10),
        make_draft("task-b", depends_on=["task-a"], timeout_seconds=10),
        make_draft("task-c", timeout_seconds=100),
    ]
    assert critical_path(tasks) == ["task-c"]


def test_ready_set() -> None:
    tasks = [
        make_draft("task-a"),
        make_draft("task-b", depends_on=["task-a"]),
        make_draft("task-c", depends_on=["task-b"]),
    ]
    assert ready_set(tasks, completed=[]) == ["task-a"]
    assert ready_set(tasks, completed=["task-a"]) == ["task-b"]
    assert ready_set(tasks, completed=["task-a", "task-b"]) == ["task-c"]
    assert ready_set(tasks, completed=["task-a", "task-b", "task-c"]) == []


def test_random_dags_are_valid_and_ordered() -> None:
    for seed in range(50):
        rng = random.Random(seed)
        count = rng.randint(1, 12)
        tasks = []
        for index in range(count):
            deps = [f"task-{other}" for other in range(index) if rng.random() < 0.35]
            tasks.append(make_draft(f"task-{index}", depends_on=deps))
        issues = validate_graph(tasks)
        assert not _errors(issues), (seed, _errors(issues))
        order = topological_order(tasks)
        positions = {key: position for position, key in enumerate(order)}
        for task in tasks:
            for dependency in task.depends_on:
                assert positions[dependency] < positions[task.key]


def test_injected_cycle_is_detected_in_random_graph() -> None:
    tasks = [make_draft("task-a"), make_draft("task-b", depends_on=["task-a"])]
    tasks[0] = tasks[0].model_copy(update={"depends_on": ["task-b"]})
    issues = validate_graph(tasks)
    assert any(issue.code == "graph.cycle" for issue in issues)
