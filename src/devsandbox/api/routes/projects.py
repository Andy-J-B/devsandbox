"""Project submission, lifecycle, and control endpoints (plan 10)."""

from __future__ import annotations

from typing import Any, Literal

import yaml
from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.errors import InvalidTransition
from ...common.types import ProjectStatus, RepoSource
from ...db.repositories import ProjectRepo, TaskRepo
from ...planner.models import Plan
from ...planner.render import render_plan_markdown
from ...spec.models import ProjectSpec, load_spec
from ..errors import ApiError
from ..queries import (
    load_project,
    project_tasks,
    project_view,
    task_attempts,
    task_summary,
)
from ..schemas import (
    ModeChangeRequest,
    ProjectSummary,
    SubmitProjectRequest,
    TaskSummary,
)
from ._common import get_container

router = APIRouter(tags=["projects"])

_TERMINAL = {ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED}


def _repo_override(spec: ProjectSpec, repo: RepoSource | str) -> RepoSource:
    if isinstance(repo, RepoSource):
        return repo
    value = repo.strip()
    if not value:
        raise ApiError(422, "invalid_repo", "repo override must not be empty")
    if "://" in value or value.startswith("git@"):
        return RepoSource(url=value, ref=spec.repo.ref)
    return RepoSource(path=value, ref=spec.repo.ref)


async def _parse_submission(request: Request, max_bytes: int) -> tuple[ProjectSpec, str]:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ApiError(
            413,
            "spec_too_large",
            f"project spec exceeds the {max_bytes} byte limit",
            details={"max_bytes": max_bytes, "received": len(raw)},
        )
    content_type = request.headers.get("content-type", "")
    repo_override: RepoSource | str | None = None
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise ApiError(422, "invalid_json", f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("spec_yaml"), str):
            raise ApiError(
                422,
                "invalid_request",
                "JSON submissions must include a string 'spec_yaml' field",
            )
        body = SubmitProjectRequest.model_validate(payload)
        spec_yaml = body.spec_yaml
        repo_override = body.repo
    else:
        try:
            spec_yaml = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(422, "invalid_encoding", "spec must be UTF-8") from exc
    if not spec_yaml.strip():
        raise ApiError(422, "empty_spec", "project spec must not be empty")

    spec = load_spec(spec_yaml)
    effective_yaml = spec_yaml
    if repo_override is not None:
        spec = spec.model_copy(update={"repo": _repo_override(spec, repo_override)})
        effective_yaml = yaml.safe_dump(
            spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )
    return spec, effective_yaml


@router.post("/projects", response_model=ProjectSummary, status_code=201)
async def submit_project(request: Request) -> ProjectSummary:
    container = get_container(request)
    spec, spec_yaml = await _parse_submission(request, container.settings.api.max_spec_bytes)
    project = await container.projects.submit_project(spec, spec_yaml=spec_yaml)
    async with container.session_factory() as session:
        return await project_view(session, project)


@router.get("/projects", response_model=list[ProjectSummary])
async def list_projects(request: Request) -> list[ProjectSummary]:
    container = get_container(request)
    async with container.session_factory() as session:
        projects = await ProjectRepo(session).list()
        return [await project_view(session, project) for project in projects]


@router.get("/projects/{project_id}", response_model=ProjectSummary)
async def get_project(request: Request, project_id: str) -> ProjectSummary:
    container = get_container(request)
    async with container.session_factory() as session:
        project = await load_project(session, project_id)
        return await project_view(session, project)


@router.post("/projects/{project_id}/analyze", response_model=ProjectSummary)
async def analyze_project(request: Request, project_id: str) -> ProjectSummary:
    """Run analysis once; repeated calls on an analyzed project are no-ops."""
    container = get_container(request)
    async with container.session_factory() as session:
        project = await load_project(session, project_id)
        status = ProjectStatus(project.status)
    if status is ProjectStatus.LOADED:
        await container.projects.analyze(project_id)
    elif status in _TERMINAL:
        raise InvalidTransition(f"project {project_id} is {status.value} and cannot be analyzed")
    async with container.session_factory() as session:
        current = await load_project(session, project_id)
        return await project_view(session, current)


@router.post("/projects/{project_id}/plans", response_model=Plan)
async def create_plan(request: Request, project_id: str) -> Plan:
    """Generate the next plan version.

    Analysis is idempotent and is run automatically for a freshly submitted
    project so that the documented ``submit -> plan`` flow works without a
    separate call.
    """
    container = get_container(request)
    async with container.session_factory() as session:
        project = await load_project(session, project_id)
        status = ProjectStatus(project.status)
    if status is ProjectStatus.LOADED:
        await container.projects.analyze(project_id)
    return await container.projects.plan(project_id)


@router.get("/projects/{project_id}/plans")
async def list_plans(
    request: Request,
    project_id: str,
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> Any:
    container = get_container(request)
    async with container.session_factory() as session:
        await load_project(session, project_id)
        service = container.plan_services(session)
        plans = await service.list_plans(project_id)
    if response_format == "markdown":
        rendered = "\n\n---\n\n".join(render_plan_markdown(plan) for plan in plans)
        return PlainTextResponse(rendered or "_No plans._\n")
    return plans


@router.post("/projects/{project_id}/start", response_model=ProjectSummary)
async def start_project(request: Request, project_id: str) -> ProjectSummary:
    container = get_container(request)
    await container.projects.start(project_id)
    async with container.session_factory() as session:
        return await project_view(session, await load_project(session, project_id))


@router.post("/projects/{project_id}/pause", response_model=ProjectSummary)
async def pause_project(request: Request, project_id: str) -> ProjectSummary:
    container = get_container(request)
    await container.projects.pause(project_id)
    async with container.session_factory() as session:
        return await project_view(session, await load_project(session, project_id))


@router.post("/projects/{project_id}/resume", response_model=ProjectSummary)
async def resume_project(request: Request, project_id: str) -> ProjectSummary:
    container = get_container(request)
    await container.projects.resume(project_id)
    async with container.session_factory() as session:
        return await project_view(session, await load_project(session, project_id))


@router.post("/projects/{project_id}/cancel", response_model=ProjectSummary)
async def cancel_project(request: Request, project_id: str) -> ProjectSummary:
    container = get_container(request)
    await container.projects.cancel(project_id)
    async with container.session_factory() as session:
        return await project_view(session, await load_project(session, project_id))


@router.post("/projects/{project_id}/mode", response_model=ProjectSummary)
async def set_mode(request: Request, project_id: str, body: ModeChangeRequest) -> ProjectSummary:
    container = get_container(request)
    await container.projects.set_mode(project_id, body.mode, requested_by="api")
    async with container.session_factory() as session:
        return await project_view(session, await load_project(session, project_id))


@router.get("/projects/{project_id}/tasks", response_model=list[TaskSummary])
async def list_project_tasks(request: Request, project_id: str) -> list[TaskSummary]:
    container = get_container(request)
    async with container.session_factory() as session:
        project = await load_project(session, project_id)
        tasks = await project_tasks(session, project)
        result: list[TaskSummary] = []
        for task in tasks:
            attempts = await task_attempts(session, task.id)
            dependencies = await container_task_dependencies(session, task.id)
            result.append(task_summary(task, attempts=attempts, dependencies=dependencies))
        return result


async def container_task_dependencies(session: AsyncSession, task_id: str) -> list[str]:
    """Return dependency task ids through the task repository."""
    return await TaskRepo(session).dependencies_for_task(task_id)


__all__ = ["router"]
