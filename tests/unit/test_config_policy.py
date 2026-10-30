"""Unit tests for DevSandbox settings and security policy."""

from __future__ import annotations

from devsandbox.common.types import NetworkPolicy, ResourceProfile
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import DevSandboxSettings, RetriesConfig


def test_settings_defaults() -> None:
    settings = DevSandboxSettings()
    assert settings.limits.max_global_concurrency >= 1
    assert settings.retries.default_max_retries >= 0
    assert settings.security.allow_main_merge is False
    assert settings.security.allow_push is False


def test_settings_rejects_negative_retries() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RetriesConfig(default_max_retries=-1)


def test_policy_never_allows_main_merge_by_default() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    assert policy.main_merge_allowed() is False


def test_policy_allows_push_only_when_configured() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    assert policy.push_allowed() is False


def test_policy_max_retries_caps_request() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    assert policy.max_retries(10) == 2


def test_effective_network_narrows_to_none() -> None:
    settings = DevSandboxSettings()
    settings.security.allow_network_default = False
    policy = SecurityPolicy(settings)
    effective = policy.effective_network(NetworkPolicy(mode="full", allowed_hosts=["example.com"]))
    assert effective.mode == "none"
    assert effective.allowed_hosts == []


def test_effective_network_allowlist_honors_request() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    effective = policy.effective_network(
        NetworkPolicy(mode="allowlist", allowed_hosts=["api.openai.com"])
    )
    assert effective.mode == "allowlist"
    assert effective.allowed_hosts == ["api.openai.com"]


def test_effective_resources_clamped_to_policy_maxima() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    effective = policy.effective_resources(
        ResourceProfile(cpu_cores=99.0, memory_mb=99999, disk_mb=99999, pids_limit=9999)
    )
    assert effective.cpu_cores <= SecurityPolicy.MAX_CPU_CORES
    assert effective.memory_mb <= SecurityPolicy.MAX_MEMORY_MB
    assert effective.disk_mb <= SecurityPolicy.MAX_DISK_MB
    assert effective.pids_limit <= SecurityPolicy.MAX_PIDS_LIMIT
