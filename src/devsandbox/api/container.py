"""Dependency-injected application container for the HTTP API (plan 10).

Routes never construct services or repositories directly.  Everything they
need is provided by an :class:`AppContainer`, which makes the API testable with
fake git/context/planner/engine implementations while still exercising the real
database repositories.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.time import utcnow
from ..config.settings import DevSandboxSettings
from ..gitmgr.interfaces import GitManager
from ..orchestrator.models import EngineStatus
from ..orchestrator.project_service import ProjectService
from ..planner.service import PlanService
from ..reporting.models import ProjectReport
from ..scheduler.interfaces import ResourceMonitor

PlanServiceFactory = Callable[[AsyncSession], PlanService]
ReportBuilder = Callable[[str], Awaitable[ProjectReport]]


@runtime_checkable
class EngineController(Protocol):
    """The subset of the orchestrator engine used by the HTTP API."""

    async def status(self) -> EngineStatus: ...


@dataclass(frozen=True)
class AppContainer:
    """All services required by the API, supplied by bootstrap or tests."""

    settings: DevSandboxSettings
    session_factory: async_sessionmaker[AsyncSession]
    projects: ProjectService
    plan_services: PlanServiceFactory
    engine: EngineController | None = None
    monitor: ResourceMonitor | None = None
    git: GitManager | None = None
    docker_ping: Callable[[], bool] | None = None
    report_builder: ReportBuilder | None = None
    version: str = "0.1.0"
    clock: Callable[[], datetime] = field(default=utcnow)
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["AppContainer", "EngineController", "PlanServiceFactory", "ReportBuilder"]
