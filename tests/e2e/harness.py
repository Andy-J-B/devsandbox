"""Shared helpers for the plan-11 end-to-end suite.

The E2E tests exercise the *real* orchestrator, git manager, context manager,
validator, Docker sandbox, and PostgreSQL repositories.  Only the agent is a
deterministic script (see :mod:`tests.e2e.agents`), so no LLM or network access
is required.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.e2e.agents import ScriptedAgent

from devsandbox.common.types import (
    CheckSpec,
    ProjectMode,
    RepoHandle,
    RepoSource,
    ResourceProfile,
    TaskStatus,
)
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import (
    AgentConfig,
    DevSandboxSettings,
    LimitsConfig,
    PlannerConfig,
    RetriesConfig,
    SecurityConfig,
    ValidationSettings,
)
from devsandbox.context.manager import ContextManagerImpl
from devsandbox.context.store import ArtifactStore
from devsandbox.db.models import (
    AttemptModel,
    EventModel,
    IntegrationModel,
    ProjectModel,
    TaskModel,
)
from devsandbox.db.repositories import (
    AttemptRepo,
    EventRepo,
    IntegrationRepo,
    ProjectRepo,
    TaskRepo,
)
from devsandbox.gitmgr.manager import GitManagerImpl
from devsandbox.orchestrator.engine import FaultInjector, OrchestratorEngine
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.orchestrator.project_service import ProjectService
from devsandbox.orchestrator.recovery import RecoveryService
from devsandbox.planner.models import (
    FailureRecord as PlannerFailureRecord,
)
from devsandbox.planner.models import (
    Plan,
    ProposedPlan,
    TaskDraft,
)
from devsandbox.planner.service import PlanService
from devsandbox.planner.templates import TemplatePlanner
from devsandbox.sandbox.manager import DockerSandboxManager
from devsandbox.scheduler.config import SchedulerConfig
from devsandbox.scheduler.monitor import PsutilResourceMonitor
from devsandbox.scheduler.scheduler import DeterministicScheduler
from devsandbox.spec.models import ProjectSpec
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.validator import ValidatorImpl

FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "e2e-python"
DEFAULT_IMAGE = "devsandbox/validator:latest"
PYTEST_CHECK = CheckSpec(
    name="tests",
    run="python -m pytest -q",
    kind="test",
    timeout_seconds=120,
)


def _slug(text: str, max_length: int = 60) -> str:
    """Turn a task title into a safe relative path fragment."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].strip("-") or "task"


def run_git(args: Sequence[str], cwd: Path) -> str:
    """Run a git command and return stdout, failing loudly."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=DevSandbox E2E",
            "-c",
            "user.email=e2e@devsandbox.local",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def materialize_fixture_repo(destination: Path) -> Path:
    """Copy the fixture project to ``destination`` and make it a git repo."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        FIXTURE_REPO,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    run_git(["init", "-q", "-b", "main"], destination)
    run_git(["add", "-A"], destination)
    run_git(["commit", "-q", "-m", "fixture: initial commit"], destination)
    return destination


