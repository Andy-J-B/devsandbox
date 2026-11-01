"""Tests for secret resolution, permission checks, and redaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.agent_helpers import make_settings

from devsandbox.agents.secrets import MODEL_API_KEY_ENV, SecretProvider, provider_secret_env
from devsandbox.common.errors import ConfigError
from devsandbox.common.redaction import Redactor, reset_redactor
from devsandbox.common.types import ModelConfig
from devsandbox.config.settings import SecretRef


@pytest.fixture(autouse=True)
def _clean_redactor():
    reset_redactor()
    yield
    reset_redactor()


def test_resolve_from_env_and_register_with_redactor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_MODEL_KEY", "sk-canary-value-1234567890")
    redactor = Redactor()
    provider = SecretProvider(make_settings(), redactor=redactor)

    secret = provider.resolve(SecretRef(env_var="MY_MODEL_KEY"))

    assert secret.get_secret_value() == "sk-canary-value-1234567890"
    assert "sk-canary-value-1234567890" not in str(
        redactor.redact("key=sk-canary-value-1234567890")
    )


def test_resolve_named_settings_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAMED_KEY", "named-secret-value")
    settings = make_settings(secrets={"model": SecretRef(env_var="NAMED_KEY")})
    provider = SecretProvider(settings)
    assert provider.resolve("model").get_secret_value() == "named-secret-value"


def test_resolve_file_requires_private_permissions(tmp_path: Path) -> None:
    secret_file = tmp_path / "key"
    secret_file.write_text("file-secret-value\n", encoding="utf-8")
    secret_file.chmod(0o600)
    provider = SecretProvider(make_settings())
    assert provider.resolve(SecretRef(path=str(secret_file))).get_secret_value() == (
        "file-secret-value"
    )

    secret_file.chmod(0o644)
    with pytest.raises(ConfigError):
        provider.resolve(SecretRef(path=str(secret_file)))


def test_missing_secret_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    provider = SecretProvider(make_settings())
    with pytest.raises(ConfigError):
        provider.resolve(SecretRef(env_var="DOES_NOT_EXIST"))
    with pytest.raises(ConfigError):
        provider.resolve(SecretRef())


def test_secret_env_for_model_passes_only_named_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_KEY", "openrouter-secret")
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    settings = make_settings(secrets={"model": SecretRef(env_var="OPENROUTER_KEY")})
    provider = SecretProvider(settings)
    model = ModelConfig(provider="openrouter", model="free/model", secret_ref="model")

    env = provider.secret_env_for(model)

    assert list(env) == ["OPENROUTER_API_KEY"]
    assert env["OPENROUTER_API_KEY"].get_secret_value() == "openrouter-secret"
    assert "UNRELATED_HOST_SECRET" not in env
    assert os.environ["UNRELATED_HOST_SECRET"] == "must-not-leak"


def test_unknown_provider_uses_generic_key_env() -> None:
    assert provider_secret_env("openrouter") == "OPENROUTER_API_KEY"
    assert provider_secret_env("some-custom-endpoint") == MODEL_API_KEY_ENV


def test_model_without_secret_ref_leaks_nothing() -> None:
    provider = SecretProvider(make_settings())
    assert provider.secret_env_for(ModelConfig(provider="ollama", model="llama3")) == {}
