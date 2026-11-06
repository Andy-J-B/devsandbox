"""Thin HTTP/ASGI client used by the Typer CLI (plan 10).

The CLI deliberately knows nothing about services or repositories: it talks to
the same ``/v1`` API a remote operator would use.  ``--local`` swaps the HTTP
transport for an in-process ASGI transport backed by the real application
container, which is handy for development and for the acceptance tests.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from ..common.errors import DevSandboxError
from ..config.settings import DevSandboxSettings, get_settings


class CliError(DevSandboxError):
    """A user-facing CLI failure (API error, connection problem, bad input)."""


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text.strip()}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "request failed")
            code = error.get("code")
            suffix = f" ({code})" if code else ""
            details = error.get("details")
            if details:
                message = f"{message}: {json.dumps(details, sort_keys=True)}"
            return f"{message}{suffix}"
    return f"HTTP {response.status_code}: {response.text.strip()}"


class ApiClient:
    """Async client for the DevSandbox ``/v1`` API."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ plumbing
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        content: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                content=content,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise CliError(f"cannot reach the DevSandbox API: {exc}") from exc
        if response.status_code >= 400:
            raise CliError(_error_message(response))
        if not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    async def _text(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        value = await self._request("GET", path, params=params, headers=headers)
        return value if isinstance(value, str) else json.dumps(value, indent=2)

    # ------------------------------------------------------------------ health
    async def health(self) -> Any:
        return await self._request("GET", "/healthz")

    async def system_status(self) -> Any:
        return await self._request("GET", "/v1/system/status")

    # ---------------------------------------------------------------- projects
    async def list_projects(self) -> list[dict[str, Any]]:
        value = await self._request("GET", "/v1/projects")
        return value if isinstance(value, list) else []

    async def get_project(self, project_id: str) -> dict[str, Any]:
        value = await self._request("GET", f"/v1/projects/{project_id}")
        return value if isinstance(value, dict) else {}

    async def submit_project(self, spec_yaml: str, *, repo: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"spec_yaml": spec_yaml}
        if repo:
            body["repo"] = repo
        value = await self._request("POST", "/v1/projects", json_body=body)
        return value if isinstance(value, dict) else {}

    async def analyze(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/analyze")
        return value if isinstance(value, dict) else {}

    async def start(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/start")
        return value if isinstance(value, dict) else {}

    async def pause(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/pause")
        return value if isinstance(value, dict) else {}

    async def resume(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/resume")
        return value if isinstance(value, dict) else {}

    async def cancel(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/cancel")
        return value if isinstance(value, dict) else {}

    async def set_mode(self, project_id: str, mode: str) -> dict[str, Any]:
        value = await self._request(
            "POST", f"/v1/projects/{project_id}/mode", json_body={"mode": mode}
        )
        return value if isinstance(value, dict) else {}

    # -------------------------------------------------------------------- plans
    async def create_plan(self, project_id: str) -> dict[str, Any]:
        value = await self._request("POST", f"/v1/projects/{project_id}/plans")
        return value if isinstance(value, dict) else {}

    async def list_plans(self, project_id: str) -> list[dict[str, Any]]:
        value = await self._request("GET", f"/v1/projects/{project_id}/plans")
        return value if isinstance(value, list) else []

    async def plans_markdown(self, project_id: str) -> str:
        return await self._text(f"/v1/projects/{project_id}/plans", params={"format": "markdown"})

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        value = await self._request("GET", f"/v1/plans/{plan_id}")
        return value if isinstance(value, dict) else {}

    async def plan_markdown(self, plan_id: str) -> str:
        return await self._text(f"/v1/plans/{plan_id}", params={"format": "markdown"})

    async def approve_plan(self, plan_id: str, *, approver: str = "user") -> dict[str, Any]:
        value = await self._request(
            "POST",
            f"/v1/plans/{plan_id}/approve",
            json_body={"approver": approver},
        )
        return value if isinstance(value, dict) else {}

    async def reject_plan(self, plan_id: str, *, reason: str) -> dict[str, Any]:
        value = await self._request(
            "POST", f"/v1/plans/{plan_id}/reject", json_body={"reason": reason}
        )
        return value if isinstance(value, dict) else {}

    # -------------------------------------------------------------------- tasks
    async def list_tasks(self, project_id: str) -> list[dict[str, Any]]:
        value = await self._request("GET", f"/v1/projects/{project_id}/tasks")
        return value if isinstance(value, list) else []

    async def get_task(self, task_id: str) -> dict[str, Any]:
        value = await self._request("GET", f"/v1/tasks/{task_id}")
        return value if isinstance(value, dict) else {}

    async def get_attempt(self, task_id: str, number: int) -> dict[str, Any]:
        value = await self._request("GET", f"/v1/tasks/{task_id}/attempts/{number}")
        return value if isinstance(value, dict) else {}

    async def logs(
        self,
        task_id: str,
        number: int,
        *,
        stream: str = "agent",
        tail: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"stream": stream}
        if tail is not None:
            params["tail"] = tail
        value = await self._request(
            "GET", f"/v1/tasks/{task_id}/attempts/{number}/logs", params=params
        )
        return value if isinstance(value, dict) else {}

    async def retry(self, task_id: str, *, reset_budget: bool = False) -> dict[str, Any]:
        value = await self._request(
            "POST",
            f"/v1/tasks/{task_id}/retry",
            json_body={"reset_budget": reset_budget},
        )
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------------- events
    async def events(
        self, project_id: str, *, since: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        value = await self._request("GET", f"/v1/projects/{project_id}/events", params=params)
        return value if isinstance(value, list) else []

    async def stream_events(
        self, project_id: str, *, since: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        try:
            async with self._client.stream(
                "GET",
                f"/v1/projects/{project_id}/events/stream",
                params=params,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise CliError(_error_message(response))
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        except httpx.HTTPError as exc:
            raise CliError(f"event stream failed: {exc}") from exc

    # ------------------------------------------------------------------- report
    async def report(self, project_id: str, *, fmt: str = "markdown") -> Any:
        if fmt == "json":
            return await self._request(
                "GET",
                f"/v1/projects/{project_id}/report",
                params={"format": "json"},
                headers={"Accept": "application/json"},
            )
        return await self._text(f"/v1/projects/{project_id}/report", params={"format": fmt})


def _local_token(settings: DevSandboxSettings, token: str | None) -> str:
    if token:
        return token
    configured = settings.effective_api_token
    if configured is not None:
        return configured.get_secret_value()
    # Local mode never leaves the process; an ephemeral token keeps the app's
    # auth requirement meaningful without forcing env setup for a quick demo.
    import secrets

    return secrets.token_urlsafe(32)


def build_local_client(
    settings: DevSandboxSettings | None = None,
    *,
    token: str | None = None,
    timeout: float = 120.0,
) -> ApiClient:
    """Create an in-process client backed by the real application container."""
    from pydantic import SecretStr

    from ..api.app import create_app
    from ..api.bootstrap import build_container

    resolved = settings or get_settings()
    local_token = _local_token(resolved, token)
    local_settings = resolved.model_copy(
        update={
            "api_token": SecretStr(local_token),
            "api": resolved.api.model_copy(update={"token": SecretStr(local_token)}),
        }
    )
    container = build_container(local_settings)
    app = create_app(container)
    return ApiClient(
        "http://devsandbox.local",
        local_token,
        transport=httpx.ASGITransport(app=app),
        timeout=timeout,
    )


@asynccontextmanager
async def open_client(
    *,
    local: bool = False,
    api_url: str | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> AsyncIterator[ApiClient]:
    """Open either a local ASGI client or a remote HTTP client."""
    settings = get_settings()
    if local:
        client = build_local_client(settings, token=token, timeout=max(timeout, 120.0))
    else:
        base_url = api_url or f"http://{settings.api.host}:{settings.api.port}"
        effective = token
        if effective is None and settings.effective_api_token is not None:
            effective = settings.effective_api_token.get_secret_value()
        client = ApiClient(base_url, effective, timeout=timeout)
    try:
        yield client
    finally:
        await client.aclose()


__all__ = ["ApiClient", "CliError", "build_local_client", "open_client"]
