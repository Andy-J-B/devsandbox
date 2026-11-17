"""Unit tests for bearer-token comparison helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from devsandbox.api.app import _matches_any_token, create_app
from devsandbox.common.errors import ConfigError
from devsandbox.config.settings import DevSandboxSettings


def test_matches_any_rotation_token() -> None:
    tokens = (SecretStr("primary-token"), SecretStr("old-token"))
    assert _matches_any_token("primary-token", tokens)
    assert _matches_any_token("old-token", tokens)
    assert not _matches_any_token("retired-token", tokens)


def test_token_comparison_handles_non_ascii_without_raising() -> None:
    token = SecretStr("sëcret-tökén")
    assert _matches_any_token("sëcret-tökén", (token,))
    assert not _matches_any_token("sëcret-token", (token,))


def test_no_tokens_never_matches() -> None:
    assert not _matches_any_token("anything", ())


def test_app_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVSANDBOX_API_TOKEN", raising=False)
    monkeypatch.delenv("DEVSANDBOX_API__TOKEN", raising=False)
    settings = DevSandboxSettings(_env_file=None, api={"token": None})
    with pytest.raises(ConfigError, match="DEVSANDBOX_API_TOKEN"):
        create_app(SimpleNamespace(settings=settings))  # type: ignore[arg-type]
