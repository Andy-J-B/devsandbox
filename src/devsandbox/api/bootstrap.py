"""Production wiring for the API/CLI (plan 10).

The container is intentionally the only place that knows about concrete
implementations.  Tests can provide the same :class:`AppContainer` with fakes,
which keeps every route unit-testable without Docker or an LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import docker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..agents.opencode import OpenCodeAdapter
from ..common.types import RepoHandle
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings, get_settings
from ..context.manager import ContextManagerImpl
from ..context.store import ArtifactStore
from ..db.models import ContextArtifactModel
from ..db.repositories import ArtifactRepo
from ..gitmgr.manager import GitManagerImpl
from ..orchestrator.engine import OrchestratorEngine
from ..orchestrator.models import OrchestratorConfig
from ..orchestrator.project_service import ProjectService
from ..orchestrator.recovery import RecoveryService
from ..planner.service import PlanService
from ..planner.templates import TemplatePlanner
from ..sandbox.manager import DockerSandboxManager
from ..scheduler.config import SchedulerConfig
from ..scheduler.monitor import PsutilResourceMonitor
from ..scheduler.scheduler import DeterministicScheduler
from ..validation.config import ValidationConfig
from ..validation.validator import ValidatorImpl
from .container import AppContainer


class SessionArtifactRepository:
    """Artifact repository that owns short-lived sessions.

    ``ArtifactStore`` outlives any single request, so it cannot hold a request
    session.  This adapter opens one session per operation and commits writes.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def create(self, artifact: ContextArtifactModel) -> ContextArtifactModel:
        async with self._factory() as session:
            created = await ArtifactRepo(session).create(artifact)
            await session.commit()
            return created

    async def get(self, artifact_id: str) -> ContextArtifactModel | None:
        async with self._factory() as session:
            return await ArtifactRepo(session).get(artifact_id)

    async def list_for_project(
        self, project_id: str, kind: str | None = None
    ) -> list[ContextArtifactModel]:
        async with self._factory() as session:
            return await ArtifactRepo(session).list_for_project(project_id, kind)

    async def latest_for_project(self, project_id: str, kind: str) -> ContextArtifactModel | None:
        async with self._factory() as session:
            return await ArtifactRepo(session).latest_for_project(project_id, kind)

    async def mark_stale(self, artifact_id: str, stale: bool = True) -> None:
        async with self._factory() as session:
            await ArtifactRepo(session).mark_stale(artifact_id, stale)
            await session.commit()


def _repo_lookup(data_root: Path) -> Callable[[str], RepoHandle | None]:
    def lookup(project_id: str) -> RepoHandle | None:
        path = data_root / "repos" / project_id / "repo.git"
        if not path.exists():
            return None
        return RepoHandle(
            project_id=project_id,
            path=str(path),
            main_branch="main",
            staging_branch=f"devsandbox/{project_id}/staging",
        )

    return lookup


def _docker_ping() -> bool:
    client = docker.from_env()  # type: ignore[attr-defined]
    try:
        return bool(client.ping())
    finally:
        client.close()


def build_container(
    settings: DevSandboxSettings | None = None,
) -> AppContainer:
    """Wire concrete services for ``devsandbox serve`` and ``--local``."""
    resolved = settings or get_settings()
    data_root = Path(resolved.data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    sql_engine = create_async_engine(resolved.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        bind=sql_engine, class_=AsyncSession, expire_on_commit=False
    )

    policy = SecurityPolicy(resolved)
    git = GitManagerImpl(resolved, data_root)
    artifacts = ArtifactStore(
        SessionArtifactRepository(session_factory),
        repo_lookup=_repo_lookup(data_root),
    )
    context = ContextManagerImpl(
        _repo_lookup(data_root),
        artifacts,
    )
    planner = TemplatePlanner()
    sandbox = DockerSandboxManager(resolved, data_root)
    validator = ValidatorImpl(
        sandbox,
        git,
        config=ValidationConfig.from_settings(resolved),
        settings=resolved,
        data_root=data_root,
    )
    agent = OpenCodeAdapter(sandbox, resolved)
    scheduler = DeterministicScheduler(SchedulerConfig.from_settings(resolved))
    monitor = PsutilResourceMonitor(SchedulerConfig.from_settings(resolved), data_root=data_root)
    projects = ProjectService(
        session_factory,
        git=git,
        context=context,
        planner=planner,
        policy=policy,
        settings=resolved,
        baseline_runner=validator.run_baseline,
    )
    recovery = RecoveryService(
        session_factory,
        git=git,
        sandbox=sandbox,
        config=OrchestratorConfig.from_settings(resolved),
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
        settings=resolved,
        policy=policy,
        config=OrchestratorConfig.from_settings(resolved),
        scheduler_config=SchedulerConfig.from_settings(resolved),
        project_service=projects,
        recovery=recovery,
    )

    def plan_services(session: AsyncSession) -> PlanService:
        return PlanService(
            session,
            planner,
            policy,
            context_manager=context,
            settings=resolved,
            auto_approve=True,
        )

    return AppContainer(
        settings=resolved,
        session_factory=session_factory,
        projects=projects,
        plan_services=plan_services,
        engine=engine,
        monitor=monitor,
        git=git,
        docker_ping=_docker_ping,
        extra={"sql_engine": sql_engine, "sandbox": sandbox, "agent": agent},
    )


__all__ = ["SessionArtifactRepository", "build_container"]
