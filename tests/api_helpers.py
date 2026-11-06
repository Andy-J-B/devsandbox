"""Helpers for API/CLI tests: real DB, fake external services (plan 10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.fakes import FakeContextManager, FakeGitManager, FakePlanner, FakeResourceMonitor

from devsandbox.api.app import create_app
from devsandbox.api.container import AppContainer
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.orchestrator.models import EngineStatus
from devsandbox.orchestrator.project_service import ProjectService
from devsandbox.planner.service import PlanService
from devsandbox.reporting.builder import build_report

TOKEN = "test-token"


@dataclass
class FakeEngine:
    """Engine controller fake used by ``/system/status``."""

    statuses: list[EngineStatus] = field(default_factory=list)
    calls: int = 0

    async def status(self) -> EngineStatus:
        self.calls += 1
        if self.statuses:
            return self.statuses.pop(0)
        return EngineStatus(
            running=["running-task"],
            ready_queue=["ready-task"],
            deferred={"deferred-task": "cpu pressure"},
            pressure="ok",
            budget_usage={"proj-1": 1234},
            attempts_total=2,
            paused=False,
            lease_holder="test-holder",
        )


def make_settings(
    data_root: str,
    *,
    token: str = TOKEN,
    max_spec_bytes: int = 1_000_000,
    allow_autonomous: bool = True,
    database_url: str = "postgresql+asyncpg://unused/unused",
) -> DevSandboxSettings:
    """Build isolated settings for one API test."""
    return DevSandboxSettings(
        data_root=data_root,
        database_url=database_url,
        api={
            "token": SecretStr(token),
            "max_spec_bytes": max_spec_bytes,
            "run_engine": False,
            "expensive_rate_limit_per_minute": 0,
        },
        security={"allow_autonomous_mode": allow_autonomous},
    )


def make_container(
    *,
    settings: DevSandboxSettings,
    session_factory: async_sessionmaker[AsyncSession],
    planner: FakePlanner | None = None,
    git: FakeGitManager | None = None,
    context: FakeContextManager | None = None,
    engine: FakeEngine | None = None,
    monitor: FakeResourceMonitor | None = None,
    auto_approve: bool = True,
    report_builder: Any = None,
    docker_ping: Any = None,
) -> AppContainer:
    """Wire the real project/plan services to fake external managers."""
    git = git or FakeGitManager()
    context = context or FakeContextManager()
    planner = planner or FakePlanner()
    policy = SecurityPolicy(settings)
    projects = ProjectService(
        session_factory,
        git=git,
        context=context,
        planner=planner,
        policy=policy,
        settings=settings,
    )

    def plan_services(session: AsyncSession) -> PlanService:
        return PlanService(
            session,
            planner,
            policy,
            context_manager=context,
            settings=settings,
            auto_approve=auto_approve,
        )

    async def default_report_builder(project_id: str) -> Any:
        return await build_report(
            project_id,
            session_factory,
            settings=settings,
            git=git,
            clock=_fixed_clock,
        )

    return AppContainer(
        settings=settings,
        session_factory=session_factory,
        projects=projects,
        plan_services=plan_services,
        engine=engine or FakeEngine(),
        monitor=monitor or FakeResourceMonitor(),
        git=git,
        docker_ping=docker_ping,
        report_builder=report_builder or default_report_builder,
        extra={"git": git, "planner": planner, "context": context},
    )


def _fixed_clock() -> datetime:
    from datetime import UTC

    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_app(container: AppContainer) -> Any:
    """Create the API app (no uvicorn/engine lifespan)."""
    return create_app(container)


def spec_yaml(
    *,
    name: str = "api-demo",
    repo_path: str = "/tmp/devsandbox-api-demo",
    mode: str = "supervised",
) -> str:
    """Minimal valid spec accepted by the plan-01 loader."""
    return (
        f"name: {name}\n"
        "goals:\n"
        "  - Add the requested feature\n"
        "requirements:\n"
        "  - Keep existing behaviour\n"
        f"mode: {mode}\n"
        "repo:\n"
        f"  path: {repo_path}\n"
        "  ref: main\n"
        "validation:\n"
        "  commands:\n"
        "    - name: tests\n"
        "      run: pytest -q\n"
    )


__all__ = [
    "TOKEN",
    "FakeEngine",
    "make_app",
    "make_container",
    "make_settings",
    "spec_yaml",
]
