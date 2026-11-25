# DevSandbox database

- SQLAlchemy 2.x async models in `src/devsandbox/db/models.py`
- Alembic migrations in `src/devsandbox/db/migrations/versions/`
- Repositories in `src/devsandbox/db/repositories/`
- Transaction helper: `devsandbox.db.session.unit_of_work`
- Backup/restore helpers: `devsandbox.db.backup`

`TaskRepo.transition` is the single write path for task status changes: it
performs a compare-and-swap (`WHERE id = :id AND status = ANY(:from)`) in a single
transaction, bumps the optimistic-lock `version`, and writes the audit event.

## Migrations

```bash
make migrate                       # alembic upgrade head
devsandbox db upgrade [REVISION]   # default: head
devsandbox db downgrade [REVISION] # default: -1 (one step back)
```

Migrations are packaged with the installed module, not tied to a repository
checkout, so the CLI works from a wheel as well. `devsandbox serve` applies them
at startup by default (`migrations.auto_upgrade`), using a PostgreSQL advisory
lock so overlapping container/systemd restarts cannot race the schema. The
systemd unit and container image both rely on this behavior; see
[deployment.md](deployment.md#55-container-and-systemd-packaging-startup-migrations).

`tests/integration/test_db_migrations.py` covers:

- `upgrade head → downgrade base → upgrade head` on a clean PostgreSQL schema;
- `0007 → 0006 → 0007` (the reversible task-pause column);
- a downgrade against an empty schema is a harmless no-op;
- idempotent startup upgrades and advisory-lock timeout behavior.

## Backup and restore

Backups use `pg_dump --format=custom`; restore uses `pg_restore` and streams
its SQL through `psql`. Install the PostgreSQL client tools on the host that
runs the command. The client major version **must equal or exceed** the server
major version (`pg_dump` refuses a newer server). The orchestrator image pins
the PGDG client major with the `POSTGRES_CLIENT_MAJOR` build arg (default
`17`); override it when the database is newer:

```bash
# Host install example (Debian/Ubuntu): match the server, or go newer.
sudo apt-get install postgresql-client-17
pg_dump --version
```

```bash
# Production image for a PostgreSQL 18 server:
docker build \
  --build-arg POSTGRES_CLIENT_MAJOR=18 \
  -t devsandbox/orchestrator:local \
  -f docker/orchestrator/Dockerfile .
```

```bash
# Stop devsandbox serve first; restore is an offline operation.
devsandbox db backup --out /var/backups/devsandbox-$(date +%F).dump

# Verify the dump + sidecar before a restore (no database connection needed).
devsandbox db verify --from /var/backups/devsandbox-2026-11-20.dump

devsandbox db restore \
  --from /var/backups/devsandbox-2026-11-20.dump \
  --yes
```

`db verify` is a pre-flight check for runbooks: it checks that the file exists,
reads the sidecar, and compares the SHA-256 digest and byte size without running
`pg_restore`. It exits non-zero on a mismatch. If the dump has no sidecar it
reports the dump as readable but unverified; pass `--require-manifest` to make
a missing sidecar an error. `db restore --require-manifest` applies the same
rule to restore.

Streaming through `psql` lets the restore drop the `SET transaction_timeout`
header that `pg_restore` 17 and newer always emit. Older servers reject that
parameter, so a newer client can still restore into them. The statement only
disables a timeout (the default) and is removed before it reaches the server.

Each dump gets a `<dump>.json` sidecar containing the creation time, database
coordinates (no password), Alembic revision, SHA-256 digest, and byte size.
`restore` verifies the digest by default and refuses a mismatch unless
`--force` is passed. If a dump has no sidecar (for example one produced by
another host), it is restored without a checksum check. `--no-clean` keeps
existing objects instead of dropping them first; the default `--clean` is safer
for a full disaster recovery.

`devsandbox db backup` refuses to overwrite an existing file unless
`--overwrite` is passed. Both commands are offline operator commands: they read
`database_url` from the normal `DEVSANDBOX_*` settings and do not require the
HTTP API or its bearer token.

### Secret handling

The database password is never placed in a command line or the child process
environment. It is written to a temporary `PGPASSFILE` with mode `0600` in a
private temporary directory; host/port/user/database are passed as `PG*`
variables. Tool error output is redacted before it reaches the CLI or logs.
Use `--json` for machine-readable results.

## Tests

- `tests/unit/test_db_backup.py` covers URL parsing, passfile isolation,
  overwrite refusal, checksum verification, and password scrubbing without a
  database.
- `tests/integration/test_db_backup_restore.py` performs a real
  `pg_dump → drop schema → pg_restore` round trip (skipped when PostgreSQL
  client tools are unavailable).
- `tests/integration/test_db_migrations.py` covers upgrade, downgrade, and
  upgrade-again.
