"""Small route helpers shared by API modules."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from ..container import AppContainer
from ..errors import ApiError


def get_container(request: Request) -> AppContainer:
    """Return the injected container or fail with a typed 503."""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise ApiError(
            503,
            "service_unavailable",
            "API container is not configured; start the server with devsandbox serve",
        )
    return cast(AppContainer, container)


__all__ = ["get_container"]
