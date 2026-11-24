"""PostgreSQL backup and restore helpers.

Operator-facing backups use ``pg_dump --format=custom`` and ``pg_restore``.
The database password is never placed in a command line or environment:

* host/port/user/database are passed as ``PG*`` environment variables;
* the password is written to a private 0600 ``PGPASSFILE`` in a temporary
  directory for the duration of the subprocess, then deleted;
* error output is redacted before it is raised or logged.

These commands are intended for offline maintenance.  Restoring into a live
control plane can race active orchestrator connections, so stop
``devsandbox serve`` first.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ..common.errors import ConfigError, DatabaseBackupError
from ..common.redaction import redact
from ..common.time import utcnow

_MANIFEST_SUFFIX = ".json"
_TOOL_TIMEOUT_SECONDS = 3600

# SQLAlchemy URL query options that map directly to libpq environment vars.
_PG_QUERY_ENV = {
    "application_name": "PGAPPNAME",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "options": "PGOPTIONS",
    "sslcert": "PGSSLCERT",
    "sslcrl": "PGSSLCRL",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
}


class PostgresTarget(BaseModel):
    """Non-secret connection coordinates for one PostgreSQL database."""

    model_config = ConfigDict(frozen=True)

    host: str | None = None
    port: int | None = None
    username: str | None = None
    database: str
    driver: str | None = None
    query: dict[str, str] = Field(default_factory=dict)

    @property
    def sanitized_url(self) -> str:
        """Render the URL without a password (safe for manifests/logs)."""
        return str(
            URL.create(
                "postgresql",
                username=self.username,
                host=self.host,
                port=self.port,
                database=self.database,
            )
        )


class BackupManifest(BaseModel):
    """Metadata written next to a custom-format dump."""

    model_config = ConfigDict(frozen=True)

    format: str = "pg_dump-custom"
    created_at: datetime
    database: str
    host: str | None = None
    port: int | None = None
    username: str | None = None
    alembic_revision: str | None = None
    sha256: str
    bytes: int


class BackupResult(BaseModel):
    """Successful backup path plus its manifest."""

    model_config = ConfigDict(frozen=True)

    path: str
    manifest_path: str
    manifest: BackupManifest


class RestoreResult(BaseModel):
    """Successful restore summary."""

    model_config = ConfigDict(frozen=True)

    source: str
    database: str
    host: str | None = None
    cleaned: bool = True
    restored_at: datetime


def parse_postgres_url(database_url: str) -> PostgresTarget:
    """Parse a SQLAlchemy URL and reject non-PostgreSQL backends."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ConfigError(f"invalid database URL: {exc}") from exc
    backend = url.get_backend_name()
    if backend != "postgresql":
        raise ConfigError(f"database backup/restore requires PostgreSQL, not {backend!r}")
    if not url.database:
        raise ConfigError("database URL must name a database")
    return PostgresTarget(
        host=url.host,
        port=url.port,
        username=url.username,
        database=url.database,
        driver=url.get_driver_name(),
        query={str(key): str(value) for key, value in url.query.items()},
    )


def manifest_path_for(destination: str | Path) -> Path:
    """Return the sidecar manifest path for a dump file."""
    path = Path(destination)
    return path.with_name(path.name + _MANIFEST_SUFFIX)


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise ConfigError(
            f"{name} not found on PATH; install PostgreSQL client tools "
            "to use database backup/restore"
        )
    return found


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "\\n")


@contextmanager
def _passfile(target: PostgresTarget, password: str | None) -> Iterator[Path | None]:
    """Yield a temporary 0600 PGPASSFILE and remove it afterwards."""
    if not password:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="devsandbox-db-") as directory:
        path = Path(directory) / "pgpass"
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            line = ":".join(
                [
                    _pgpass_escape(target.host or "localhost"),
                    _pgpass_escape(str(target.port or 5432)),
                    _pgpass_escape(target.database),
                    _pgpass_escape(target.username or ""),
                    _pgpass_escape(password),
                ]
            )
            os.write(handle, (line + "\n").encode("utf-8"))
        finally:
            os.close(handle)
        yield path


def _postgres_env(target: PostgresTarget, passfile: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "PGPASSWORD",
        "PGPASSFILE",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGDATABASE",
        "PGSERVICE",
        "PGSERVICEFILE",
        *_PG_QUERY_ENV.values(),
    ):
        env.pop(key, None)
    if target.host:
        env["PGHOST"] = target.host
    if target.port:
        env["PGPORT"] = str(target.port)
    if target.username:
        env["PGUSER"] = target.username
    env["PGDATABASE"] = target.database
    env["PGCONNECT_TIMEOUT"] = "15"
    for key, env_name in _PG_QUERY_ENV.items():
        if key in target.query:
            env[env_name] = target.query[key]
    if passfile is not None:
        env["PGPASSFILE"] = str(passfile)
    return env