def make_bare_remote(source: Path, destination: Path) -> str:
    """Create a ``file://`` bare remote from a local repository."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "-q", "--bare", str(source), str(destination)], destination.parent)
    # A bare clone of a non-bare repo has the source as origin; make the
    # default HEAD explicit so `file://` imports resolve `main`.
    run_git(["symbolic-ref", "HEAD", "refs/heads/main"], destination)
    return destination.as_uri()


def make_settings(
    *,
    data_root: Path,
    database_url: str,
    image: str = DEFAULT_IMAGE,
    max_project_concurrency: int = 1,
    max_global_concurrency: int = 1,
    max_retries: int = 2,
    max_replans: int = 2,
) -> DevSandboxSettings:
    """Build deterministic E2E settings (no ambient env surprises)."""
    return DevSandboxSettings(
        data_root=str(data_root),
        database_url=database_url,
        limits=LimitsConfig(
            max_global_concurrency=max_global_concurrency,
            max_project_concurrency=max_project_concurrency,
        ),
        retries=RetriesConfig(
            default_max_retries=max_retries,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
        ),
        security=SecurityConfig(
            allow_network_default=False,
            allow_push=False,
            allow_main_merge=False,
            allow_autonomous_mode=True,
        ),
        agent=AgentConfig(default_runtime="fake-script"),
        planner=PlannerConfig(max_replans=max_replans),
        validation=ValidationSettings(image=image),
    )


def make_spec(
    *,
    name: str,
    repo: RepoSource,
    goals: Sequence[str] = ("Implement the requested change",),
    mode: ProjectMode = ProjectMode.SUPERVISED,
    checks: Sequence[CheckSpec] = (PYTEST_CHECK,),
    restricted_paths: Sequence[str] = (),
    max_retries: int = 2,
    task_timeout_seconds: int = 300,
    requirements: Sequence[str] = (),
) -> ProjectSpec:
    """Build a valid project spec for the fixture repository."""
    from devsandbox.spec.models import (
        ConstraintsSpec,
        LimitsSpec,
        ValidationSpec,
    )

    return ProjectSpec(
        name=name,
        mode=mode,
        repo=repo,
        goals=list(goals),
        requirements=list(requirements),
        constraints=ConstraintsSpec(restricted_paths=list(restricted_paths)),
        validation=ValidationSpec(commands=list(checks)),
        limits=LimitsSpec(
            max_retries=max_retries,
            task_timeout_seconds=task_timeout_seconds,
            max_concurrency=1,
        ),
    )


class FixedPlanner:
    """Planner returning a caller-supplied deterministic task graph."""

    name = "e2e-fixed"

    def __init__(
        self,
        tasks: Callable[[ProjectSpec], list[TaskDraft]],
        *,
        replan_tasks: Callable[[ProjectSpec], list[TaskDraft]] | None = None,
    ) -> None:
        self._tasks = tasks
        self._replan_tasks = replan_tasks or tasks
        self._spec: ProjectSpec | None = None

    async def generate_plan(self, spec: ProjectSpec, profile: Any, ctx: Any) -> ProposedPlan:
        self._spec = spec
        return ProposedPlan(
            tasks=self._tasks(spec),
            rationale="fixed E2E plan",
            generated_by=self.name,
            source_revision=getattr(profile, "revision", ""),
        )

    async def replan(
        self, project_id: str, plan: Plan, failures: list[PlannerFailureRecord]
    ) -> ProposedPlan:
        spec = self._spec
        if spec is None:
            raise RuntimeError("FixedPlanner.replan called before generate_plan")
        return ProposedPlan(
            tasks=self._replan_tasks(spec),
            rationale="fixed E2E replan",
            generated_by=self.name,
        )


def task_draft(
    key: str,
    *,
    depends_on: Sequence[str] = (),
    checks: Sequence[CheckSpec] = (PYTEST_CHECK,),
    max_retries: int = 0,
    timeout_seconds: int = 120,
    resources: ResourceProfile | None = None,
    restricted_files: Sequence[str] = (),
    expected_files: Sequence[str] = (),
) -> TaskDraft:
    """Build one deterministic plan task."""
    return TaskDraft(
        key=key,
        title=key,
        description=f"E2E task {key}",
        instructions=f"Perform E2E task {key}.",
        depends_on=list(depends_on),
        priority=50,
        success_criteria=["validation passes"],
        checks=list(checks),
        expected_files=list(expected_files),
        restricted_files=list(restricted_files),
        resources=resources or ResourceProfile(),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


@dataclass
class E2EContext:
    """All real services wired for one E2E test."""

    settings: DevSandboxSettings
    data_root: Path
    session_factory: async_sessionmaker[AsyncSession]
    git: GitManagerImpl
    context: ContextManagerImpl
    sandbox: DockerSandboxManager
    validator: ValidatorImpl
    planner: Any
    scheduler: DeterministicScheduler
    monitor: PsutilResourceMonitor
    projects: ProjectService
    engine: OrchestratorEngine
    agent: ScriptedAgent

    def plan_services(self, session: AsyncSession) -> PlanService:
        """Build a plan service bound to an existing session."""
        return PlanService(
            session,
            self.planner,
            SecurityPolicy(self.settings),
            context_manager=self.context,
            settings=self.settings,
            auto_approve=True,
        )

    async def submit_analyze_plan(
        self,
        spec: ProjectSpec,
        *,
        approve: bool | None = None,
    ) -> tuple[str, Plan]:
        """Submit, analyze, and plan a project, optionally approving it."""
        project = await self.projects.submit_project(spec)
        await self.projects.analyze(project.id)
        plan = await self.projects.plan(project.id)
        should_approve = spec.mode is ProjectMode.SUPERVISED if approve is None else approve
        if should_approve and plan.status.value == "proposed":
            plan = await self.projects.approve_plan(project.id, plan.id)
        return project.id, plan

    async def run_engine(self, *, timeout: float = 600.0, max_rounds: int = 1000) -> None:
        """Run the engine until idle (or fail the test on timeout)."""
        import asyncio

        await asyncio.wait_for(self.engine.run_until_idle(max_rounds=max_rounds), timeout=timeout)

    async def submit_and_run(
        self,
        spec: ProjectSpec,
        *,
        approve: bool | None = None,
        timeout: float = 600.0,
    ) -> tuple[str, Plan]:
        """Submit → analyze → plan → (approve) → run to idle."""
        project_id, plan = await self.submit_analyze_plan(spec, approve=approve)
        await self.run_engine(timeout=timeout)
        return project_id, plan

    # -- queries ----------------------------------------------------------
    async def project(self, project_id: str) -> ProjectModel:
        async with self.session_factory() as session:
            row = await ProjectRepo(session).get(project_id)
            assert row is not None
            return row

    async def tasks(self, project_id: str) -> list[TaskModel]:
        async with self.session_factory() as session:
            project = await ProjectRepo(session).get(project_id)
            assert project is not None and project.active_plan_id is not None
            return await TaskRepo(session).list_for_plan(project.active_plan_id)

    async def all_tasks(self, project_id: str) -> list[TaskModel]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(TaskModel).where(TaskModel.project_id == project_id)
            )
            return list(rows.scalars().all())

    async def attempts(self, task_id: str) -> list[AttemptModel]:
        async with self.session_factory() as session:
            return await AttemptRepo(session).list_for_task(task_id)

    async def integrations(self, project_id: str) -> list[IntegrationModel]:
        async with self.session_factory() as session:
            return await IntegrationRepo(session).list_for_project(project_id)

    async def events(self, project_id: str) -> list[EventModel]:
        async with self.session_factory() as session:
            return await EventRepo(session).list_for_project(project_id)

    async def managed_containers(self) -> list[Any]:
        return await self.sandbox.list_managed()

    def repo_handle(self, project_id: str) -> RepoHandle:
        return RepoHandle(
            project_id=project_id,
            path=str(self.data_root / "repos" / project_id / "repo.git"),
            main_branch="main",
            staging_branch=f"devsandbox/{project_id}/staging",
        )

    def ref(self, project_id: str, ref: str) -> str:
        bare = self.data_root / "repos" / project_id / "repo.git"
        return run_git(["rev-parse", ref], bare)


def build_context(
    *,
    settings: DevSandboxSettings,
    session_factory: async_sessionmaker[AsyncSession],
    agent: ScriptedAgent,
    planner: Any | None = None,
    data_root: Path | None = None,
    sandbox: DockerSandboxManager | None = None,
    monitor: Any | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
    scheduler_config: SchedulerConfig | None = None,
    faults: FaultInjector | None = None,
    lease_holder: str | None = None,
) -> E2EContext:
    """Wire the real plan-11 services around a session factory and agent."""
    root = Path(data_root or settings.data_root)
    root.mkdir(parents=True, exist_ok=True)
    policy = SecurityPolicy(settings)
    git = GitManagerImpl(settings, root)

    def repo_lookup(project_id: str) -> RepoHandle | None:
        path = root / "repos" / project_id / "repo.git"
        if not path.exists():
            return None
        return RepoHandle(
            project_id=project_id,
            path=str(path),
            main_branch="main",
            staging_branch=f"devsandbox/{project_id}/staging",
        )

    context = ContextManagerImpl(
        repo_lookup,
        ArtifactStore(_SessionArtifactRepository(session_factory), repo_lookup=repo_lookup),
    )
    sandbox = sandbox or DockerSandboxManager(settings, root)
    validator = ValidatorImpl(
        sandbox,
        git,
        config=ValidationConfig.from_settings(settings),
        settings=settings,
        data_root=root,
    )
    resolved_planner = planner or TemplatePlanner()
    scheduler_config = scheduler_config or SchedulerConfig.from_settings(settings)
    scheduler = DeterministicScheduler(scheduler_config)
    monitor = monitor or PsutilResourceMonitor(scheduler_config, data_root=root)
    projects = ProjectService(
        session_factory,
        git=git,
        context=context,
        planner=resolved_planner,
        policy=policy,
        settings=settings,
    )
    engine_config = orchestrator_config or OrchestratorConfig.from_settings(
        settings,
        idle_sleep_seconds=0.01,
        tick_seconds=0.01,
        shutdown_timeout_seconds=15.0,
    )

    def plan_services(session: AsyncSession) -> PlanService:
        return PlanService(
            session,
            resolved_planner,
            policy,
            context_manager=context,
            settings=settings,
            auto_approve=True,
        )

    async def replanner(project_id: str, failures: list[PlannerFailureRecord]) -> Plan:
        async with session_factory() as session:
            plan = await plan_services(session).replan(project_id, failures)
            await session.commit()
            return plan

    recovery = RecoveryService(
        session_factory,
        git=git,
        sandbox=sandbox,
        config=engine_config,
    )
    engine = OrchestratorEngine(
        session_factory=session_factory,
        git=git,
        context=context,
        agent=agent,
        validator=validator,
        scheduler=scheduler,
        monitor=monitor,
        sandbox=sandbox,
        settings=settings,
        policy=policy,
        config=engine_config,
        scheduler_config=scheduler_config,
        project_service=projects,
        replanner=replanner,
        recovery=recovery,
        faults=faults,
        lease_holder=lease_holder,
    )
    return E2EContext(
        settings=settings,
        data_root=root,
        session_factory=session_factory,
        git=git,
        context=context,
        sandbox=sandbox,
        validator=validator,
        planner=resolved_planner,
        scheduler=scheduler,
        monitor=monitor,
        projects=projects,
        engine=engine,
        agent=agent,
    )


class _SessionArtifactRepository:
    """Artifact repository that owns short-lived sessions (same as production)."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def create(self, artifact: Any) -> Any:
        from devsandbox.db.repositories import ArtifactRepo

        async with self._factory() as session:
            created = await ArtifactRepo(session).create(artifact)
            await session.commit()
            return created

    async def get(self, artifact_id: str) -> Any:
        from devsandbox.db.repositories import ArtifactRepo

        async with self._factory() as session:
            return await ArtifactRepo(session).get(artifact_id)

    async def list_for_project(self, project_id: str, kind: str | None = None) -> list[Any]:
        from devsandbox.db.repositories import ArtifactRepo

        async with self._factory() as session:
            return await ArtifactRepo(session).list_for_project(project_id, kind)

    async def latest_for_project(self, project_id: str, kind: str) -> Any:
        from devsandbox.db.repositories import ArtifactRepo

        async with self._factory() as session:
            return await ArtifactRepo(session).latest_for_project(project_id, kind)

    async def mark_stale(self, artifact_id: str, stale: bool = True) -> None:
        from devsandbox.db.repositories import ArtifactRepo

        async with self._factory() as session:
            await ArtifactRepo(session).mark_stale(artifact_id, stale)
            await session.commit()


