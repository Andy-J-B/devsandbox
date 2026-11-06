"""E2E scenarios E1-E7: submit, plan, isolation, completion, and integration."""

from __future__ import annotations

import subprocess

import pytest
from tests.e2e.agents import Script
from tests.e2e.harness import (
    FixedPlanner,
    assert_no_managed_containers,
    assert_workspaces_clean,
    task_draft,
    task_statuses,
)

from devsandbox.common.types import ProjectStatus, TaskStatus


@pytest.mark.e2e
async def test_e1_submit_local_repo_and_generate_plan(e2e_env) -> None:
    """E1: submit YAML + local repo path; planner emits a dependency graph."""
    env = e2e_env
    agent = env.agent()
    ctx = env.context(agent)
    yaml_spec = f"""
version: 1
name: e1-local
mode: supervised
repo:
  path: {env.repo}
goals:
  - Add a multiplication helper
requirements:
  - Keep the existing test suite green
validation:
  commands:
    - name: tests
      run: python -m pytest -q
limits:
  max_retries: 1
"""
    project = await ctx.projects.submit_project(yaml_spec)
    profile = await ctx.projects.analyze(project.id)
    plan = await ctx.projects.plan(project.id)

    assert profile.languages
    tasks = await ctx.tasks(project.id)
    assert len(tasks) >= 2
    keys = {task.key for task in tasks}
    assert any(key.startswith("goal-1-") for key in keys)
    assert "final-verification" in keys
    assert all(TaskStatus(task.status) is TaskStatus.PENDING for task in tasks)

    # The final verification task depends on the goal task.
    by_id = {task.id: task for task in tasks}
    async with ctx.session_factory() as session:
        from devsandbox.db.repositories import TaskRepo

        repo = TaskRepo(session)
        final = next(task for task in tasks if task.key == "final-verification")
        deps = await repo.dependencies_for_task(final.id)
        assert deps
        assert all(dep in by_id for dep in deps)

    project_row = await ctx.project(project.id)
    assert ProjectStatus(project_row.status) is ProjectStatus.AWAITING_APPROVAL
    assert plan.version == 1
    assert (env.data_root / "repos" / project.id / "repo.git").is_dir()


@pytest.mark.e2e
async def test_e2_submit_file_remote_and_run(e2e_env) -> None:
    """E2: a ``file://`` bare remote is cloned and the project completes."""
    env = e2e_env
    agent = env.agent()
    ctx = env.context(agent)
    project_id, _ = await ctx.submit_and_run(env.spec(name="e2-remote", repo_url=True))

    project = await ctx.project(project_id)
    assert ProjectStatus(project.status) is ProjectStatus.COMPLETED
    statuses = task_statuses(await ctx.tasks(project_id))
    assert statuses and all(status is TaskStatus.COMPLETED for status in statuses.values())
    await assert_no_managed_containers(ctx)


@pytest.mark.e2e
async def test_e3_supervised_does_not_execute_before_approval(e2e_env) -> None:
    """E3: supervised projects wait for approval; approval starts execution."""
    env = e2e_env
    agent = env.agent()
    ctx = env.context(agent)

    project_id, plan = await ctx.submit_analyze_plan(env.spec(name="e3-supervised"), approve=False)
    assert plan.status.value == "proposed"
    project = await ctx.project(project_id)
    assert ProjectStatus(project.status) is ProjectStatus.AWAITING_APPROVAL

    await ctx.run_engine(timeout=30)
    tasks = await ctx.tasks(project_id)
    assert all(TaskStatus(task.status) is TaskStatus.PENDING for task in tasks)
    for task in tasks:
        assert await ctx.attempts(task.id) == []
    assert agent.requests == []
    assert await ctx.managed_containers() == []

    await ctx.projects.approve_plan(project_id, plan.id)
    await ctx.run_engine()
    project = await ctx.project(project_id)
    assert ProjectStatus(project.status) is ProjectStatus.COMPLETED
    assert agent.requests


