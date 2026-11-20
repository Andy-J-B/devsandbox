"""FastAPI application factory for DevSandbox (plan 10)."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from ..common.errors import ConfigError
from ..common.logging import get_logger
from .container import AppContainer
from .errors import error_response, register_error_handlers
from .metrics import CONTENT_TYPE, HttpMetrics
from .routes import events, plans, projects, reports, system, tasks

_log = get_logger("devsandbox.api")

_EXPENSIVE_SUFFIXES = {
    "analyze",
    "plans",
    "approve",
    "reject",
    "start",
    "pause",
    "resume",
    "cancel",
    "mode",
    "retry",
    "gc",
}


class _RateLimiter:
    """Tiny in-memory sliding-window limiter for expensive endpoints."""

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self._limit <= 0:
            return True
        now = time.monotonic()
        bucket = self._events[key]
        cutoff = now - 60.0
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return True


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def _is_expensive(method: str, path: str) -> bool:
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return False
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return tail in _EXPENSIVE_SUFFIXES


def _client_key(request: Request) -> str:
    token = _bearer_token(request)
    if token:
        # Hash the whole credential so two tokens that share a prefix do not
        # share a rate-limit bucket, and the raw token is not retained.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"token:{digest}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _matches_any_token(provided: str, expected: tuple[SecretStr, ...]) -> bool:
    """Constant-time comparison against every accepted token.

    ``secrets.compare_digest`` on ``str`` raises for non-ASCII input, so both
    sides are encoded first.  The loop deliberately does not short-circuit.
    """
    provided_bytes = provided.encode("utf-8")
    matched = False
    for candidate in expected:
        value = candidate.get_secret_value()
        matched |= secrets.compare_digest(provided_bytes, value.encode("utf-8"))
    return matched


def _version(container: AppContainer | None) -> str:
    return container.version if container is not None else "0.1.0"


def _route_template(request: Request) -> str:
    """Return the full matched route template for bounded metric labels.

    Newer FastAPI versions keep the included router prefix outside
    ``route.path`` (for example ``/projects/{project_id}`` for the real
    ``/v1/projects/{project_id}``), so static segments are aligned from the
    right against the actual path.  Parameter values are never copied into
    the label.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str) or not template.startswith("/"):
        return "unmatched"
    route_parts = [part for part in template.strip("/").split("/") if part]
    actual_parts = [part for part in request.url.path.strip("/").split("/") if part]
    if not route_parts or len(route_parts) > len(actual_parts):
        return template
    offset = len(actual_parts) - len(route_parts)
    rendered = actual_parts[:offset]
    for route_part, actual_part in zip(route_parts, actual_parts[offset:], strict=True):
        if route_part.startswith("{") and route_part.endswith("}"):
            rendered.append(route_part)
        else:
            rendered.append(actual_part)
    return "/" + "/".join(rendered)


def create_app(
    container: AppContainer | None = None,
    *,
    lifespan: Any = None,
) -> FastAPI:
    """Create the API app around an injected :class:`AppContainer`.

    Passing ``None`` is supported only for schema generation / import-time
    tooling; every real server and test must supply a container.  ``lifespan``
    lets :mod:`devsandbox.api.server` start the orchestration engine alongside
    the HTTP server.
    """
    settings = container.settings if container is not None else None
    if container is not None and not container.settings.effective_api_tokens:
        raise ConfigError("DEVSANDBOX_API_TOKEN is required before the API can serve requests")

    app = FastAPI(
        title="DevSandbox API",
        version=_version(container),
        description="Supervised and autonomous coding-agent orchestration.",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.metrics = HttpMetrics()
    app.state.rate_limiter = _RateLimiter(
        settings.api.expensive_rate_limit_per_minute if settings is not None else 60
    )
    register_error_handlers(app)
    app.include_router(projects.router, prefix="/v1")
    app.include_router(plans.router, prefix="/v1")
    app.include_router(tasks.router, prefix="/v1")
    app.include_router(events.router, prefix="/v1")
    app.include_router(reports.router, prefix="/v1")
    app.include_router(system.router, prefix="/v1")

    def _openapi() -> dict[str, Any]:
        """Add the bearer scheme enforced by the middleware to the schema."""
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
            "description": (
                "Static token from DEVSANDBOX_API_TOKEN (or an old token kept in "
                "api.additional_tokens during a rotation window)."
            ),
        }
        schema["security"] = [{"BearerAuth": []}]
        for probe in ("/healthz", "/readyz"):
            for operation in schema.get("paths", {}).get(probe, {}).values():
                if isinstance(operation, dict):
                    operation["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]

    @app.middleware("http")
    async def _middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        current = app.state.container
        current_settings = current.settings if current is not None else None
        header = (
            current_settings.api.request_id_header
            if current_settings is not None
            else "X-Request-ID"
        )
        request_id = request.headers.get(header) or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()

        def record(response: Response) -> Response:
            app.state.metrics.observe(
                request.method,
                _route_template(request),
                response.status_code,
                time.perf_counter() - started,
            )
            return response

        if current is not None and request.url.path.startswith("/v1"):
            expected = current.settings.effective_api_tokens
            provided = _bearer_token(request)
            if not expected or provided is None or not _matches_any_token(provided, expected):
                return record(
                    error_response(
                        request,
                        401,
                        "unauthorized",
                        "missing or invalid bearer token",
                    )
                )
            if _is_expensive(request.method, request.url.path):
                limiter: _RateLimiter = app.state.rate_limiter
                if not limiter.allow(_client_key(request)):
                    limited = error_response(
                        request,
                        429,
                        "rate_limited",
                        "too many expensive requests; retry later",
                    )
                    limited.headers["Retry-After"] = "60"
                    return record(limited)

        response = await call_next(request)
        response.headers[header] = request_id
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        _log.info(
            "api request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=elapsed_ms,
        )
        return record(response)

    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "checks": {}})

    @app.get("/readyz", include_in_schema=False)
    async def _readyz() -> JSONResponse:
        return await system.readiness(app.state.container)

    if settings is None or settings.api.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def _metrics() -> Response:
            return Response(
                content=await system.metrics_document(app.state.container, app.state.metrics),
                media_type=CONTENT_TYPE,
            )

    return app


__all__ = ["create_app"]
