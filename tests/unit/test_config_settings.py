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
