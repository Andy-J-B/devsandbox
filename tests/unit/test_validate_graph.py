"""Unit tests for task graph validation."""

from __future__ import annotations

from devsandbox.planner.interfaces import validate_graph
from devsandbox.planner.types import TaskDraft


def test_validate_graph_ok() -> None:
    issues = validate_graph([TaskDraft(key="task-a", title="A")])
    assert issues == []


def test_validate_graph_detects_unknown_dependency() -> None:
    issues = validate_graph([TaskDraft(key="task-a", title="A", depends_on=["task-missing"])])
    assert len(issues) == 1
    assert issues[0].task_key == "task-a"
    assert "unknown dependency" in issues[0].issue


def test_validate_graph_detects_self_dependency() -> None:
    issues = validate_graph([TaskDraft(key="task-a", title="A", depends_on=["task-a"])])
    assert len(issues) == 1


def test_validate_graph_detects_duplicate_keys() -> None:
    issues = validate_graph(
        [TaskDraft(key="task-a", title="A"), TaskDraft(key="task-a", title="A2")]
    )
    assert len(issues) == 1
    assert issues[0].task_key == "task-a"
    assert "duplicate" in issues[0].issue
