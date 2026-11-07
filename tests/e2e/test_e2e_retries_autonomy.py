"""E2E scenarios E9-E14: retries, limits, conflicts, autonomy, and reporting."""

from __future__ import annotations

import pytest
from tests.e2e.agents import Script
from tests.e2e.harness import (
    FixedPlanner,
    assert_no_managed_containers,
    task_draft,
    task_statuses,
)
from tests.fakes.scheduler import FakeResourceMonitor

from devsandbox.common.types import (
    ProjectMode,
    ProjectStatus,
    ResourceProfile,
    TaskStatus,
)
from devsandbox.db.repositories import PlanRepo
from devsandbox.reporting.builder import build_report
from devsandbox.scheduler.models import PressureState


@pytest.mark.e2e
async def test_e9_flaky_agent_retries_then_succeeds_with_failure_context(e2e_env) -> None:
    """E9: two failures then success within max_retries, with failure summary."""
    env = e2e_env
    agent = env.agent(
        scripts={
            "flaky": [
                Script(mode="fail"),
                Script(mode="lie"),
                Script(file="docs/flaky-done.md"),
            ]
        }
    )
    planner = FixedPlanner(lambda spec: [task_draft("flaky", max_retries=2, timeout_seconds=120)])
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(env.spec(name="e9-flaky", max_retries=2), timeout=240)

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.COMPLETED
    assert task.attempt_count == 3
    attempts = await ctx.attempts(task.id)
    assert len(attempts) == 3
    assert [attempt.status for attempt in attempts] == ["failed", "failed", "completed"]

    # Retry prompts carry the previous failure summary (not just a bare retry).
    assert agent.requests[1].prior_failure_summary
    assert "agent_error" in agent.requests[1].prior_failure_summary.lower()
    assert agent.requests[2].prior_failure_summary
    assert agent.requests[2].prior_failure_summary != agent.requests[1].prior_failure_summary

    integrations = await ctx.integrations(project_id)
    assert len(integrations) == 1
    assert integrations[0].status == "integrated"
    assert (await ctx.project(project_id)).status == ProjectStatus.COMPLETED.value


@pytest.mark.e2e
async def test_e10_exhausted_retries_fail_task_and_block_dependents(e2e_env) -> None:
    """E10: exhausted retries -> FAILED, dependents BLOCKED, diagnostics kept."""
    env = e2e_env
    agent = env.agent(scripts={"fails": Script(mode="fail")})
    planner = FixedPlanner(
        lambda spec: [
            task_draft("fails", max_retries=1, timeout_seconds=60),
            task_draft("depends", depends_on=["fails"], max_retries=0, timeout_seconds=60),
        ]
    )
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e10-exhausted", max_retries=1), timeout=180
    )

    statuses = task_statuses(await ctx.tasks(project_id))
    assert statuses["fails"] is TaskStatus.FAILED
    assert statuses["depends"] is TaskStatus.BLOCKED
    assert (await ctx.project(project_id)).status == ProjectStatus.FAILED.value

    failed_task = next(task for task in await ctx.tasks(project_id) if task.key == "fails")
    attempts = await ctx.attempts(failed_task.id)
    assert len(attempts) == failed_task.max_retries + 1
    assert failed_task.failure_json
    assert attempts[-1].failure_json
    assert attempts[-1].agent_log_path
    from pathlib import Path

    assert Path(attempts[-1].agent_log_path or "").exists()

    events = await ctx.events(project_id)
    assert any(event.type == "task.exhausted" for event in events)
    assert any(event.type == "task.dependents_blocked" for event in events)


@pytest.mark.e2e
async def test_e11_agent_timeout_is_killed_and_fails(e2e_env) -> None:
    """E11: an infinite loop is killed by the sandbox timeout."""
    env = e2e_env
    agent = env.agent(scripts={"hangs": Script(mode="hang")})
    planner = FixedPlanner(lambda spec: [task_draft("hangs", max_retries=0, timeout_seconds=2)])
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e11-timeout", max_retries=0, task_timeout_seconds=2), timeout=120
    )

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.FAILED
    attempt = (await ctx.attempts(task.id))[0]
    assert attempt.exit_status == "timeout"
    assert (attempt.failure_json or {}).get("kind") == "timeout"
    await assert_no_managed_containers(ctx)


@pytest.mark.e2e
async def test_e11_memory_hog_is_oom_killed(e2e_env) -> None:
    """E11: a memory hog hits the container memory limit and is OOM-killed."""
    env = e2e_env
    hog = Script(
        command=(
            "python3",
            "-c",
            "blocks=[]\nwhile True:\n    blocks.append(bytearray(1024*1024))",
        )
    )
    agent = env.agent(scripts={"hog": hog})
    planner = FixedPlanner(
        lambda spec: [
            task_draft(
                "hog",
                max_retries=0,
                timeout_seconds=30,
                resources=ResourceProfile(cpu_cores=0.5, memory_mb=64, disk_mb=512, pids_limit=64),
            )
        ]
    )
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e11-oom", max_retries=0, task_timeout_seconds=30), timeout=180
    )

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.FAILED
    attempt = (await ctx.attempts(task.id))[0]
    assert attempt.exit_status == "oom"
    assert (attempt.failure_json or {}).get("kind") == "oom"


