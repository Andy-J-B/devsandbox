# HTTP API (plan 10)

The FastAPI app is created by `devsandbox.api.create_app(container)` and served
by `devsandbox serve` (or `python -m devsandbox.api` / `devsandbox-api`). The
committed schema is [`openapi.json`](openapi.json) and is kept current by
`python3 scripts/export_openapi.py`; a test fails if it is stale.

## Security

| Concern | Behaviour |
| --- | --- |
| Binding | `DEVSANDBOX_API__HOST` defaults to `127.0.0.1` |
| Authentication | required static bearer token (`DEVSANDBOX_API_TOKEN` or `DEVSANDBOX_API__TOKEN`), constant-time comparison |
| CORS | no CORS middleware is installed |
| Policy mutation | no route can change network/resource/push/main-merge policy; those come only from settings files/env |
| Mode escalation | `POST /v1/projects/{id}/mode` is the only mode change; supervised → autonomous is rejected unless `security.allow_autonomous_mode` is true and the caller is explicit |
| Spec size | rejected with `413` above `api.max_spec_bytes` (default 1 MB) |
| Logs | redacted, size-capped, and restricted to paths under `<data_root>/logs` or `<data_root>/workspaces` |
| Rate limit | an in-memory sliding window (default 60/minute) covers expensive `POST`/`PATCH` operations |

Every `/v1` request requires:

```http
Authorization: Bearer $DEVSANDBOX_API_TOKEN
```

`GET /healthz` and `GET /readyz` are unauthenticated. `/readyz` checks the
database and Docker daemon and returns `503` when either is unavailable.

## Errors

Failures always use the envelope:

```json
{
  "error": {
    "code": "invalid_spec",
    "message": "validation failed",
    "details": {"...": "field errors"}
  },
  "request_id": "…"
}
```

`request_id` is echoed in the `X-Request-ID` response header (configurable via
`api.request_id_header`). Common statuses: `400` typed service errors, `401`
missing/invalid token, `403` policy violation, `404` unknown resource, `409`
invalid state transition or retry conflict, `413` spec too large, `422` invalid
spec/request/log tail, `429` rate limited, `500` internal error.

## Endpoints

All paths are prefixed with `/v1` except the two health probes.

### Projects

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/projects` | Body: raw YAML (`text/yaml`, `application/yaml`, …) or JSON `{spec_yaml, repo?}`. Returns `201` with the project. `repo` accepts a path or URL and overrides `spec.repo`. |
| `GET` | `/projects` | List projects with status, mode, repo/branch info, and task counts. |
| `GET` | `/projects/{id}` | Project detail; same shape as the list row. |
| `POST` | `/projects/{id}/analyze` | Idempotent analyze + baseline. Terminal projects return `409`. |
| `POST` | `/projects/{id}/plans` | Generate the next plan version. Automatically analyzes a `LOADED` project, so `submit → plan` works directly. |
| `GET` | `/projects/{id}/plans` | Plan versions; `?format=markdown` renders all plans as text. |
| `POST` | `/projects/{id}/start` | Start/resume execution; idempotent for an approved plan. |
| `POST` | `/projects/{id}/pause` | Persist a pause request; scheduling stops. |
| `POST` | `/projects/{id}/resume` | Resume a paused project. |
| `POST` | `/projects/{id}/cancel` | Cancel every non-terminal task. |
| `POST` | `/projects/{id}/mode` | Body `{"mode": "supervised"|"autonomous"}`; policy-checked and audited. |
| `GET` | `/projects/{id}/tasks` | Task rows with attempts, dependencies, and failure info. |

### Plans

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/plans/{plan_id}` | Full plan with tasks/dependencies/issues; `?format=markdown` renders it. |
| `PATCH` | `/plans/{plan_id}` | `PlanPatch` edit of task fields; the graph is revalidated. |
| `POST` | `/plans/{plan_id}/approve` | Optional `{"approver": "alice"}`; activates the plan and starts execution. |
| `POST` | `/plans/{plan_id}/reject` | Optional `{"reason": "..."}`; the project stays `AWAITING_APPROVAL` so a new plan can be requested. |

### Tasks, attempts, and logs

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/tasks/{task_id}` | Full task detail plus attempts and dependencies. |
| `GET` | `/tasks/{task_id}/attempts/{n}` | Attempt detail with the independent validation report. |
| `GET` | `/tasks/{task_id}/attempts/{n}/logs` | `?stream=agent|stdout|stderr&tail=N` (1..`api.max_log_lines`). Output is redacted. |
| `POST` | `/tasks/{task_id}/retry` | Re-opens only `BLOCKED` tasks as `PENDING`. `FAILED` is terminal (00 §7) and returns `409`; replan instead. `{"reset_budget": true}` resets attempt counters and is explicit/audited. |

The log reader resolves the attempt's configured path, rejects anything outside
the data root (`400 unsafe_log_path`), tails at most `api.max_log_bytes`, and
returns `{attempt_id, stream, content, lines, truncated, path}`.

### Events

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/projects/{id}/events` | Ordered audit trail. `since=<event_id>` resumes strictly after that row; `limit` is 1..5000 (default 500). |
| `GET` | `/projects/{id}/events/stream` | Server-sent events with the same `since`, plus `poll_seconds` (0, 60] and `once=true` for tests/tooling. |

Each SSE frame is `id: <event_id>`, `event: <type>`, `data: <json>`.

### Reports and system

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/projects/{id}/report` | `?format=json|markdown|html` (default JSON). Read-only and deterministic for a given DB state; see [reporting.md](reporting.md). |
| `GET` | `/system/status` | Engine state, host resources/pressure, budget usage, project count, and API info. |
| `GET` | `/healthz` | Liveness; always `200` while the process is up. |
| `GET` | `/readyz` | Readiness (`200`/`503`) with `database` and `docker` checks. |

## Dependency injection

Routes only use `AppContainer` (`api/container.py`): settings, a session
factory, `ProjectService`, a plan-service factory, and optional
engine/monitor/git/docker/report hooks. `api/bootstrap.py` wires the concrete
implementations for `serve`; tests inject fake services and still exercise the
real repositories. The engine is started in the server lifespan and stopped
gracefully on shutdown (`api/server.py`).