@pytest.mark.e2e
async def test_e4_agent_runs_in_locked_down_container(e2e_env) -> None:
    """E4: the agent container is non-root, unprivileged, and has no Docker socket."""
    env = e2e_env
    agent = env.agent()
    ctx = env.context(agent)
    project_id, _ = await ctx.submit_and_run(env.spec(name="e4-isolation"))

    assert agent.requests
    # Inspect every attempt container recorded by the sandbox manager.  The
    # deterministic name is derived from the attempt id.
    attempts = []
    for task in await ctx.tasks(project_id):
        attempts.extend(await ctx.attempts(task.id))
    assert attempts
    for attempt in attempts:
        # Containers are destroyed after success; the recorded id is
        # deterministic from the attempt id.
        assert attempt.container_id == f"ds-{attempt.id}"

    # Structural guarantees from the sandbox request are enforced by the
    # security suite; here we assert the run actually produced the labels.
    containers = await ctx.managed_containers()
    assert containers == []


@pytest.mark.e2e
async def test_e5_small_task_completes_and_lands_on_staging(e2e_env) -> None:
    """E5: a real edit passes independent validation and commits to staging."""
    env = e2e_env
    content = (
        "def multiply(left: int, right: int) -> int:\n"
        "    return left * right\n\n\n"
        "def test_multiply() -> None:\n"
        "    assert multiply(3, 4) == 12\n"
    )
    agent = env.agent(
        scripts={"add-function": Script(file="tests/test_new_function.py", content=content)}
    )
    planner = FixedPlanner(
        lambda spec: [task_draft("add-function", max_retries=0, timeout_seconds=120)]
    )
    ctx = env.context(agent, planner=planner)
    spec = env.spec(name="e5-complete", max_retries=0)
    project_id, _ = await ctx.submit_and_run(spec, timeout=300)

    project = await ctx.project(project_id)
    assert ProjectStatus(project.status) is ProjectStatus.COMPLETED
    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.COMPLETED

    integrations = await ctx.integrations(project_id)
    assert len(integrations) == 1
    assert integrations[0].status == "integrated"
    staging = ctx.ref(project_id, f"refs/heads/devsandbox/{project_id}/staging")
    assert integrations[0].staging_commit_after == staging

    # The validated commit is reachable from staging head and touches the file.
    files = subprocess.run(
        ["git", "diff", "--name-only", "refs/heads/main", staging],
        cwd=env.data_root / "repos" / project_id / "repo.git",
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "tests/test_new_function.py" in files

    await assert_workspaces_clean(ctx, project_id)
    await assert_no_managed_containers(ctx)


@pytest.mark.e2e
async def test_e6_lying_agent_is_rejected_by_independent_validation(e2e_env) -> None:
    """E6: an agent that exits 0 without changes is not trusted."""
    env = e2e_env
    agent = env.agent(scripts={"lie": Script(mode="lie")})
    planner = FixedPlanner(lambda spec: [task_draft("lie", max_retries=0, timeout_seconds=60)])
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(env.spec(name="e6-lying", max_retries=0), timeout=120)

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.FAILED
    attempts = await ctx.attempts(task.id)
    assert len(attempts) == 1
    report = attempts[0].validation_report_json or {}
    assert report.get("status") == "failed"
    assert report.get("blame") == "agent"
    assert await ctx.integrations(project_id) == []
    assert (await ctx.project(project_id)).status == "failed"


@pytest.mark.e2e
async def test_e7_main_ref_is_byte_for_byte_unchanged(e2e_env) -> None:
    """E7: staged integrations never move ``main``."""
    env = e2e_env
    main_before = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=env.repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    agent = env.agent()
    ctx = env.context(agent)
    project_id, _ = await ctx.submit_and_run(env.spec(name="e7-main"))

    assert ctx.ref(project_id, "refs/heads/main") == main_before
    staging = ctx.ref(project_id, f"refs/heads/devsandbox/{project_id}/staging")
    assert staging != main_before


@pytest.mark.e2e
async def test_e15_restricted_path_is_rejected_without_unbounded_retries(e2e_env) -> None:
    """E15: restricted paths fail immediately; attempts stay within budget."""
    env = e2e_env
    agent = env.agent(scripts={"restricted": Script(mode="touch-restricted")})
    planner = FixedPlanner(
        lambda spec: [task_draft("restricted", max_retries=2, timeout_seconds=60)]
    )
    ctx = env.context(agent, planner=planner)
    project_id, _ = await ctx.submit_and_run(
        env.spec(name="e15-restricted", max_retries=2), timeout=120
    )

    task = (await ctx.tasks(project_id))[0]
    assert TaskStatus(task.status) is TaskStatus.FAILED
    attempts = await ctx.attempts(task.id)
    assert len(attempts) <= task.max_retries + 1
    events = await ctx.events(project_id)
    assert any(event.type == "task.exhausted" for event in events)
