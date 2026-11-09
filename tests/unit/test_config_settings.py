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
