"""Consistent API error handling and the ``{error:{...}}`` envelope."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..common.errors import (
    ConfigError,
    DevSandboxError,
    InvalidTransition,
    PlanLimitError,
    PlanNotFoundError,
    PlanStateError,
    PlanValidationError,
    PolicyViolation,
    ProjectNotFoundError,
    SpecError,
    TaskNotFoundError,
    ValidationError,
)
from ..common.logging import get_logger

_log = get_logger("devsandbox.api.errors")


class ApiError(DevSandboxError):
    """An HTTP-specific problem raised by a route."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details


def request_id(request: Request) -> str | None:
    """Return the middleware-assigned request id (if any)."""
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: Any = None,
) -> JSONResponse:
    """Build the stable error envelope."""
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": jsonable_encoder(details),
        },
        "request_id": request_id(request),
    }
    headers: dict[str, str] = {}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    rid = request_id(request)
    if rid:
        headers["X-Request-ID"] = rid
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _spec_details(exc: SpecError) -> Any:
    cause = exc.__cause__
    if isinstance(cause, PydanticValidationError):
        try:
            return json.loads(cause.json())
        except (TypeError, ValueError):
            return jsonable_encoder(cause.errors())
    return None


def _code_for(exc: BaseException) -> str:
    name = type(exc).__name__
    if name.endswith("Error"):
        name = name[: -len("Error")]
    out: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def register_error_handlers(app: FastAPI) -> None:
    """Install exception handlers on ``app``."""

    @app.exception_handler(ApiError)
    async def _api_problem(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, str(exc), details=exc.details)

    @app.exception_handler(ProjectNotFoundError)
    @app.exception_handler(TaskNotFoundError)
    @app.exception_handler(PlanNotFoundError)
    async def _not_found(request: Request, exc: DevSandboxError) -> JSONResponse:
        return error_response(request, 404, _code_for(exc), str(exc))

    @app.exception_handler(InvalidTransition)
    @app.exception_handler(PlanStateError)
    @app.exception_handler(PlanLimitError)
    async def _conflict(request: Request, exc: DevSandboxError) -> JSONResponse:
        return error_response(request, 409, _code_for(exc), str(exc))

    @app.exception_handler(PolicyViolation)
    async def _forbidden(request: Request, exc: PolicyViolation) -> JSONResponse:
        return error_response(request, 403, "policy_violation", str(exc))

    @app.exception_handler(SpecError)
    async def _invalid_spec(request: Request, exc: SpecError) -> JSONResponse:
        return error_response(
            request,
            422,
            "invalid_spec",
            str(exc),
            details=_spec_details(exc),
        )

    @app.exception_handler(PlanValidationError)
    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: DevSandboxError) -> JSONResponse:
        return error_response(request, 422, _code_for(exc), str(exc))

    @app.exception_handler(ConfigError)
    async def _config(request: Request, exc: ConfigError) -> JSONResponse:
        _log.error("api configuration error", error=str(exc))
        return error_response(request, 500, "config_error", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            422,
            "request_validation_error",
            "request validation failed",
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "http_error"
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 401:
            code = "unauthorized"
        elif exc.status_code == 429:
            code = "rate_limited"
        detail = exc.detail if isinstance(exc.detail, str) else jsonable_encoder(exc.detail)
        return error_response(request, exc.status_code, code, detail)

    @app.exception_handler(DevSandboxError)
    async def _typed(request: Request, exc: DevSandboxError) -> JSONResponse:
        return error_response(request, 400, _code_for(exc), str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        rid = request_id(request)
        _log.error(
            "unhandled api error",
            request_id=rid,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return error_response(
            request,
            500,
            "internal_error",
            "internal server error",
            details={"request_id": rid},
        )


__all__ = ["ApiError", "error_response", "register_error_handlers", "request_id"]
