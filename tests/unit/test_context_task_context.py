"""Task context budget, priority, secret handling and outcome tests (plan 05)."""

from __future__ import annotations

from tests.context_helpers import (
    InMemoryArtifactRepository,
    build_fixture_repo,
    make_task,
)

from devsandbox.common.types import TaskOutcome, TaskOutcomeNote
from devsandbox.context.analyzer import analyze_repo
from devsandbox.context.manager import ContextManagerImpl
from devsandbox.context.outcomes import TaskOutcomeStore
from devsandbox.context.store import ArtifactStore
from devsandbox.context.summary import render_profile_summary
from devsandbox.context.task_context import TaskContextBuilder


def _stores(fixture):
    repository = InMemoryArtifactRepository()
    artifacts = ArtifactStore(repository, repo_lookup=lambda _pid: fixture.handle)
    outcomes = TaskOutcomeStore(artifacts)
    return artifacts, outcomes


async def _builder(tmp_path, *, max_chars: int = 24_000):
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    profile = await analyze_repo(fixture.handle, fixture.revision)
    _artifacts, outcomes = _stores(fixture)
    builder = TaskContextBuilder(
        fixture.handle,
        profile,
        summary=render_profile_summary(profile, profile.readme_excerpt),
        outcome_store=outcomes,
        max_chars=max_chars,
    )
    return fixture, profile, outcomes, builder


async def test_bundle_is_stamped_and_contains_task_files(tmp_path) -> None:
    fixture, profile, _, builder = await _builder(tmp_path)
    task = make_task(expected_files=["src/fixture/app.py"])

    bundle = await builder.build(fixture.name, task, fixture.revision)

    assert bundle.revision == fixture.revision
    assert bundle.built_at is not None
    assert bundle.context_files == ["src/fixture/app.py"]
    assert "FastAPI" in bundle.file_contents["src/fixture/app.py"]
    assert "## task" in bundle.instructions
    assert "Directory: src/fixture" in bundle.instructions
    assert profile.conventions and bundle.conventions == profile.conventions
    assert bundle.commands


async def test_secret_files_are_excluded_and_secret_canary_redacted(tmp_path) -> None:
    fixture, _, _, builder = await _builder(tmp_path)
    task = make_task(
        expected_files=[".env", "src/fixture/settings.py"],
    )

    bundle = await builder.build(fixture.name, task, fixture.revision)

    assert ".env" not in bundle.file_contents
    assert ".env" not in bundle.context_files
    assert "sk-canary-secret-1234567890abcdef" not in bundle.instructions
    assert "[REDACTED]" in bundle.instructions
    assert any("secret-bearing .env" in item for item in bundle.truncation_report)


async def test_budget_prioritizes_task_then_dependencies_then_summary_and_tree(tmp_path) -> None:
    fixture, _, outcomes, builder = await _builder(tmp_path, max_chars=1_200)
    await outcomes.record(
        fixture.name,
        "dep-task",
        TaskOutcomeNote(
            task_id="dep-task",
            outcome=TaskOutcome.SUCCESS,
            revision=fixture.revision,
            files_changed=["src/fixture/app.py"],
            validation_summary="pytest passed",
            decisions=["Kept the public API stable"],
        ),
    )
    task = make_task(expected_files=["src/fixture/app.py"])

    bundle = await builder.build(
        fixture.name,
        task,
        fixture.revision,
        dependency_task_ids=["dep-task"],
    )

    assert "Task: Implement the feature" in bundle.instructions
    assert "pytest passed" in bundle.instructions
    assert any(item.startswith("repository tree: omitted") for item in bundle.truncation_report)
    assert any(
        item.startswith("project summary:") or item.startswith("repository tree:")
        for item in bundle.truncation_report
    )
    assert len(bundle.instructions) <= 1_200


async def test_budget_and_instructions_are_deterministic(tmp_path) -> None:
    fixture, _, outcomes, builder = await _builder(tmp_path, max_chars=1_200)
    await outcomes.record(
        fixture.name,
        "dep-task",
        TaskOutcomeNote(
            task_id="dep-task",
            outcome=TaskOutcome.SUCCESS,
            revision=fixture.revision,
            validation_summary="ok",
        ),
    )
    task = make_task(expected_files=["src/fixture/app.py"])
    first = await builder.build(
        fixture.name, task, fixture.revision, dependency_task_ids=["dep-task"]
    )
    second = await builder.build(
        fixture.name, task, fixture.revision, dependency_task_ids=["dep-task"]
    )

    assert first.instructions == second.instructions
    assert first.truncation_report == second.truncation_report
    assert first.context_files == second.context_files


async def test_recent_failure_notes_are_included_and_marked_untrusted(tmp_path) -> None:
    fixture, _, outcomes, builder = await _builder(tmp_path)
    await outcomes.record(
        fixture.name,
        "task-1",
        TaskOutcomeNote(
            task_id="task-1",
            outcome=TaskOutcome.FAILURE,
            revision=fixture.revision,
            failure_learnings=["The import path must be absolute"],
            decisions=["Claimed it worked (not verified)"],
        ),
    )
    task = make_task(expected_files=["src/fixture/app.py"])

    bundle = await builder.build(fixture.name, task, fixture.revision)

    assert "recent failures" in bundle.instructions
    assert "The import path must be absolute" in bundle.instructions
    assert "untrusted agent report" in bundle.instructions


async def test_outcome_store_history_and_recent_failures(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    _, outcomes = _stores(fixture)
    note = TaskOutcomeNote(
        task_id="task-1",
        outcome=TaskOutcome.FAILURE,
        revision=fixture.revision,
        failure_learnings=["retry with a clean cache"],
    )

    recorded = await outcomes.record(fixture.name, "task-1", note)

    assert recorded.untrusted is True
    assert recorded.recorded_at is not None
    assert await outcomes.latest_for_task(fixture.name, "task-1") == recorded
    assert await outcomes.latest_for_tasks(fixture.name, ["task-1", "missing"]) == [recorded]
    assert await outcomes.recent_failures(fixture.name, "task-1") == [recorded]


async def test_manager_generates_artifacts_and_records_outcomes(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    repository = InMemoryArtifactRepository()
    artifacts = ArtifactStore(repository, repo_lookup=lambda _pid: fixture.handle)
    manager = ContextManagerImpl(
        lambda _pid: fixture.handle,
        artifacts,
    )
    task = make_task(expected_files=["main.go"])

    bundle = await manager.get_task_context(fixture.name, task, fixture.revision)
    assert bundle.revision == fixture.revision
    assert "go test ./..." in bundle.instructions

    await manager.record_task_outcome(
        fixture.name,
        task.id,
        TaskOutcomeNote(
            task_id=task.id,
            outcome=TaskOutcome.SUCCESS,
            validation_summary="go test passed",
        ),
    )
    stores = await manager.get_project_context(fixture.name, fixture.revision)
    assert stores is not None
    kinds = {artifact.kind for artifact in await artifacts.history(fixture.name)}
    assert "repo_profile" in kinds
    assert "project_summary" in kinds
    assert "outcome_notes" in kinds
