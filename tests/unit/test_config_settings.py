"""Settings loading tests (dotenv filtering)."""

from __future__ import annotations

from pathlib import Path

import pytest

from devsandbox.config.settings import DevSandboxSettings


def test_dotenv_ignores_unprefixed_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVSANDBOX_DATA_ROOT", raising=False)
    (tmp_path / ".env").write_text(
        "OPENCODE_API_KEY=not-a-devsandbox-setting\n"
        "DEVSANDBOX_DATA_ROOT=/tmp/devsandbox-test-root\n",
        encoding="utf-8",
    )

    settings = DevSandboxSettings()

    assert settings.data_root == "/tmp/devsandbox-test-root"


def test_nested_env_uses_double_underscore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVSANDBOX_API__PORT", "8123")
    monkeypatch.setenv("DEVSANDBOX_AGENT__DEFAULT_MODEL", "ollama/qwen3:8b")
    monkeypatch.setenv("DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT", "true")

    settings = DevSandboxSettings(_env_file=None)

    assert settings.api.port == 8123
    assert settings.agent.default_model == "ollama/qwen3:8b"
    assert settings.security.allow_network_default is True


def test_api_rotation_tokens_parse_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVSANDBOX_API_TOKEN", "primary-token")
    monkeypatch.setenv(
        "DEVSANDBOX_API__ADDITIONAL_TOKENS",
        '["old-token-1", "old-token-2"]',
    )

    settings = DevSandboxSettings(_env_file=None)

    assert [token.get_secret_value() for token in settings.effective_api_tokens] == [
        "primary-token",
        "old-token-1",
        "old-token-2",
    ]
    assert settings.effective_api_token is not None
    assert settings.effective_api_token.get_secret_value() == "primary-token"


def test_effective_api_tokens_ignores_empty_and_duplicate_values() -> None:
    from pydantic import SecretStr

    settings = DevSandboxSettings(
        _env_file=None,
        api={
            "token": SecretStr("primary"),
            "additional_tokens": [
                SecretStr("primary"),
                SecretStr(""),
                SecretStr("old"),
            ],
        },
    )

    assert [token.get_secret_value() for token in settings.effective_api_tokens] == [
        "primary",
        "old",
    ]
    assert settings.effective_api_token is not None
    assert settings.effective_api_token.get_secret_value() == "primary"


def test_git_blob_cap_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVSANDBOX_GIT__MAX_BLOB_BYTES", "2048")

    settings = DevSandboxSettings(_env_file=None)

    assert settings.git.max_blob_bytes == 2048


def test_git_blob_cap_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_blob_bytes"):
        DevSandboxSettings(_env_file=None, git={"max_blob_bytes": 0})
