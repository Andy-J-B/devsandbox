"""Unit tests for DevSandbox test fakes against the shared protocols."""

from __future__ import annotations

from tests.fakes import (
    FakeAgentAdapter,
    FakeContextManager,
    FakeGitManager,
    FakePlanner,
    FakeResourceMonitor,
    FakeSandboxManager,
    FakeValidator,
)

from devsandbox.agents.interfaces import AgentAdapter
from devsandbox.context.interfaces import ContextManager
from devsandbox.gitmgr.interfaces import GitManager
from devsandbox.planner.interfaces import Planner
from devsandbox.sandbox.interfaces import SandboxManager
from devsandbox.scheduler.interfaces import ResourceMonitor
from devsandbox.validation.interfaces import Validator


def test_fake_sandbox_manager_conforms() -> None:
    assert isinstance(FakeSandboxManager(), SandboxManager)


def test_fake_git_manager_conforms() -> None:
    assert isinstance(FakeGitManager(), GitManager)


def test_fake_agent_adapter_conforms() -> None:
    assert isinstance(FakeAgentAdapter(), AgentAdapter)


def test_fake_context_manager_conforms() -> None:
    assert isinstance(FakeContextManager(), ContextManager)


def test_fake_planner_conforms() -> None:
    assert isinstance(FakePlanner(), Planner)


def test_fake_validator_conforms() -> None:
    assert isinstance(FakeValidator(), Validator)


def test_fake_resource_monitor_conforms() -> None:
    assert isinstance(FakeResourceMonitor(), ResourceMonitor)


def test_fake_resource_monitor_sample() -> None:
    import asyncio

    monitor = FakeResourceMonitor()

    async def run() -> object:
        return await monitor.sample()

    result = asyncio.run(run())
    assert result is not None
