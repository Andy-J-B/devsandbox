"""Health, readiness, and system status endpoints (plan 10)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from ...common.types import ProjectStatus, TaskStatus
from ...db.models import AttemptModel, TaskModel
from ...db.repositories import LeaseRepo, ProjectRepo
from ...orchestrator.models import EngineStatus
from ..container import AppContainer
from ..schemas import EngineStatusView, HealthResponse, HostResourcesView, SystemStatus
from ._common import get_container

router = APIRouter(tags=["system"])

_ACTIVE_TASKS = {
    TaskStatus.RUNNING,
    TaskStatus.VALIDATING,
    TaskStatus.VALIDATED,
    TaskStatus.INTEGRATING,
    TaskStatus.SCHEDULED,
}


async def readiness(container: AppContainer | None) -> JSONResponse:
    """Check database and Docker connectivity for ``/readyz``."""
    if container is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"container": False}},
        )
    checks: dict[str, bool] = {}
    try:
        async with container.session_factory() as session:
            await session.execute(select(1))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    if container.docker_ping is None:
        checks["docker"] = False
    else:
        try:
            checks["docker"] = bool(await asyncio.to_thread(container.docker_ping))
        except Exception:
            checks["docker"] = False
    ready = all(checks.values())
    body = HealthResponse(
        status="ready" if ready else "not_ready",
        checks=checks,
        version=container.version,
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body.model_dump(mode="json"),
    )


async def _db_engine_status(container: AppContainer) -> EngineStatus:
    async with container.session_factory() as session:
        projects = await ProjectRepo(session).list_by_status(
            [ProjectStatus.EXECUTING, ProjectStatus.PAUSED]
        )
        running: list[str] = []
        ready: list[str] = []
        attempts_total = 0
        for project in projects:
            if project.active_plan_id is not None:
                result = await session.execute(
                    select(TaskModel).where(TaskModel.plan_id == project.active_plan_id)
                )
                for task in result.scalars().all():
                    status = TaskStatus(task.status)
                    if status in _ACTIVE_TASKS:
                        running.append(task.id)
                    elif status is TaskStatus.READY:
                        ready.append(task.id)
            count = await session.execute(
                select(func.count())
                .select_from(AttemptModel)
                .join(TaskModel, TaskModel.id == AttemptModel.task_id)
                .where(TaskModel.project_id == project.id)
            )
            attempts_total += int(count.scalar_one())
        lease = await LeaseRepo(session).get("orchestrator")
    return EngineStatus(
        running=running,
        ready_queue=ready,
        pressure="unknown",
        attempts_total=attempts_total,
        paused=any(ProjectStatus(project.status) is ProjectStatus.PAUSED for project in projects),
        lease_holder=lease.holder if lease is not None else None,
    )


@router.get("/system/status", response_model=SystemStatus)
async def system_status(request: Request) -> SystemStatus:
    container = get_container(request)
    engine = (
        await container.engine.status()
        if container.engine is not None
        else await _db_engine_status(container)
    )
    resources: HostResourcesView | None = None
    pressure = engine.pressure
    if container.monitor is not None:
        try:
            sample = await container.monitor.sample()
            resources = HostResourcesView(
                cpu_percent=sample.cpu_percent,
                cpu_count=sample.cpu_count,
                memory_total_mb=sample.memory_total_mb,
                memory_available_mb=sample.memory_available_mb,
                memory_used_percent=sample.memory_used_percent,
                disk_free_mb=sample.disk_free_mb,
                docker_disk_free_mb=sample.docker_disk_free_mb,
                load_avg=sample.load_avg,
                process_count=sample.process_count,
                sampled_at=sample.sampled_at,
            )
            state = await container.monitor.pressure_state()
            pressure = state.value
        except Exception:
            resources = None

    async with container.session_factory() as session:
        projects = await ProjectRepo(session).list()
    budget = dict(engine.budget_usage)
    api_info: dict[str, Any] = {
        "host": container.settings.api.host,
        "port": container.settings.api.port,
        "auth_required": True,
        "request_id_header": container.settings.api.request_id_header,
    }
    return SystemStatus(
        version=container.version,
        engine=EngineStatusView(
            running=list(engine.running),
            ready_queue=list(engine.ready_queue),
            deferred=dict(engine.deferred),
            pressure=pressure,
            budget_usage=dict(engine.budget_usage),
            attempts_total=engine.attempts_total,
            paused=engine.paused,
            lease_holder=engine.lease_holder,
            last_round_at=engine.last_round_at,
        ),
        resources=resources,
        pressure=pressure,
        budget_usage=budget,
        projects=len(projects),
        api=api_info,
    )


__all__ = ["readiness", "router"]
