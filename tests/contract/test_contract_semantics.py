"""Shared behavioural contracts for real and fake Protocol implementations.

The fakes in ``tests/fakes`` are used across the whole suite.  These tests pin
the semantics the orchestrator relies on (deterministic naming, idempotency,
return types, and ordering) so a fake cannot silently drift from the real
implementation.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.agent_helpers import make_request, make_settings, make_workspace
from tests.api_helpers import spec_yaml
from tests.fakes import (
    FakeAgentAdapter,
    FakeContextManager,
    FakeGitManager,
    FakePlanner,
    FakeResourceMonitor,
    FakeSandboxManager,
    FakeScheduler,
    FakeValidator,
)

from devsandbox.agents.fake import FakeScriptAdapter
from devsandbox.agents.interfaces import AgentRunResult
from devsandbox.common.types import RepoSource
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.context.manager import ContextManagerImpl
from devsandbox.context.store import ArtifactStore
from devsandbox.gitmgr.interfaces import ChangeSet, Workspace
from devsandbox.gitmgr.manager import GitManagerImpl
from devsandbox.planner.models import Plan, ProposedPlan
from devsandbox.planner.templates import TemplatePlanner
from devsandbox.sandbox.interfaces import SandboxResult
from devsandbox.scheduler.models import (
    HostResources,
    PressureState,
    ReadyTask,
    SchedulingLimits,
    SchedulingSnapshot,
)
from devsandbox.scheduler.monitor import PsutilResourceMonitor
from devsandbox.scheduler.scheduler import DeterministicScheduler
from devsandbox.spec.models import load_spec
from devsandbox.validation.interfaces import ValidationReport
from devsandbox.validation.validator import ValidatorImpl

pytestmark = pytest.mark.contract

BACKENDS = ["fake", "real"]


def _now() -> datetime:
    return datetime.now(UTC)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


async def _git_backend(kind: str, tmp_path: Path):
    if kind == "fake":
        return FakeGitManager(), RepoSource(path="/tmp/unused")
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.email", "contract@example.com", cwd=source)
    _git("config", "user.name", "Contract", cwd=source)
    (source / "a.txt").write_text("aaa\n", encoding="utf-8")
    _git("add", "a.txt", cwd=source)
    _git("commit", "-m", "add a", cwd=source)
    manager = GitManagerImpl(
        DevSandboxSettings(data_root=str(tmp_path / "data")), tmp_path / "data"
    )
    return manager, RepoSource(path=str(source))


@pytest.mark.parametrize("kind", BACKENDS)
async def test_git_init_and_workspace_are_deterministic(kind: str, tmp_path: Path) -> None:
    manager, source = await _git_backend(kind, tmp_path)

    repo = await manager.init_project_repo("p1", source)
    again = await manager.init_project_repo("p1", source)
    assert again.project_id == repo.project_id
    assert again.path == repo.path

    workspace = await manager.create_workspace(repo, "attempt-1", "main")
    repeat = await manager.create_workspace(repo, "attempt-1", "main")
    assert repeat.path == workspace.path
    assert repeat.branch == workspace.branch
    assert repeat.base_commit == workspace.base_commit


@pytest.mark.parametrize("kind", BACKENDS)
async def test_git_collect_and_staging_semantics(kind: str, tmp_path: Path) -> None:
    manager, source = await _git_backend(kind, tmp_path)
    repo = await manager.init_project_repo("p1", source)
    workspace = await manager.create_workspace(repo, "attempt-1", "main")

    changes = await manager.collect_changes(workspace)
    assert isinstance(changes, ChangeSet)
    assert changes.base_commit == workspace.base_commit

    target = workspace.base_commit
    await manager.reset_staging_to(repo, target)
    assert await manager.staging_head(repo) == target
    assert await manager.is_ancestor(repo, target, target) is True

    await manager.remove_workspace(workspace, keep_for_diagnostics=False)


@pytest.mark.parametrize("kind", BACKENDS)
async def test_planner_generate_and_replan(kind: str, tmp_path: Path) -> None:
    planner = FakePlanner() if kind == "fake" else TemplatePlanner()
    spec = load_spec(spec_yaml(name=f"contract-{kind}"))
    from devsandbox.common.types import ProjectContext, RepoProfile

    proposed = await planner.generate_plan(spec, RepoProfile(), ProjectContext())
    assert isinstance(proposed, ProposedPlan)
    assert proposed.tasks

    plan = Plan(id="plan-1", version=1, tasks=list(proposed.tasks))
    replanned = await planner.replan("proj-1", plan, [])
    assert isinstance(replanned, ProposedPlan)
    assert len(replanned.tasks) == len(proposed.tasks)


def _snapshot() -> SchedulingSnapshot:
    return SchedulingSnapshot(
        now=_now(),
        host=HostResources(
            cpu_percent=0.0,
            cpu_count=4,
            memory_total_mb=8192,
            memory_available_mb=8192,
            disk_free_mb=100_000,
        ),
        pressure=PressureState.OK,
        ready_tasks=[
            ReadyTask(
                task_id="task-1",
                project_id="proj-1",
                resources={"cpu_cores": 1.0, "memory_mb": 512, "pids_limit": 64},
            )
        ],
        limits=SchedulingLimits(
            max_global_concurrency=2,
            max_project_concurrency=2,
            cpu_headroom_pct=0,
            mem_headroom_pct=0,
            disk_min_free_mb=0,
        ),
    )


@pytest.mark.parametrize("kind", BACKENDS)
async def test_scheduler_is_deterministic_and_one_decision_per_task(kind: str) -> None:
    scheduler = FakeScheduler() if kind == "fake" else DeterministicScheduler()
    snapshot = _snapshot()

    first = await scheduler.select(snapshot)
    second = await scheduler.select(snapshot)

    assert [decision.task_id for decision in first] == ["task-1"]
    assert [decision.action.value for decision in first] == ["launch"]
    assert [decision.model_dump() for decision in first] == [
        decision.model_dump() for decision in second
    ]


@pytest.mark.parametrize("kind", BACKENDS)
async def test_resource_monitor_sample_and_history(kind: str, tmp_path: Path) -> None:
    monitor = FakeResourceMonitor() if kind == "fake" else PsutilResourceMonitor(data_root=tmp_path)

    first = await monitor.sample()
    second = await monitor.sample()

    assert isinstance(first, HostResources)
    assert isinstance(second, HostResources)
    history = await monitor.history()
    assert len(history) >= 2
    assert history[-1] == second
    assert isinstance(await monitor.pressure_state(), PressureState)


@pytest.mark.parametrize("kind", BACKENDS)
async def test_context_manager_project_context(kind: str) -> None:
    if kind == "fake":
        manager = FakeContextManager()
    else:
        manager = ContextManagerImpl(lambda project_id: None, ArtifactStore(_NullArtifacts()))
    from devsandbox.common.types import ProjectContext

    context = await manager.get_project_context("proj-1", "abc")
    assert isinstance(context, ProjectContext)


class _NullArtifacts:
    async def create(self, artifact):  # type: ignore[no-untyped-def]
        return artifact

    async def get(self, artifact_id):  # type: ignore[no-untyped-def]
        return None

    async def list_for_project(self, project_id, kind=None):  # type: ignore[no-untyped-def]
        return []

    async def latest_for_project(self, project_id, kind):  # type: ignore[no-untyped-def]
        return None

    async def mark_stale(self, artifact_id, stale=True):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.parametrize("kind", BACKENDS)
async def test_validator_returns_typed_report(kind: str, tmp_path: Path) -> None:
    if kind == "fake":
        validator = FakeValidator()
    else:
        validator = ValidatorImpl(FakeSandboxManager(), FakeGitManager())
    from devsandbox.common.types import TaskSpec

    task = TaskSpec(id="task-1", title="t", instructions="do it")
    workspace = Workspace(
        attempt_id="attempt-1", path=str(tmp_path / "ws"), base_commit="a", branch="b"
    )
    changes = ChangeSet(base_commit="a", head_commit="b")

    report = await validator.validate_task(task, workspace, changes)

    assert isinstance(report, ValidationReport)
    assert report.status in {"passed", "failed", "error"}


def _queued_sandbox_result(tmp_path: Path) -> SandboxResult:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("contract\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return SandboxResult(
        container_id="container-1",
        exit_code=0,
        timed_out=False,
        oom_killed=False,
        stdout_path=stdout,
        stderr_path=stderr,
        started_at=_now(),
        finished_at=_now(),
        peak_memory_mb=1,
    )


@pytest.mark.parametrize("kind", ["fake", "script"])
async def test_agent_adapters_return_typed_results_and_cancel(kind: str, tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    request = make_request(workspace)
    settings = make_settings()

    if kind == "fake":
        adapter = FakeAgentAdapter()
        adapter.queue_result(AgentRunResult(exit_status="exited_ok", exit_code=0, summary="ok"))
    else:
        sandbox = FakeSandboxManager()
        sandbox.queue_result(_queued_sandbox_result(tmp_path))
        adapter = FakeScriptAdapter(sandbox, settings)

    result = await adapter.run(request)
    assert isinstance(result, AgentRunResult)
    assert result.exit_status in {
        "exited_ok",
        "exited_error",
        "timeout",
        "oom",
        "crashed",
        "sandbox_error",
    }

    await adapter.cancel(request.workspace.attempt_id)
