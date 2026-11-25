"""API bearer-token file loading and SIGHUP reload behaviour."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from tests.api_helpers import make_settings

from devsandbox.api.app import create_app
from devsandbox.api.tokens import (
    TokenStore,
    read_token_file,
    tokens_from_settings,
)
from devsandbox.common.errors import ConfigError
from devsandbox.config.settings import DevSandboxSettings


def _values(tokens: tuple[SecretStr, ...]) -> list[str]:
    return [token.get_secret_value() for token in tokens]


def test_token_file_ignores_blanks_comments_and_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "api-tokens"
    source.write_text(
        "# rotation window\n\nnew-token  \nold-token\nnew-token\n",
        encoding="utf-8",
    )

    assert _values(read_token_file(source)) == ["new-token", "old-token"]


def test_token_file_must_be_readable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read API token file"):
        read_token_file(tmp_path / "missing")


def test_settings_and_file_tokens_are_combined(tmp_path: Path) -> None:
    source = tmp_path / "api-tokens"
    source.write_text("file-token\nprimary\n", encoding="utf-8")
    settings = DevSandboxSettings(
        _env_file=None,
        api={
            "token": SecretStr("primary"),
            "additional_tokens": [SecretStr("old-token")],
            "token_file": str(source),
        },
    )

    assert _values(tokens_from_settings(settings)) == [
        "primary",
        "old-token",
        "file-token",
    ]


def test_token_store_requires_an_initial_token() -> None:
    with pytest.raises(ConfigError, match="DEVSANDBOX_API_TOKEN"):
        TokenStore(())


def test_token_store_reload_replaces_tokens() -> None:
    current = ["primary"]
    store = TokenStore([SecretStr("primary")], loader=lambda: (SecretStr(current[0]),))

    current[0] = "rotated"
    result = store.reload()

    assert result.accepted is True
    assert result.token_count == 1
    assert _values(store.tokens) == ["rotated"]


def test_token_store_reload_failure_keeps_last_good_tokens() -> None:
    def _broken() -> tuple[SecretStr, ...]:
        raise ConfigError("token file disappeared")

    store = TokenStore([SecretStr("primary")], loader=_broken)

    result = store.reload()

    assert result.accepted is False
    assert _values(store.tokens) == ["primary"]


def test_token_store_empty_reload_keeps_last_good_tokens() -> None:
    store = TokenStore([SecretStr("primary")], loader=lambda: ())

    result = store.reload()

    assert result.accepted is False
    assert "keeping 1 token" in result.detail
    assert _values(store.tokens) == ["primary"]


async def test_rotation_without_restart_changes_accepted_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "api-tokens"
    source.write_text("old-token\nnew-token\n", encoding="utf-8")
    settings = make_settings(str(tmp_path / "data"))
    settings = settings.model_copy(
        update={
            "api": settings.api.model_copy(
                update={"token": None, "additional_tokens": [], "token_file": str(source)}
            )
        }
    )
    monkeypatch.setenv("DEVSANDBOX_API__TOKEN_FILE", str(source))
    monkeypatch.delenv("DEVSANDBOX_API_TOKEN", raising=False)
    monkeypatch.delenv("DEVSANDBOX_API__TOKEN", raising=False)

    app = create_app(SimpleNamespace(settings=settings, version="test"))  # type: ignore[arg-type]

    @app.get("/v1/auth-probe")
    async def _probe() -> dict[str, bool]:
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        old = await client.get("/v1/auth-probe", headers={"Authorization": "Bearer old-token"})
        assert old.status_code == 200

        source.write_text("new-token\n", encoding="utf-8")
        store = app.state.token_store
        assert store.reload().accepted is True

        retired = await client.get("/v1/auth-probe", headers={"Authorization": "Bearer old-token"})
        assert retired.status_code == 401
        new = await client.get("/v1/auth-probe", headers={"Authorization": "Bearer new-token"})
        assert new.status_code == 200
