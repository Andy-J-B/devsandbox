"""Deterministic template planner tests (plan 06)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.planner_helpers import make_spec

from devsandbox.common.types import CheckSpec, ProjectContext, RepoProfile
from devsandbox.planner.models import FailureRecord, Plan
from devsandbox.planner.templates import TemplatePlanner, slugify

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "planner" / "template_plan.json"


def _profile() -> RepoProfile:
    return RepoProfile(
        name="golden-demo",
        languages=["python"],
        frameworks=["pytest"],
        test_commands=["pytest -q"],
        build_commands=["python -m build"],
        conventions=["Use type hints"],
        revision="abc123",
    )


def _ctx() -> ProjectContext:
    return ProjectContext(
        goals=["Add feature A", "Fix bug B"],
        requirements=["Keep the API stable"],
        constraints=["Python only"],
    )


async def test_template_plan_is_deterministic() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=["Add feature A", "Fix bug B"])
    first = await planner.generate_plan(spec, _profile(), _ctx())
    second = await planner.generate_plan(spec, _profile(), _ctx())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


async def test_template_plan_from_goals() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=["Add feature A", "Fix bug B"])
    plan = await planner.generate_plan(spec, _profile(), _ctx())

    assert [task.key for task in plan.tasks] == [
        "goal-1-add-feature-a",
        "goal-2-fix-bug-b",
        "final-verification",
    ]
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["goal-1-add-feature-a"]
    assert set(plan.tasks[2].depends_on) == {
        "goal-1-add-feature-a",
        "goal-2-fix-bug-b",
    }
    assert plan.tasks[0].checks[0].run == "pytest -q"
    assert plan.tasks[0].success_criteria
    assert plan.generated_by == "template"
    assert plan.source_revision == "abc123"
    assert "Keep the API stable" in plan.tasks[0].instructions
    assert "Use type hints" in plan.tasks[0].instructions


async def test_template_plan_uses_detected_commands_when_spec_has_none() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=["One goal"], commands=[])
    plan = await planner.generate_plan(spec, _profile(), _ctx())
    assert plan.tasks[0].checks[0].name == "tests"
    assert plan.tasks[0].checks[0].run == "pytest -q"
    assert any(check.name == "build" and not check.required for check in plan.tasks[0].checks)


async def test_template_plan_adds_required_check_when_spec_only_has_optional() -> None:
    planner = TemplatePlanner()
    spec = make_spec(
        goals=["One goal"],
        commands=[CheckSpec(name="lint", run="ruff check .", required=False)],
    )
    plan = await planner.generate_plan(spec, _profile(), _ctx())
    assert any(check.required for check in plan.tasks[0].checks)
    assert any(check.name == "repo-sanity" for check in plan.tasks[0].checks)


async def test_template_plan_falls_back_to_repo_sanity_check() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=["One goal"], commands=[])
    profile = RepoProfile(name="empty")
    plan = await planner.generate_plan(spec, profile, _ctx())
    assert plan.tasks[0].checks[0].name == "repo-sanity"
    assert plan.tasks[0].checks[0].required


async def test_template_plan_with_no_goals() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=[], requirements=["Do the thing"])
    plan = await planner.generate_plan(spec, _profile(), ProjectContext())
    assert plan.tasks[0].key == "goal-1-implement-the-project-requirements"


async def test_template_baseline_and_final_flags() -> None:
    planner = TemplatePlanner(include_baseline=True, include_final_verification=False)
    spec = make_spec(goals=["One goal"])
    plan = await planner.generate_plan(spec, _profile(), _ctx())
    assert plan.tasks[0].key == "baseline"
    assert plan.tasks[1].depends_on == ["baseline"]
    assert all(task.key != "final-verification" for task in plan.tasks)


async def test_template_replan_includes_failure_context() -> None:
    planner = TemplatePlanner()
    spec = make_spec(goals=["Add feature A", "Fix bug B"])
    first = await planner.generate_plan(spec, _profile(), _ctx())
    plan = Plan(id="plan-1", project_id="proj-1", version=1, tasks=first.tasks)
    failures = [
        FailureRecord(
            task_id="task-1",
            task_key="goal-1-add-feature-a",
            attempt_id="attempt-1",
            summary="tests failed",
            error="assertion error",
        )
    ]
    replanned = await planner.replan("proj-1", plan, failures)
    failed_task = next(task for task in replanned.tasks if task.key == "goal-1-add-feature-a")
    assert "Previous attempt failed" in failed_task.instructions
    assert "assertion error" in failed_task.instructions
    assert replanned.generated_by == "template-replan"


async def test_template_golden_plan() -> None:
    planner = TemplatePlanner()
    spec = make_spec(
        name="golden-demo",
        goals=["Add feature A", "Fix bug B"],
        requirements=["Keep the API stable"],
        commands=[
            CheckSpec(name="tests", run="pytest -q", required=True, timeout_seconds=300),
            CheckSpec(name="lint", run="ruff check .", required=False, timeout_seconds=120),
        ],
        restricted_paths=["LICENSE"],
    )
    plan = await planner.generate_plan(spec, _profile(), _ctx())
    actual = json.loads(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Add `done` command", "add-done-command"),
        ("  Mixed CASE and punctuation!!!  ", "mixed-case-and-punctuation"),
        ("", "task"),
        ("---", "task"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected
