# CLI (plan 10)

The `devsandbox` entrypoint is a thin Typer client over the `/v1` HTTP API.
Every command accepts the global options below (place them before the command,
e.g. `devsandbox --json tasks PROJECT`).

| Option | Default | Meaning |
| --- | --- | --- |
| `--json` | off | Emit machine-readable JSON instead of Rich tables. |
| `--local` | off | Use an in-process ASGI client (real DB/services, no HTTP listener). |
| `--api-url URL` | `http://$DEVSANDBOX_API__HOST:$DEVSANDBOX_API__PORT` | Remote API base URL (also `DEVSANDBOX_API_URL`). |
| `--token TOKEN` | `DEVSANDBOX_API_TOKEN` | Bearer token. |
| `--timeout SECONDS` | `30` | HTTP timeout (raised to 120 in local mode). |

Exit codes: `0` success, `1` the project is `failed`/`cancelled` (or the
requested plan does not exist), `2` input, configuration, connection, or API
errors. Error messages go to stderr; `--json` stdout is never polluted by
request logs (structured logging writes to stderr).

## Commands

| Command | Purpose |
| --- | --- |
| `devsandbox init-db` | Apply the packaged Alembic migrations to `database_url`. |
| `devsandbox serve [--host H] [--port P] [--token T] [--no-engine] [--log-level L]` | Run the API and the orchestration engine until interrupted. |
| `devsandbox submit SPEC [--repo PATH\|URL]` | Submit a YAML spec; prints the project id/status. |
| `devsandbox plan PROJECT` | Generate the next plan version (analyzes a fresh project automatically). |
| `devsandbox show-plan PROJECT [--md]` | Show the newest plan as a table, or all plans as Markdown. |
| `devsandbox approve PLAN [--approver NAME]` | Approve and activate a plan. |
| `devsandbox reject PLAN [--reason TEXT]` | Reject a plan; the project stays awaiting approval. |
| `devsandbox start PROJECT` | Start/resume execution of an approved project. |
| `devsandbox pause PROJECT` / `resume PROJECT` / `cancel PROJECT` | Execution controls. |
| `devsandbox mode PROJECT supervised\|autonomous` | Explicit, policy-checked mode change. |
| `devsandbox status [PROJECT] [--watch] [--interval S]` | System status when no project is given; otherwise project status, optionally polled. |
| `devsandbox tasks PROJECT` | Task table: key/title/status/attempts/blocked reason. |
| `devsandbox logs TASK [--attempt N] [--stream agent\|stdout\|stderr] [--tail N]` | Redacted logs (newest attempt by default). |
| `devsandbox events PROJECT [--follow] [--since ID] [--limit N]` | Audit trail; `--follow` streams SSE. |
| `devsandbox report PROJECT [--out FILE] [--format markdown\|json\|html]` | Write/print the deterministic report. `--json` forces JSON. |
| `devsandbox validate-spec SPEC` | Offline validation via the plan-01 loader; no DB/Docker needed. |
| `devsandbox retry TASK [--reset-budget]` | Re-open a `BLOCKED` task (audited). `FAILED` tasks require a replan. |

## Full supervised flow

```bash
export DEVSANDBOX_API_TOKEN=...            # same token as the server

devsandbox submit todo.yaml                # → project id
devsandbox plan PROJECT_ID                 # → plan id (prints tasks/issues)
devsandbox show-plan PROJECT_ID --md
devsandbox approve PLAN_ID
devsandbox start PROJECT_ID
devsandbox status PROJECT_ID --watch
devsandbox tasks PROJECT_ID
devsandbox report PROJECT_ID --out report.md
```

For scripting:

```bash
PROJECT=$(devsandbox --json submit todo.yaml | jq -r .id)
PLAN=$(devsandbox --json plan "$PROJECT" | jq -r .id)
devsandbox --json approve "$PLAN"
devsandbox --json status "$PROJECT"
```

## Local mode

`--local` builds the real application container in-process and talks to it over
`httpx.ASGITransport`, so no server or token setup is required (an ephemeral
token is generated unless one is configured). It still needs the database and
Docker for services that use them. Local mode does **not** start the
orchestration engine loop, so task execution requires `devsandbox serve`.

```bash
devsandbox --local submit todo.yaml
devsandbox --local plan PROJECT_ID
devsandbox --local show-plan PROJECT_ID
```

## Output conventions

- Tables are Rich-rendered for humans; `--json` emits stable, sorted JSON.
- `report` writes to stdout unless `--out` is given, in which case content goes
  to the file and a confirmation to stderr.
- `status --watch`, `logs`, and `events --follow` exit non-zero when the project
  ends in `failed`/`cancelled` (watch) or the request fails.