# -- invariant helpers -----------------------------------------------------


async def assert_no_secret_in_persisted_state(
    context: E2EContext,
    project_id: str,
    canary: str,
) -> None:
    """Invariant 7: the canary never appears in DB rows or logs."""
    needles = [canary, canary.encode().hex()]
    async with context.session_factory() as session:
        rows = []
        for table in (
            ProjectModel,
            TaskModel,
            AttemptModel,
            IntegrationModel,
            EventModel,
        ):
            result = await session.execute(select(table))
            for row in result.scalars():
                rows.append(repr(vars(row)))
    logs = context.data_root / "logs"
    if logs.exists():
        for path in logs.rglob("*"):
            if path.is_file():
                rows.append(path.read_text(encoding="utf-8", errors="replace"))
    combined = "\n".join(rows)
    for needle in needles:
        assert needle not in combined, f"canary {needle!r} leaked into persisted state"


async def assert_workspaces_clean(
    context: E2EContext, project_id: str, *, allow_retained: bool = False
) -> None:
    """Invariant 4: no workspaces remain for a fully completed project."""
    root = context.data_root / "workspaces" / project_id
    if allow_retained:
        return
    if root.exists():
        leftovers = [p.name for p in root.iterdir() if p.is_dir()]
        assert leftovers == [], f"orphan workspaces remain: {leftovers}"


async def assert_no_managed_containers(context: E2EContext) -> None:
    """Invariant 4: the sandbox manager left no managed containers behind."""
    containers = await context.managed_containers()
    assert containers == [], f"leftover managed containers: {containers}"


def task_statuses(tasks: Sequence[TaskModel]) -> dict[str, TaskStatus]:
    return {task.key: TaskStatus(task.status) for task in tasks}
