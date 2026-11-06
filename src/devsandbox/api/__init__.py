"""FastAPI application factory and dependency container (plan 10)."""

from __future__ import annotations

from .app import create_app
from .container import AppContainer, EngineController

__all__ = ["AppContainer", "EngineController", "create_app"]