def _scrubbed(text_value: str, password: str | None) -> str:
    scrubbed = str(redact(text_value))
    if password:
        scrubbed = scrubbed.replace(password, "[REDACTED]")
    return scrubbed


_TRANSACTION_TIMEOUT_SET = "SET transaction_timeout = 0;"
_IDLE_TIMEOUT_SET = "SET idle_in_transaction_session_timeout = 0;"


def _restore_sql(lines: Iterable[str]) -> Iterator[str]:
    """Yield restore SQL with settings unsupported by older servers removed.

    ``pg_restore`` 17 and newer unconditionally writes ``SET transaction_timeout
    = 0;`` into its fixed output header.  A server older than 17 does not know
    that parameter and aborts the restore.  The parameter's default is already
    0 (disabled), so dropping the statement is safe when the client is newer
    than the server.

    Only the fixed header occurrence is removed: it directly follows the
    idle-timeout statement.  Tracking COPY data and arbitrary SQL bodies means a
    table value or function body that happens to contain the same text is kept.
    """
    previous = ""
    in_copy = False
    for line in lines:
        current = line.rstrip("\r\n")
        if in_copy:
            yield line
            if current == "\\.":
                in_copy = False
                previous = current
            continue
        if current.startswith("COPY ") and current.endswith(" FROM stdin;"):
            in_copy = True
            yield line
            previous = current
            continue
        if current == _TRANSACTION_TIMEOUT_SET and previous == _IDLE_TIMEOUT_SET:
            previous = current
            continue
        yield line
        previous = current


