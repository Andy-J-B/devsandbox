"""Plan retrieval, editing, and approval endpoints (plan 10)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from ...planner.models import Plan, PlanPatch
from ...planner.render import render_plan_markdown
from ..queries import load_plan
from ..schemas import ApproveRequest, RejectRequest
from ._common import get_container

router = APIRouter(tags=["plans"])


@router.get("/plans/{plan_id}")
async def get_plan(
    request: Request,
    plan_id: str,
    response_format: Literal["json", "markdown"] = Query(default="json", alias="format"),
) -> Any:
    container = get_container(request)
    async with container.session_factory() as session:
        await load_plan(session, plan_id)
        plan = await container.plan_services(session).get_plan(plan_id)
    if response_format == "markdown":
        return PlainTextResponse(render_plan_markdown(plan))
    return plan


@router.patch("/plans/{plan_id}", response_model=Plan)
async def edit_plan(request: Request, plan_id: str, patch: PlanPatch) -> Plan:
    container = get_container(request)
    async with container.session_factory() as session:
        plan = await container.plan_services(session).edit_plan(plan_id, patch)
        await session.commit()
        return plan


@router.post("/plans/{plan_id}/approve", response_model=Plan)
async def approve_plan(
    request: Request,
    plan_id: str,
    body: ApproveRequest | None = None,
) -> Plan:
    container = get_container(request)
    approver = body.approver if body is not None else "user"
    async with container.session_factory() as session:
        plan_model = await load_plan(session, plan_id)
        project_id = plan_model.project_id
    return await container.projects.approve_plan(project_id, plan_id, approver=approver)


@router.post("/plans/{plan_id}/reject", response_model=Plan)
async def reject_plan(
    request: Request,
    plan_id: str,
    body: RejectRequest | None = None,
) -> Plan:
    container = get_container(request)
    reason = body.reason if body is not None else "rejected by user"
    async with container.session_factory() as session:
        plan_model = await load_plan(session, plan_id)
        project_id = plan_model.project_id
    return await container.projects.reject_plan(
        project_id, plan_id, reason=reason, rejected_by="api"
    )


__all__ = ["router"]
