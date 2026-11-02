"""Plan rendering tests (plan 06)."""

from __future__ import annotations

import json

from tests.planner_helpers import make_draft

from devsandbox.planner.models import GraphIssue, GraphSeverity, Plan, PlanStatus
from devsandbox.planner.render import (
    render_dependency_graph,
    render_plan_json,
    render_plan_markdown,
)


def _plan() -> Plan:
    return Plan(
        id="plan-1",
        project_id="proj-1",
        version=2,
        status=PlanStatus.PROPOSED,
        generated_by="template",
        planner_version="1.0",
        source_revision="abc",
        rationale="Because it is a small graph.",
        assumptions=["Tests exist"],
        risks=["None"],
        tasks=[
            make_draft("task-a", title="First task"),
            make_draft("task-b", title="Second task", depends_on=["task-a"]),
        ],
        issues=[
            GraphIssue(
                code="graph.file_overlap",
                severity=GraphSeverity.WARNING,
                task_key="task-a",
                issue="possible conflict",
            )
        ],
    )


def test_markdown_includes_graph_criteria_checks_and_warnings() -> None:
    rendered = render_plan_markdown(_plan())
    assert "# Plan v2" in rendered
    assert "Dependency graph" in rendered
    assert "task-a" in rendered
    assert "task-b" in rendered
    assert "Success criteria" in rendered
    assert "pytest -q" in rendered
    assert "Validation issues" in rendered
    assert "possible conflict" in rendered


def test_json_is_stable_and_parseable() -> None:
    rendered = render_plan_json(_plan())
    parsed = json.loads(rendered)
    assert parsed["id"] == "plan-1"
    assert parsed["tasks"][1]["depends_on"] == ["task-a"]


def test_dependency_graph_handles_merges() -> None:
    tasks = [
        make_draft("root"),
        make_draft("left", depends_on=["root"]),
        make_draft("right", depends_on=["root"]),
        make_draft("join", depends_on=["left", "right"]),
    ]
    graph = render_dependency_graph(tasks)
    assert "root" in graph
    assert "join" in graph
    assert "join (see above)" in graph


def test_empty_graph() -> None:
    assert render_dependency_graph([]) == "(empty plan)"
