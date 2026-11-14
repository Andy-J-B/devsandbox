# Operational drills

Drills exercise failure modes that ordinary unit and E2E tests do not cover.
They are opt-in because they are slow and, in their manual form, disruptive:

```bash
scripts/drills.sh
scripts/drills.sh -k postgres
DEVSANDBOX_DRILL_IDLE_SECONDS=300 scripts/drills.sh
```

The automatic suite is `tests/drills/` and is marked `drill`. It only runs when
`DEVSANDBOX_DRILLS=1`; `scripts/drills.sh` sets that plus the E2E prerequisites
(Docker, PostgreSQL, `devsandbox/validator:latest`). The PostgreSQL drill starts
its own throwaway container and never restarts the database named by
`DEVSANDBOX_DATABASE_URL`.

**Never run the manual procedures on a host with production data.** Use a
dedicated VM or a disposable development host.

## Automatic drills

| Drill | What it proves |
| --- | --- |
| `test_drill_docker_outage.py` | A sandbox call that fails like an unavailable Docker daemon becomes an `infra_error` retry; the task completes when the daemon recovers. |
| `test_drill_postgres_restart.py` | The engine loop retries transient disconnects after a real PostgreSQL stop/start, keeps its lease, and resumes scheduling. |
| `test_drill_disk_full.py` | `ENOSPC` is translated into a clear `SandboxError`, and a transient disk-full failure is retried rather than crashing the engine. |
| `test_drill_idle_resources.py` | An idle engine (DB ticks every 100 ms, no tasks) does not grow RSS beyond 64 MiB or use more than 50% of one core over the window. |

