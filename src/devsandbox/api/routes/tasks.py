"""Task detail, attempt detail, logs, and manual retry endpoints (plan 10)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.errors import TaskNotFoundError
from ...common.ids import TaskId
from ...common.redaction import redact
from ...common.types import TaskStatus
from ...db.models import AttemptModel
from ...db.repositories import AttemptRepo, TaskRepo
from ..errors import ApiError
from ..queries import (
    attempt_detail,
    load_task,
    task_attempts,
    task_detail,
)
from ..schemas import AttemptDetail, LogChunk, RetryRequest, RetryResponse, TaskDetail
from ._common import get_container

router = APIRouter(tags=["tasks"])


async def _dependencies(session: AsyncSession, task_id: str) -> list[str]:
    return await TaskRepo(session).dependencies_for_task(task_id)


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(request: Request, task_id: str) -> TaskDetail:
    container = get_container(request)
    async with container.session_factory() as session:
        task = await load_task(session, task_id)
        attempts = await task_attempts(session, task_id)
        return task_detail(
            task,
            attempts=attempts,
            dependencies=await _dependencies(session, task_id),
        )


@router.get("/tasks/{task_id}/attempts/{attempt_number}", response_model=AttemptDetail)
async def get_attempt(request: Request, task_id: str, attempt_number: int) -> AttemptDetail:
    container = get_container(request)
    async with container.session_factory() as session:
        await load_task(session, task_id)
        attempt = await AttemptRepo(session).get_for_task(task_id, attempt_number)
        if attempt is None:
            raise TaskNotFoundError(f"attempt {attempt_number} for task {task_id} not found")
        return attempt_detail(attempt)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _log_path(container_settings_data_root: str, attempt: AttemptModel, stream: str) -> Path:
    data_root = Path(container_settings_data_root)
    log_root = data_root / "logs" / attempt.id
    if stream == "agent" and attempt.agent_log_path:
        candidate = Path(attempt.agent_log_path)
    else:
        candidate = log_root / ("stderr.log" if stream == "stderr" else "stdout.log")
    allowed = (data_root / "logs", data_root / "workspaces")
    if not any(_within(candidate, root) for root in allowed):
        raise ApiError(
            400,
            "unsafe_log_path",
            "log path is outside the DevSandbox data root",
        )
    return candidate


def _read_tail(path: Path, *, max_bytes: int, max_lines: int) -> tuple[str, int, bool]:
    if not path.is_file():
        raise ApiError(404, "log_not_found", f"log file not found: {path.name}")
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        raw = handle.read(max_bytes)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    truncated = size > max_bytes
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return "\n".join(lines), len(lines), truncated


@router.get(
    "/tasks/{task_id}/attempts/{attempt_number}/logs",
    response_model=LogChunk,
)
async def get_attempt_logs(
    request: Request,
    task_id: str,
    attempt_number: int,
    stream: Literal["agent", "stdout", "stderr"] = "agent",
    tail: int | None = None,
) -> LogChunk:
    container = get_container(request)
    max_lines = container.settings.api.max_log_lines
    requested_tail = max_lines if tail is None else tail
    if requested_tail < 1 or requested_tail > max_lines:
        raise ApiError(
            422,
            "invalid_tail",
            f"tail must be between 1 and {max_lines}",
        )
    async with container.session_factory() as session:
        await load_task(session, task_id)
        attempt = await AttemptRepo(session).get_for_task(task_id, attempt_number)
        if attempt is None:
            raise TaskNotFoundError(f"attempt {attempt_number} for task {task_id} not found")
        path = _log_path(container.settings.data_root, attempt, stream)
    content, lines, truncated = _read_tail(
        path,
        max_bytes=container.settings.api.max_log_bytes,
        max_lines=requested_tail,
    )
    return LogChunk(
        attempt_id=attempt.id,
        stream=stream,
        content=redact(content),
        lines=lines,
        truncated=truncated,
        path=str(path),
    )


@router.post("/tasks/{task_id}/retry", response_model=RetryResponse)
async def retry_task(
    request: Request, task_id: str, body: RetryRequest | None = None
) -> RetryResponse:
    """Re-open a BLOCKED task through the audited transition function.

    ``FAILED`` is terminal in the authoritative 00 §7 state machine; the API
    refuses to bypass that gate and requires a replan instead.  ``reset_budget``
    is explicit because it changes policy-derived retry accounting.
    """
    container = get_container(request)
    reset_budget = body.reset_budget if body is not None else False
    async with container.session_factory() as session:
        task = await load_task(session, task_id)
        status = TaskStatus(task.status)
        if status is not TaskStatus.BLOCKED:
            raise ApiError(
                409,
                "task_not_retryable",
                f"task {task_id} is {status.value}; only BLOCKED tasks can be "
                "re-opened manually (FAILED tasks require a replan)",
            )
        fields: dict[str, object] = {"blocked_reason": None}
        if reset_budget:
            fields["attempt_count"] = 0
            fields["max_retries"] = container.settings.retries.default_max_retries
        updated = await TaskRepo(session).transition(
            TaskId(task_id),
            [TaskStatus.BLOCKED],
            TaskStatus.PENDING,
            "manual retry requested through API",
            **fields,
        )
        await session.commit()
        return RetryResponse(
            task_id=task_id,
            status=updated.status,
            reset_budget=reset_budget,
        )


__all__ = ["router"]
