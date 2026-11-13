"""Unit tests for DevSandbox settings and security policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devsandbox.common.types import NetworkPolicy, ResourceProfile
from devsandbox.config.policy import SecurityPolicy, is_digest_pinned
from devsandbox.config.settings import (
    AgentConfig,
    DevSandboxSettings,
    RetriesConfig,
    SecurityConfig,
    ValidationSettings,
)
from devsandbox.validation.config import ValidationConfig

DIGEST_IMAGE = "registry.example/devsandbox/agent@sha256:" + "a" * 64
LOCAL_DIGEST_IMAGE = "sha256:" + "b" * 64


def test_settings_defaults() -> None:
    settings = DevSandboxSettings()
    assert settings.limits.max_global_concurrency >= 1
    assert settings.retries.default_max_retries >= 0
    assert settings.security.allow_push is False


def test_settings_rejects_removed_main_merge_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main-merge path does not exist; the setting must fail closed."""
    monkeypatch.setenv("DEVSANDBOX_SECURITY__ALLOW_MAIN_MERGE", "true")
    with pytest.raises(ValidationError, match="allow_main_merge"):
        DevSandboxSettings(_env_file=None)


def test_settings_rejects_negative_retries() -> None:
    with pytest.raises(ValidationError):
        RetriesConfig(default_max_retries=-1)


def test_policy_allows_push_only_when_configured() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    assert policy.push_allowed() is False


def test_agent_image_defaults_and_can_be_overridden() -> None:
    assert DevSandboxSettings().agent.image == "devsandbox/agent:latest"
    settings = DevSandboxSettings(agent=AgentConfig(image=DIGEST_IMAGE))
    assert settings.agent.image == DIGEST_IMAGE


def test_digest_pinning_detection() -> None:
    assert is_digest_pinned(DIGEST_IMAGE)
    assert is_digest_pinned(LOCAL_DIGEST_IMAGE)
    assert not is_digest_pinned("devsandbox/agent:latest")
    assert not is_digest_pinned("registry.example/devsandbox/agent")


def test_image_pinning_is_optional_for_local_development() -> None:
    policy = SecurityPolicy(DevSandboxSettings())
    assert policy.image_violation("devsandbox/agent:latest") is None


def test_image_pinning_rejects_tags_when_required() -> None:
    settings = DevSandboxSettings(security=SecurityConfig(require_pinned_images=True))
    policy = SecurityPolicy(settings)

    violation = policy.image_violation("devsandbox/agent:latest")

    assert violation is not None
    assert "not pinned by digest" in violation
    assert policy.image_violation(DIGEST_IMAGE) is None
    assert policy.image_violation(LOCAL_DIGEST_IMAGE) is None


def test_image_allowlist_is_checked_before_pinning() -> None:
    settings = DevSandboxSettings(
        security=SecurityConfig(
            require_pinned_images=True,
            allowed_images=[DIGEST_IMAGE],
        )
    )
    violation = SecurityPolicy(settings).image_violation("evil:latest")

    assert violation is not None
    assert "not allowed" in violation


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


def test_validation_settings_reject_bad_limits() -> None:
    with pytest.raises(ValidationError):
        ValidationSettings(max_output_bytes=0)
    with pytest.raises(ValidationError):
        ValidationSettings(assert_removal_threshold=-1)


def test_validation_config_from_settings_and_overrides() -> None:
    settings = DevSandboxSettings(
        validation=ValidationSettings(
            image="custom/validator:1",
            max_output_bytes=1024,
            tamper_mode="warn",
        )
    )
    config = ValidationConfig.from_settings(settings, max_output_bytes=2048)
    assert config.image == "custom/validator:1"
    assert config.max_output_bytes == 2048
    assert config.tamper_mode == "warn"
    assert config.network.mode == "none"
