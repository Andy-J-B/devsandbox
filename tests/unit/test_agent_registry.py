"""Tests for the agent adapter registry and extensibility."""

from __future__ import annotations

import pytest

from devsandbox.agents.interfaces import AgentAdapter, AgentRunRequest, AgentRunResult
from devsandbox.agents.registry import AgentCapabilities, AgentRegistry
from devsandbox.common.errors import ConfigError
from devsandbox.common.types import AttemptId


class EchoAdapter:
    """Minimal second runtime: demonstrates the extension point."""

    name = "echo"

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        return AgentRunResult(exit_status="exited_ok", summary=f"echo: {req.task.title}")

    async def cancel(self, attempt_id: AttemptId) -> None:
        return None


def test_registry_get_and_capabilities() -> None:
    registry = AgentRegistry()
    adapter = EchoAdapter()
    registry.register(
        "echo",
        adapter,
        AgentCapabilities(
            supports_non_interactive=True,
            supports_model_selection=False,
            emits_token_usage=False,
        ),
    )
    assert registry.get("echo") is adapter
    assert registry.names() == ["echo"]
    capabilities = registry.capabilities("echo")
    assert capabilities.supports_non_interactive is True
    assert capabilities.emits_token_usage is False


def test_registry_unknown_runtime_is_config_error() -> None:
    registry = AgentRegistry()
    with pytest.raises(ConfigError):
        registry.get("missing")
    with pytest.raises(ConfigError):
        registry.capabilities("missing")


def test_registry_rejects_empty_name() -> None:
    with pytest.raises(ConfigError):
        AgentRegistry().register("", EchoAdapter())


def test_echo_adapter_conforms_and_can_be_registered() -> None:
    registry = AgentRegistry()
    adapter = EchoAdapter()
    assert isinstance(adapter, AgentAdapter)
    registry.register("echo", adapter)
    assert isinstance(registry.get("echo"), AgentAdapter)