def _run_restore(
    args: Sequence[str],
    *,
    psql: str,
    env: Mapping[str, str],
    database: str,
    password: str | None,
) -> None:
    """Stream ``pg_restore`` SQL through ``psql``, filtering fixed state.

    Restoring through SQL (instead of letting ``pg_restore`` execute directly)
    lets us drop client-generated statements the target server does not know.
    ``psql`` runs with ``ON_ERROR_STOP`` so the first real error still fails the
    restore, matching ``pg_restore --exit-on-error``.
    """
    psql_args = [
        psql,
        "--no-psqlrc",
        "--quiet",
        "--set=ON_ERROR_STOP=1",
        "--dbname",
        database,
    ]
    # stderr goes to temporary files, not pipes: neither child can block on a
    # full pipe while the other one is still running.
    with (
        tempfile.TemporaryFile(mode="w+b") as restore_err,
        tempfile.TemporaryFile(mode="w+b") as psql_err,
    ):
        try:
            restore = subprocess.Popen(
                list(args),
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=restore_err,
                text=True,
            )
        except OSError as exc:
            raise DatabaseBackupError(f"{Path(args[0]).name} failed to start: {exc}") from exc
        assert restore.stdout is not None
        try:
            apply = subprocess.Popen(
                psql_args,
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=psql_err,
                text=True,
            )
        except OSError as exc:
            restore.kill()
            restore.communicate()
            raise DatabaseBackupError(f"{Path(psql).name} failed to start: {exc}") from exc
        assert apply.stdin is not None
        restore_stdout = restore.stdout
        apply_stdin = apply.stdin

        def _feed() -> None:
            try:
                for line in _restore_sql(restore_stdout):
                    apply_stdin.write(line)
                apply_stdin.flush()
            except (BrokenPipeError, OSError):
                # psql stopped reading (usually because it hit an error); the
                # process status below carries the real diagnostic.
                pass
            finally:
                with contextlib.suppress(OSError):
                    restore_stdout.close()
                with contextlib.suppress(OSError):
                    apply_stdin.close()

        feeder = threading.Thread(target=_feed, name="devsandbox-restore-feed", daemon=True)
        feeder.start()
        try:
            restore_status = restore.wait(timeout=_TOOL_TIMEOUT_SECONDS)
            apply_status = apply.wait(timeout=_TOOL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            for process in (restore, apply):
                if process.poll() is None:
                    process.kill()
            restore.wait()
            apply.wait()
            raise DatabaseBackupError(
                f"{Path(args[0]).name} timed out after {_TOOL_TIMEOUT_SECONDS} seconds"
            ) from exc
        feeder.join(timeout=5)
        restore_err.seek(0)
        restore_detail = restore_err.read().decode("utf-8", errors="replace")
        psql_err.seek(0)
        psql_detail = psql_err.read().decode("utf-8", errors="replace")

    # psql owns execution: if it failed, its message is the useful one.  A
    # non-zero pg_restore status with a clean psql usually means the archive
    # could not be read.
    if apply_status != 0:
        detail = (psql_detail or restore_detail or "no diagnostic output").strip()
        raise DatabaseBackupError(
            f"{Path(psql).name} exited with status {apply_status}: {_scrubbed(detail, password)}"
        )
    if restore_status != 0:
        detail = (restore_detail or psql_detail or "no diagnostic output").strip()
        raise DatabaseBackupError(
            f"{Path(args[0]).name} exited with status {restore_status}: "
            f"{_scrubbed(detail, password)}"
        )


def _run_tool(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    password: str | None = None,
) -> None:
    """Run a PostgreSQL client tool in a thread with scrubbed diagnostics."""
    try:
        completed = subprocess.run(
            list(args),
            env=dict(env),
            check=False,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DatabaseBackupError(f"{Path(args[0]).name} failed to start: {exc}") from exc
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
    raise DatabaseBackupError(
        f"{Path(args[0]).name} exited with status {completed.returncode}: "
        f"{_scrubbed(detail, password)}"
    )


async def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file."""

    def _digest() -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return await asyncio.to_thread(_digest)


async def current_alembic_revision(database_url: str) -> str | None:
    """Return the current ``alembic_version.version_num``, if available."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("select version_num from alembic_version"))
            return result.scalar_one_or_none()
    except Exception:
        return None
    finally:
        await engine.dispose()


def password_from_url(database_url: str) -> str | None:
    """Return the URL password without exposing it in a rendered URL."""
    try:
        return make_url(database_url).password
    except Exception:
        return None


def sanitize_database_url(database_url: str) -> str:
    """Render a database URL for logs with the password removed."""
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid database URL>"


async def backup_database(
    database_url: str,
    destination: str | Path,
    *,
    overwrite: bool = False,
    clock: Callable[[], datetime] = utcnow,
) -> BackupResult:
    """Write a custom-format dump and sidecar manifest."""
    target = parse_postgres_url(database_url)
    password = password_from_url(database_url)
    output = Path(destination)
    if output.exists() and not overwrite:
        raise ConfigError(f"backup destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{secrets.token_hex(8)}.tmp")
    dump = _tool("pg_dump")

    try:
        with _passfile(target, password) as passfile:
            env = _postgres_env(target, passfile)
            args = [
                dump,
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(temporary),
            ]
            await asyncio.to_thread(_run_tool, args, env=env, password=password)
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    digest = await sha256_file(output)
    revision = await current_alembic_revision(database_url)
    created_at = clock()
    manifest = BackupManifest(
        created_at=created_at,
        database=target.database,
        host=target.host,
        port=target.port,
        username=target.username,
        alembic_revision=revision,
        sha256=digest,
        bytes=output.stat().st_size,
    )
    sidecar = manifest_path_for(output)
    sidecar.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return BackupResult(path=str(output), manifest_path=str(sidecar), manifest=manifest)


async def restore_database(
    database_url: str,
    source: str | Path,
    *,
    clean: bool = True,
    verify: bool = True,
    force: bool = False,
    clock: Callable[[], datetime] = utcnow,
) -> RestoreResult:
    """Restore a custom-format dump into the target database."""
    target = parse_postgres_url(database_url)
    password = password_from_url(database_url)
    dump = Path(source)
    if not dump.is_file():
        raise ConfigError(f"backup file not found: {dump}")
    if dump.stat().st_size == 0:
        raise ConfigError(f"backup file is empty: {dump}")

    sidecar = manifest_path_for(dump)
    if verify and sidecar.is_file() and not force:
        try:
            manifest = BackupManifest.model_validate_json(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigError(f"invalid backup manifest {sidecar}: {exc}") from exc
        actual = await sha256_file(dump)
        if actual != manifest.sha256:
            raise ConfigError(
                "backup checksum does not match its manifest; pass force=True to restore anyway"
            )

    restore = _tool("pg_restore")
    psql = _tool("psql")
    args = [
        restore,
        "--file=-",
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
    ]
    if clean:
        args.extend(["--clean", "--if-exists"])
    args.append(str(dump))
    with _passfile(target, password) as passfile:
        env = _postgres_env(target, passfile)
        await asyncio.to_thread(
            _run_restore,
            args,
            psql=psql,
            env=env,
            database=target.database,
            password=password,
        )
    restored_at = clock()
    return RestoreResult(
        source=str(dump),
        database=target.database,
        host=target.host,
        cleaned=clean,
        restored_at=restored_at,
    )


def read_manifest(path: str | Path) -> BackupManifest:
    """Read and validate a sidecar manifest."""
    sidecar = Path(path)
    if not sidecar.is_file():
        raise ConfigError(f"backup manifest not found: {sidecar}")
    try:
        return BackupManifest.model_validate_json(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid backup manifest {sidecar}: {exc}") from exc


__all__ = [
    "BackupManifest",
    "BackupResult",
    "PostgresTarget",
    "RestoreResult",
    "backup_database",
    "current_alembic_revision",
    "manifest_path_for",
    "parse_postgres_url",
    "password_from_url",
    "read_manifest",
    "restore_database",
    "sanitize_database_url",
    "sha256_file",
]
