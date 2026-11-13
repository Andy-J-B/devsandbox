"""Policy non-escalation: settings, not specs/agents, decide the posture.

Every test here runs without Docker.  These are the policy checks that must
reject or clamp *before* any external side effect happens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devsandbox.common.errors import PolicyViolation
from devsandbox.common.types import (
    AttemptId,
    NetworkPolicy,
    RepoHandle,
    ResourceProfile,
)
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import DevSandboxSettings, SecurityConfig
from devsandbox.gitmgr.manager import GitManagerImpl
from devsandbox.sandbox.interfaces import SandboxRequest
from devsandbox.sandbox.manager import DockerSandboxManager
from devsandbox.spec.models import _check_no_secret_literals

pytestmark = pytest.mark.security


def _settings(**security: object) -> DevSandboxSettings:
    return DevSandboxSettings(
        data_root="/tmp/devsandbox-security",
        security=SecurityConfig(**security),
    )


def test_network_defaults_to_off() -> None:
    """The authoritative default is offline unless explicitly enabled."""
    settings = DevSandboxSettings()
    assert settings.security.allow_network_default is False
    policy = SecurityPolicy(settings)
    assert policy.effective_network(NetworkPolicy(mode="full")).mode == "none"


def test_network_can_only_be_narrowed_by_policy() -> None:
    denied = SecurityPolicy(_settings(allow_network_default=False))
    allowed = SecurityPolicy(_settings(allow_network_default=True))

    assert denied.effective_network(NetworkPolicy(mode="none")).mode == "none"
    assert denied.effective_network(NetworkPolicy(mode="full")).mode == "none"
    assert allowed.effective_network(NetworkPolicy(mode="full")).mode == "full"
    # Allowlist mode is preserved here; the sandbox manager still refuses it
    # rather than silently widening it to full network access.
    assert allowed.effective_network(
        NetworkPolicy(mode="allowlist", allowed_hosts=["example.com"])
    ).allowed_hosts == ["example.com"]


def test_resources_are_clamped_to_policy_maxima() -> None:
    policy = SecurityPolicy(_settings())
    effective = policy.effective_resources(
        ResourceProfile(cpu_cores=64.0, memory_mb=1_000_000, disk_mb=1_000_000, pids_limit=99_999)
    )
    assert effective.cpu_cores == SecurityPolicy.MAX_CPU_CORES
    assert effective.memory_mb == SecurityPolicy.MAX_MEMORY_MB
    assert effective.disk_mb == SecurityPolicy.MAX_DISK_MB
    assert effective.pids_limit == SecurityPolicy.MAX_PIDS_LIMIT


def _request(tmp_path: Path, *, image: str, network: NetworkPolicy) -> SandboxRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SandboxRequest(
        attempt_id=AttemptId("policy-attempt"),
        image=image,
        workspace_path=workspace,
        command=["true"],
        resources=ResourceProfile(cpu_cores=1.0, memory_mb=128, pids_limit=16),
        network=network,
        timeout_seconds=5,
    )


def test_sandbox_rejects_allowlist_instead_of_widening(tmp_path: Path) -> None:
    manager = DockerSandboxManager(_settings(), tmp_path, client=object())
    with pytest.raises(PolicyViolation, match="allowlist"):
        _run(manager, _request(tmp_path, image="good:img", network=NetworkPolicy(mode="allowlist")))


def test_sandbox_rejects_unlisted_image_before_docker(tmp_path: Path) -> None:
    settings = _settings(allowed_images=["good:img"])
    manager = DockerSandboxManager(settings, tmp_path, client=object())
    with pytest.raises(PolicyViolation, match="image not allowed"):
        _run(manager, _request(tmp_path, image="evil:img", network=NetworkPolicy(mode="none")))


def test_sandbox_rejects_unpinned_image_before_docker(tmp_path: Path) -> None:
    settings = _settings(require_pinned_images=True)
    manager = DockerSandboxManager(settings, tmp_path, client=object())
    with pytest.raises(PolicyViolation, match="not pinned by digest"):
        _run(
            manager,
            _request(tmp_path, image="devsandbox/agent:latest", network=NetworkPolicy(mode="none")),
        )


def _run(manager: DockerSandboxManager, request: SandboxRequest) -> None:
    """Run a request expected to fail policy checks before touching Docker."""
    import asyncio

    asyncio.run(manager.run(request))


async def test_push_is_denied_by_default(tmp_path: Path) -> None:
    settings = _settings()
    manager = GitManagerImpl(settings, tmp_path / "data")
    repo = RepoHandle(
        project_id="p1",
        path=str(tmp_path / "repo.git"),
        url="file:///tmp/remote.git",
        staging_branch="devsandbox/p1/staging",
    )

    result = await manager.push_branch(repo, "devsandbox/p1/staging")

    assert result.pushed is False
    assert "allow_push" in result.detail


async def test_push_never_allows_main_even_when_enabled(tmp_path: Path) -> None:
    settings = _settings(allow_push=True)
    manager = GitManagerImpl(settings, tmp_path / "data")
    repo = RepoHandle(
        project_id="p1",
        path=str(tmp_path / "repo.git"),
        url="file:///tmp/remote.git",
        staging_branch="devsandbox/p1/staging",
    )

    main = await manager.push_branch(repo, "main")
    arbitrary = await manager.push_branch(repo, "feature/anything")

    assert main.pushed is False
    assert "main" in main.detail
    assert arbitrary.pushed is False
    assert "non-devsandbox" in arbitrary.detail


def test_autonomous_escalation_is_policy_controlled() -> None:
    assert SecurityPolicy(_settings()).autonomous_mode_allowed() is True
    assert SecurityPolicy(_settings(allow_autonomous_mode=False)).autonomous_mode_allowed() is False


def test_spec_literal_secret_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="looks like a secret value"):
        _check_no_secret_literals({"agent": {"api_key": "sk-live-secret"}}, "spec.")
    with pytest.raises(ValueError, match="looks like a secret value"):
        _check_no_secret_literals([{"password": "hunter2"}], "spec.")
    # Nested non-secret content is allowed.
    _check_no_secret_literals({"agent": {"model": "gpt-4o"}}, "spec.")
