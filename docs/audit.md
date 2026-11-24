# Audit log shipping

DevSandbox already writes an append-only `events` table for every project,
task, attempt, scheduling decision, policy denial, and recovery action. Audit
shipping is an optional export of that log to an operator-owned destination so
external SIEM, log, or alerting pipelines can consume it without reading the
database.

The database remains the source of truth. Shipping is **at-least-once**: a
crash can duplicate the last batch, but it cannot silently skip events. The
cursor advances only after the sink accepts a batch.

## Enabling shipping

```bash
export DEVSANDBOX_AUDIT__ENABLED=true
# Default sink: NDJSON under the data root.
export DEVSANDBOX_AUDIT__SINK=file
```

By default the shipper appends to `<data_root>/audit/events.ndjson` and stores
its cursor in `<data_root>/audit/cursor.json`. Override with
`DEVSANDBOX_AUDIT__PATH` and `DEVSANDBOX_AUDIT__STATE_PATH`.

Shipping runs in the API server lifespan independently of the orchestration
engine, so `devsandbox serve --no-engine` still exports events. The shipper
polls every `audit.interval_seconds` (default `5`) and reads
`audit.batch_size` rows (default `200`) in `(at, id)` order.

## Webhook sink

```bash
export DEVSANDBOX_AUDIT__ENABLED=true
export DEVSANDBOX_AUDIT__SINK=webhook
export DEVSANDBOX_AUDIT__WEBHOOK_URL=https://logs.example.internal/devsandbox
export DEVSANDBOX_AUDIT__WEBHOOK_TOKEN=...   # sent as a Bearer token
```

Each request is:

```json
{
  "schema_version": "1",
  "events": [
    {
      "schema_version": "1",
      "id": "…",
      "project_id": "…",
      "task_id": "…",
      "attempt_id": null,
      "type": "task.started",
      "category": "execution",
      "severity": "info",
      "at": "2026-01-01T12:00:00+00:00",
      "source": {"host": "…", "pid": 123, "component": "devsandbox"},
      "payload": {"…": "…"}
    }
  ]
}
```

Transient failures (transport errors, HTTP 408/425/429/5xx) are retried with
exponential backoff up to `audit.max_retries`. Other 4xx responses fail
immediately: a bad URL, token, or payload will not improve on retry.

## Envelope classification

Every shipped event gets a coarse `category` and `severity` so downstream
pipelines do not have to understand DevSandbox's event vocabulary:

| Category | Examples |
| --- | --- |
| `project` | `project.created`, `project.approved`, `project.completed` |
| `execution` | `task.*`, `attempt.*`, `agent.*`, `validation.*`, `integration.*` |
| `engine` | `ENGINE_*`, `LEASE_*`, `RECOVERY_*` |
| `operations` | `SCHEDULE_DECISION`, `RESOURCE_PRESSURE`, `USAGE_MISMATCH`, `STORAGE_GC_COMPLETED` |
| `security` | `POLICY_DENIED` |
| `audit` | `AUDIT_*` |

`severity` is `error` for policy denials, circuit breakers, exhausted tasks,
failed projects, and audit-ship failures; `warning` for resource pressure,
usage-ledger drift, retries, and attention requests; otherwise `info`.

## Redaction and size bounds

Payloads are redacted with the same process-wide redactor used for logs and
event writes (registered secrets plus token patterns). A payload larger than
`audit.max_payload_bytes` is replaced with a `{"truncated": true, "reason": …}`
record instead of being dropped, so consumers can detect the gap.

## Status and recovery

`GET /v1/system/status` includes an `audit` object when shipping is enabled:

```json
{
  "audit": {
    "enabled": true,
    "shipped": 1284,
    "last_event_at": "2026-01-01T12:00:00+00:00",
    "last_event_id": "…",
    "last_error": null
  }
}
```

If the cursor file is corrupt or unreadable, the shipper logs a warning and
restarts from the first event rather than risking a skip. Re-sent batches are
expected in that case; de-duplicate by envelope `id`.

The cursor is local to the process/data root. Running multiple API replicas
against the same database with shipping enabled would duplicate work; keep
shipping on one replica (or give each replica its own state path and sink
filter) until a shared outbox is added.
