"""Tests for selecting the configured agent runtime."""

from __future__ import annotations

import pytest
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.agents.fake import FakeScriptAdapter
from devsandbox.agents.opencode import OpenCodeAdapter
from devsandbox.api.bootstrap import _make_agent
from devsandbox.config.settings import AgentConfig, DevSandboxSettings


def _settings(runtime: str) -> DevSandboxSettings:
    return DevSandboxSettings(agent=AgentConfig(default_runtime=runtime))


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ("opencode", OpenCodeAdapter),
        ("fake-script", FakeScriptAdapter),
        (" OpenCode ", OpenCodeAdapter),
    ],
)
def test_configured_runtime_selects_adapter(runtime: str, expected: type[object]) -> None:
    agent = _make_agent(FakeSandboxManager(), _settings(runtime))
    assert isinstance(agent, expected)


def test_unknown_runtime_fails_closed() -> None:
    with pytest.raises(ValueError, match=r"unsupported agent\.default_runtime"):
        _make_agent(FakeSandboxManager(), _settings("not-a-runtime"))
