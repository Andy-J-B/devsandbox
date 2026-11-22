"""Startup wiring: migrations run before the container and HTTP server start."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.api_helpers import make_settings

from devsandbox.api import server


def _settings(tmp_path: Path, *, auto_upgrade: bool) -> Any:
    settings = make_settings(str(tmp_path / "data"))
    return settings.model_copy(
        update={
            "migrations": settings.migrations.model_copy(
                update={"auto_upgrade": auto_upgrade, "lock_timeout_seconds": 7.0}
            )
        }
    )


def test_run_applies_startup_migrations_before_building_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object]] = []
    settings = _settings(tmp_path, auto_upgrade=True)
    fake_container = object()

    monkeypatch.setattr(
        "devsandbox.db.migrations_runner.run_startup_migrations",
        lambda url, *, lock_timeout_seconds: calls.append(("migrate", (url, lock_timeout_seconds))),
    )
    monkeypatch.setattr(
        server,
        "build_container",
        lambda resolved: calls.append(("build", resolved)) or fake_container,
    )
    monkeypatch.setattr(
        server,
        "create_server_app",
        lambda container: calls.append(("app", container)) or object(),
    )
    monkeypatch.setattr(
        server.uvicorn, "run", lambda *args, **kwargs: calls.append(("serve", args))
    )

    server.run(settings)

    assert [name for name, _ in calls] == ["migrate", "build", "app", "serve"]
    assert calls[0][1] == (settings.database_url, 7.0)


def test_run_can_skip_startup_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    settings = _settings(tmp_path, auto_upgrade=False)

    monkeypatch.setattr(
        "devsandbox.db.migrations_runner.run_startup_migrations",
        lambda *args, **kwargs: calls.append("migrate"),
    )
    monkeypatch.setattr(
        server, "build_container", lambda resolved: calls.append("build") or object()
    )
    monkeypatch.setattr(
        server, "create_server_app", lambda container: calls.append("app") or object()
    )
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: calls.append("serve"))

    server.run(settings)

    assert calls == ["build", "app", "serve"]
