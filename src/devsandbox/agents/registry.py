"""Agent adapter registry and capability descriptors.

Adapters register themselves by name (e.g. ``"opencode"``).  The registry is
the single place an orchestrator looks up an adapter for a requested runtime;
an unknown runtime raises :class:`~devsandbox.common.errors.ConfigError`.

Adding a new runtime is intentionally small: implement a class with ``name``,
``run`` and ``cancel`` and register an instance.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.errors import ConfigError
from .interfaces import AgentAdapter


@dataclass(frozen=True)
class AgentCapabilities:
    """Capabilities advertised by an agent runtime adapter."""

    supports_non_interactive: bool = False
    supports_model_selection: bool = False
    emits_token_usage: bool = False


class AgentRegistry:
    """Registry of agent adapters keyed by runtime name."""

    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        self._capabilities: dict[str, AgentCapabilities] = {}

    def register(
        self,
        name: str,
        adapter: AgentAdapter,
        capabilities: AgentCapabilities | None = None,
    ) -> None:
        """Register ``adapter`` under ``name``.

        Re-registering a name replaces the previous adapter (useful in tests
        and for plugins); callers that need strictness can check ``names()``.
        """
        if not name:
            raise ConfigError("agent adapter name must not be empty")
        self._adapters[name] = adapter
        self._capabilities[name] = capabilities or AgentCapabilities()

    def get(self, name: str) -> AgentAdapter:
        """Return the adapter registered under ``name``.

        Raises:
            ConfigError: when no adapter is registered for ``name``.
        """
        if name not in self._adapters:
            known = ", ".join(self.names()) or "<none>"
            raise ConfigError(f"unknown agent runtime: {name!r} (known: {known})")
        return self._adapters[name]

    def capabilities(self, name: str) -> AgentCapabilities:
        """Return the capabilities for the adapter registered under ``name``."""
        if name not in self._capabilities:
            raise ConfigError(f"unknown agent runtime: {name!r}")
        return self._capabilities[name]

    def names(self) -> list[str]:
        """Return the registered runtime names (sorted, deterministic)."""
        return sorted(self._adapters)


# Process-wide default registry used by the orchestrator and tests.
default_registry = AgentRegistry()


__all__ = ["AgentCapabilities", "AgentRegistry", "default_registry"]
