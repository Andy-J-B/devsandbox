"""Unit tests for PostgreSQL backup/restore helpers (no real database)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from devsandbox.common.errors import ConfigError, DatabaseBackupError
from devsandbox.db import backup as backup_module
from devsandbox.db.backup import (
    BackupManifest,
    backup_database,
    manifest_path_for,
    parse_postgres_url,
    restore_database,
)

URL = "postgresql+asyncpg://dbuser:topsecret@db.example:5433/devsandbox"


def test_parse_postgres_url_and_sanitized_url() -> None:
    target = parse_postgres_url(URL)

    assert target.host == "db.example"
    assert target.port == 5433
    assert target.username == "dbuser"
    assert target.database == "devsandbox"
    assert target.driver == "asyncpg"
    assert "topsecret" not in target.sanitized_url
    assert "db.example" in target.sanitized_url


def test_sanitize_database_url_hides_password() -> None:
    rendered = backup_module.sanitize_database_url(URL)

    assert "topsecret" not in rendered
    assert "db.example" in rendered
    assert rendered.startswith("postgresql+asyncpg://")
    assert backup_module.sanitize_database_url("not a url") == "<invalid database URL>"


def test_query_options_are_forwarded_as_libpq_environment() -> None:
    target = parse_postgres_url(URL + "?sslmode=require&connect_timeout=5")
    env = backup_module._postgres_env(target, None)

    assert env["PGSSLMODE"] == "require"
    assert env["PGCONNECT_TIMEOUT"] == "5"
    assert "topsecret" not in repr(env)


def test_parse_postgres_url_rejects_other_backends() -> None:
    with pytest.raises(ConfigError, match="PostgreSQL"):
        parse_postgres_url("sqlite+aiosqlite:///devsandbox.db")


async def test_backup_uses_private_passfile_and_keeps_password_out_of_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def _fake_run(args: list[str], *, env: dict[str, str], password: str | None = None) -> None:
        observed["args"] = list(args)
        observed["env"] = dict(env)
        passfile = Path(env["PGPASSFILE"])
        observed["passfile_exists"] = passfile.exists()
        observed["passfile_mode"] = passfile.stat().st_mode & 0o777
        observed["passfile_text"] = passfile.read_text(encoding="utf-8")
        destination = Path(args[args.index("--file") + 1])
        destination.write_bytes(b"custom dump")

    async def _revision(_url: str) -> str:
        return "0007"

    monkeypatch.setattr(backup_module, "_tool", lambda name: f"/fake/{name}")
    monkeypatch.setattr(backup_module, "_run_tool", _fake_run)
    monkeypatch.setattr(backup_module, "current_alembic_revision", _revision)

    result = await backup_database(
        URL,
        tmp_path / "backup.dump",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )

    args = observed["args"]
    assert isinstance(args, list)
    assert "/fake/pg_dump" in args
    assert "topsecret" not in " ".join(args)
    env = observed["env"]
    assert isinstance(env, dict)
    assert "PGPASSWORD" not in env
    assert env["PGDATABASE"] == "devsandbox"
    assert observed["passfile_exists"] is True
    assert observed["passfile_mode"] == 0o600
    passfile_text = observed["passfile_text"]
    assert isinstance(passfile_text, str)
    assert "topsecret" in passfile_text
    # The temporary passfile is gone after the subprocess returns.
    assert not Path(str(env["PGPASSFILE"])).exists()

    assert Path(result.path) == tmp_path / "backup.dump"
    assert Path(result.path).read_bytes() == b"custom dump"
    assert result.manifest.alembic_revision == "0007"
    assert result.manifest.bytes == len(b"custom dump")
    assert result.manifest.sha256
    assert Path(result.manifest_path) == manifest_path_for(result.path)
    assert Path(result.manifest_path).is_file()


def test_backup_refuses_to_overwrite_without_flag(tmp_path: Path) -> None:
    destination = tmp_path / "backup.dump"
    destination.write_bytes(b"existing")

    with pytest.raises(ConfigError, match="already exists"):
        import asyncio

        asyncio.run(backup_database(URL, destination))


def test_restore_missing_source_is_rejected(tmp_path: Path) -> None:
    import asyncio

    with pytest.raises(ConfigError, match="not found"):
        asyncio.run(restore_database(URL, tmp_path / "missing.dump"))


def test_restore_rejects_checksum_mismatch(tmp_path: Path) -> None:
    import asyncio

    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"tampered")
    manifest = BackupManifest(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        database="devsandbox",
        host="db.example",
        port=5433,
        username="dbuser",
        sha256="0" * 64,
        bytes=dump.stat().st_size,
    )
    manifest_path_for(dump).write_text(manifest.model_dump_json(), encoding="utf-8")

    with pytest.raises(ConfigError, match="checksum"):
        asyncio.run(restore_database(URL, dump))


def test_run_tool_scrubs_password_from_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _failed(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="connection failed for password topsecret",
        )

    monkeypatch.setattr(backup_module.subprocess, "run", _failed)

    with pytest.raises(DatabaseBackupError) as caught:
        backup_module._run_tool(
            ["/fake/pg_dump"],
            env={"PGPASSWORD": "topsecret"},
            password="topsecret",
        )

    assert "topsecret" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_missing_postgres_tool_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_module.shutil, "which", lambda _name: None)

    with pytest.raises(ConfigError, match="not found on PATH"):
        backup_module._tool("pg_dump")
