"""Project report endpoint (plan 10)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ...reporting.builder import build_report
from ...reporting.models import ProjectReport
from ...reporting.render import render_html, render_markdown
from ..queries import load_project
from ._common import get_container

router = APIRouter(tags=["report"])


@router.get("/projects/{project_id}/report", response_model=ProjectReport)
async def get_report(
    request: Request,
    project_id: str,
    response_format: Literal["json", "markdown", "html"] = Query(default="json", alias="format"),
) -> Any:
    """Return a read-only, deterministic report of the current DB state."""
    container = get_container(request)
    async with container.session_factory() as session:
        await load_project(session, project_id)
    if container.report_builder is not None:
        report = await container.report_builder(project_id)
    else:
        report = await build_report(
            project_id,
            container.session_factory,
            settings=container.settings,
            git=container.git,
            clock=container.clock,
        )
    if response_format == "markdown":
        return PlainTextResponse(render_markdown(report))
    if response_format == "html":
        return HTMLResponse(render_html(report))
    return report


__all__ = ["router"]
