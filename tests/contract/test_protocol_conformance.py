"""Every Protocol in 00 §6 must be satisfied by both real and fake impls.

``runtime_checkable`` protocols check method presence; the semantic suites in
``test_contract_semantics.py`` cover ordering, idempotency, and return types.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fakes import (
    FakeAgentAdapter,
    FakeContextManager,
    FakeGitManager,
    FakePlanner,
    FakeResourceMonitor,
    FakeSandboxManager,
    FakeScheduler,
    FakeValidator,
)

from devsandbox.agents.fake import FakeScriptAdapter
from devsandbox.agents.interfaces import AgentAdapter
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.context.interfaces import ContextManager
from devsandbox.context.manager import ContextManagerImpl
from devsandbox.context.store import ArtifactStore
from devsandbox.gitmgr.interfaces import GitManager
from devsandbox.gitmgr.manager import GitManagerImpl
from devsandbox.planner.interfaces import Planner
from devsandbox.planner.templates import TemplatePlanner
from devsandbox.sandbox.interfaces import SandboxManager
from devsandbox.sandbox.manager import DockerSandboxManager
from devsandbox.scheduler.interfaces import ResourceMonitor, Scheduler
from devsandbox.scheduler.monitor import PsutilResourceMonitor
from devsandbox.scheduler.scheduler import DeterministicScheduler
from devsandbox.validation.interfaces import Validator
from devsandbox.validation.validator import ValidatorImpl

pytestmark = pytest.mark.contract


class _NullArtifactRepository:
    async def create(self, artifact):  # type: ignore[no-untyped-def]
        return artifact

    async def get(self, artifact_id):  # type: ignore[no-untyped-def]
        return None

    async def list_for_project(self, project_id, kind=None):  # type: ignore[no-untyped-def]
        return []

    async def latest_for_project(self, project_id, kind):  # type: ignore[no-untyped-def]
        return None

    async def mark_stale(self, artifact_id, stale=True):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def settings(tmp_path: Path) -> DevSandboxSettings:
    return DevSandboxSettings(data_root=str(tmp_path / "data"))


def _real_sandbox(settings: DevSandboxSettings, tmp_path: Path) -> DockerSandboxManager:
    return DockerSandboxManager(settings, tmp_path, client=object())


def test_git_manager_protocol() -> None:
    assert isinstance(FakeGitManager(), GitManager)
    assert isinstance(GitManagerImpl(DevSandboxSettings(), Path("/tmp/contract-git")), GitManager)


def test_agent_adapter_protocol(settings: DevSandboxSettings) -> None:
    fake_sandbox = FakeSandboxManager()
    assert isinstance(FakeAgentAdapter(), AgentAdapter)
    assert isinstance(FakeScriptAdapter(fake_sandbox, settings), AgentAdapter)


def test_context_manager_protocol() -> None:
    assert isinstance(FakeContextManager(), ContextManager)
    artifacts = ArtifactStore(_NullArtifactRepository())
    assert isinstance(ContextManagerImpl(lambda project_id: None, artifacts), ContextManager)


def test_planner_protocol() -> None:
    assert isinstance(FakePlanner(), Planner)
    assert isinstance(TemplatePlanner(), Planner)


def test_scheduler_protocol() -> None:
    assert isinstance(FakeScheduler(), Scheduler)
    assert isinstance(DeterministicScheduler(), Scheduler)


def test_resource_monitor_protocol(tmp_path: Path) -> None:
    assert isinstance(FakeResourceMonitor(), ResourceMonitor)
    assert isinstance(PsutilResourceMonitor(data_root=tmp_path), ResourceMonitor)


def test_validator_protocol() -> None:
    assert isinstance(FakeValidator(), Validator)
    assert isinstance(ValidatorImpl(FakeSandboxManager(), FakeGitManager()), Validator)


def test_sandbox_manager_protocol(settings: DevSandboxSettings, tmp_path: Path) -> None:
    assert isinstance(FakeSandboxManager(), SandboxManager)
    assert isinstance(_real_sandbox(settings, tmp_path), SandboxManager)