@pytest.mark.e2e
async def test_e11_pressure_defers_scheduling(e2e_env) -> None:
    """E11: critical host pressure suppresses every launch."""
    env = e2e_env
    agent = env.agent()
    planner = FixedPlanner(lambda spec: [task_draft("deferred", max_retries=0)])
    monitor = FakeResourceMonitor(default_pressure=PressureState.CRITICAL)
    ctx = env.context(agent, planner=planner, monitor=monitor)

    project_id, _ = await ctx.submit_analyze_plan(env.spec(name="e11-pressure"))
    await ctx.run_engine(timeout=30)

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) in {TaskStatus.READY, TaskStatus.PENDING}
    assert await ctx.attempts(task.id) == []
    assert agent.requests == []
    events = await ctx.events(project_id)
    deferrals = [
        event
        for event in events
        if event.type == "SCHEDULE_DECISION"
        and (event.payload_json or {}).get("reason_code") == "PRESSURE"
        and (event.payload_json or {}).get("action") == "defer"
    ]
    assert deferrals


@pytest.mark.e2e
async def test_e12_conflict_is_retried_on_new_staging_head(e2e_env) -> None:
    """E12: concurrent independent edits conflict; the loser retries cleanly."""
    e2e_env.configure(max_project_concurrency=2, max_global_concurrency=2)
    env = e2e_env
    agent = env.agent(
        scripts={
            "edit-a": Script(file="conflict.txt", content="content from task A\n"),
            "edit-b": Script(file="conflict.txt", content="content from task B\n"),
        },
        delay_seconds=0.25,
    )
    planner = FixedPlanner(
        lambda spec: [
            task_draft("edit-a", max_retries=1, timeout_seconds=120),
            task_draft("edit-b", max_retries=1, timeout_seconds=120),
        ]
    )
    ctx = env.context(
        agent, planner=planner, monitor=FakeResourceMonitor(default_pressure=PressureState.OK)
    )
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e12-conflict", max_retries=1), timeout=300
    )

    statuses = task_statuses(await ctx.tasks(project_id))
    assert statuses == {"edit-a": TaskStatus.COMPLETED, "edit-b": TaskStatus.COMPLETED}
    # The concurrency cap was respected...
    assert agent.peak_active <= 2
    assert agent.peak_active >= 2
    # ...and at least one integration lost the merge race and was retried.
    integrations = await ctx.integrations(project_id)
    assert any(integration.status == "conflict" for integration in integrations)
    assert len([i for i in integrations if i.status == "integrated"]) == 2
    staging = ctx.ref(project_id, f"refs/heads/devsandbox/{project_id}/staging")
    assert ctx.ref(project_id, "refs/heads/main") != staging


@pytest.mark.e2e
async def test_e13_autonomous_dag_with_bounded_replan(e2e_env) -> None:
    """E13: autonomous mode runs a 4-task DAG and replans a failed task once."""
    e2e_env.configure(max_replans=1)
    env = e2e_env

    def tasks(_spec):
        return [
            task_draft("start", max_retries=0, timeout_seconds=120),
            task_draft("left", depends_on=["start"], max_retries=0, timeout_seconds=120),
            task_draft("flaky", depends_on=["start"], max_retries=0, timeout_seconds=120),
            task_draft("finish", depends_on=["left", "flaky"], max_retries=0, timeout_seconds=120),
        ]

    agent = env.agent(
        scripts={"flaky": [Script(mode="fail"), Script(file="docs/flaky-replanned.md")]}
    )
    planner = FixedPlanner(tasks)
    ctx = env.context(
        agent, planner=planner, monitor=FakeResourceMonitor(default_pressure=PressureState.OK)
    )
    project_id, _ = await ctx.submit_and_run(
        env.spec(
            name="e13-autonomous",
            mode=ProjectMode.AUTONOMOUS,
            max_retries=0,
        ),
        timeout=420,
    )

    statuses = task_statuses(await ctx.tasks(project_id))
    assert statuses == {
        "start": TaskStatus.COMPLETED,
        "left": TaskStatus.COMPLETED,
        "flaky": TaskStatus.COMPLETED,
        "finish": TaskStatus.COMPLETED,
    }
    async with ctx.session_factory() as session:
        assert await PlanRepo(session).count_replans(project_id) == 1
    events = await ctx.events(project_id)
    assert any(event.type == "project.replanned" for event in events)
    assert (await ctx.project(project_id)).status == ProjectStatus.COMPLETED.value


@pytest.mark.e2e
async def test_e14_report_contains_changes_failures_and_costs(e2e_env) -> None:
    """E14: the final report captures tasks, failures, outstanding work, costs."""
    env = e2e_env
    agent = env.agent(scripts={"fails": Script(mode="fail")})
    planner = FixedPlanner(
        lambda spec: [
            task_draft("fails", max_retries=1, timeout_seconds=60),
            task_draft("blocked", depends_on=["fails"], max_retries=0, timeout_seconds=60),
        ]
    )
    ctx = env.context(
        agent, planner=planner, monitor=FakeResourceMonitor(default_pressure=PressureState.OK)
    )
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e14-report", max_retries=1), timeout=180
    )

    report = await build_report(project_id, ctx.session_factory, settings=env.settings, git=ctx.git)
    assert report.counts.total == 2
    assert report.counts.failed == 1
    assert report.counts.blocked == 1
    assert any(task.attempts for task in report.tasks)
    assert report.outstanding.unresolved_failures
    assert report.outstanding.blocked_tasks
    # Costs are split and the local fake agent never bills inference.
    assert report.costs.inference_usd == 0.0
    assert report.costs.infra_usd == 0.0
    assert report.costs.total_usd == 0.0