Tune the idle drill with:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEVSANDBOX_DRILL_IDLE_SECONDS` | `30` | Observation window. Use 300+ for a pre-release soak. |
| `DEVSANDBOX_DRILL_MAX_RSS_GROWTH_MB` | `64` | Allowed RSS growth after a 2 s warm-up. |
| `DEVSANDBOX_DRILL_MAX_CPU_PCT` | `50` | Allowed mean CPU as a percentage of one core. |
| `DEVSANDBOX_DRILL_POSTGRES_IMAGE` | `postgres:16` | Image for the throwaway restart drill. |

Record the observed RSS growth and CPU percentage in the release notes or
`docs/acceptance.md` when you run the longer window.

## Manual: Docker daemon restart

Use this when you need to verify behaviour against the real host daemon.

### Preflight

The drill only tests the daemon if the agent itself works. Before restarting
Docker, confirm a normal run reaches a completed task:

1. Configure a real model in the `serve` environment
   (`DEVSANDBOX_AGENT__DEFAULT_MODEL`, plus `BASE_URL`/`SECRET_REF` when
   needed) and confirm the container can reach it.
2. Submit a small project and let one task complete without restarting
   anything.
3. If the task fails as `agent_error`, fix the model/auth configuration first.
   A failure in seconds with no `infra_error` event means Docker was never the
   cause.

### Deterministic variant (no model)

To validate only the Docker/infrastructure path, run `serve` with the built-in
fake agent:

```bash
DEVSANDBOX_AGENT__DEFAULT_RUNTIME=fake-script devsandbox serve
```

Submit a project whose validation accepts the fake agent's committed file
(for example `test -f AGENT_OUTPUT.md`). The fake agent always edits and
commits, so a completed task proves the orchestrator survived the daemon
restart. Do not use this variant to judge real model quality; it deliberately
bypasses the LLM.

### Procedure

1. Submit a fresh supervised project and wait until a task is running (or use
   the project from the preflight).
2. Watch the orchestrator log and note the active attempt id.
3. Restart the daemon:
   - Linux: `sudo systemctl restart docker`
   - Docker Desktop: restart Docker Desktop (or `osascript` equivalent)
4. While the daemon is down, expect sandbox operations to raise
   `SandboxError`; the task should move to `RETRY_WAIT` on an infra retry (the
   payload is `infra_error` for a raised `SandboxError` and
   `container_failure` for an agent run that returned `sandbox_error`). The
   orchestrator process must stay alive. Docker SDK transport failures (for
   example `ChunkedEncodingError: Response ended prematurely` from a dropped
   log/wait connection) are translated to `SandboxError`, not left as an
   unclassified fatal agent error.
5. After `docker info` succeeds again, expect the retry to launch a fresh
   container. The stale container from the crashed daemon is replaced by the
   deterministic attempt name on the next run and removed by the janitor.
   Infra retries back off with the infra-failure count and the default cap is
   8, which leaves several minutes for a normal Docker Desktop restart.
   Repeated identical sandbox errors during the outage are expected; the
   repeat-identical breaker only stops budget-consuming failures, so it does
   not abort the task mid-outage.
6. Verify the task eventually completes and the staging branch contains the
   validated commit.

If the engine process itself was restarted, recovery re-enters from the
persisted checkpoint; see `docs/orchestrator.md`.

## Manual: PostgreSQL restart

1. Start the orchestrator and confirm it owns the lease (engine started event).
2. Restart PostgreSQL:
   - Compose: `docker compose -f docker/compose.yaml restart postgres`
   - Native: `sudo systemctl restart postgresql`
3. Expect the engine loop to log `transient database error; retrying` and back
   off (1 s doubling to 30 s by default). The process must not exit.
4. After PostgreSQL accepts connections, the next round should succeed without
   an orchestrator restart. `pool_pre_ping` replaces stale pooled connections.
5. Confirm a new task can be scheduled and completed.

Outages longer than half the lease TTL can let the lease expire; another
orchestrator could then acquire it. That is intentional single-writer
behaviour, not a reconnect bug. Keep the engine's `lease_ttl_seconds` larger
than the longest expected database outage, or run only one engine.

## Manual: disk full

Use a disposable Linux VM (or a loopback filesystem) so a full disk cannot
damage your workstation.

```bash
# Linux example: 200 MiB loopback filesystem
truncate -s 200M /tmp/devsandbox-disk.img
mkfs.ext4 /tmp/devsandbox-disk.img
mkdir -p /mnt/devsandbox-disk
sudo mount -o loop /tmp/devsandbox-disk.img /mnt/devsandbox-disk
sudo chown "$(id -u):$(id -g)" /mnt/devsandbox-disk
export DEVSANDBOX_DATA_ROOT=/mnt/devsandbox-disk
export DEVSANDBOX_LIMITS__DISK_MIN_FREE_MB=64
```

1. Fill the filesystem in the background with `fallocate` or `dd` until free
   space drops below `disk_min_free_mb`.
2. Submit a project. The scheduler should defer launches with reason
   `insufficient_disk` and emit a `schedule.decision` event; it must not launch
   a container on a full disk.
3. If space runs out during a run, expect `SandboxError` with a
   `disk full or quota exceeded` hint for directory creation, or
   `disk full while writing logs` if the log stream fails. The task records an
   `infra_error` retry and the engine stays alive.
4. Free space and confirm the deferred/retried task proceeds.
5. Clean up:

```bash
sudo umount /mnt/devsandbox-disk
rm -f /tmp/devsandbox-disk.img
```

On macOS, an APFS disk image can be mounted with `hdiutil`, but Linux ext4 is
the more representative target for production.

## Manual: idle RSS/CPU soak

Run the automatic idle drill with a longer window on the target host:

```bash
DEVSANDBOX_DRILLS=1 \
DEVSANDBOX_DRILL_IDLE_SECONDS=300 \
DEVSANDBOX_DRILL_MAX_RSS_GROWTH_MB=64 \
DEVSANDBOX_DRILL_MAX_CPU_PCT=50 \
python3 -m pytest -q -m drill tests/drills/test_drill_idle_resources.py
```

For a real deployment, repeat with the production database and the normal
engine tick, and record:

- start/end RSS (MiB) and growth,
- mean CPU percentage of one core,
- number of tasks/database rows at start and end (no work should accumulate),
- any `transient database error` log lines.

A flat RSS curve and low single-digit CPU are expected when no tasks are
runnable. A steady upward slope or sustained CPU near the limit indicates a
leak or a busy loop; capture a profile before changing limits.
