# Quickstart

This walkthrough takes a fresh checkout to a supervised project run and a
report, first with the HTTP API (`curl`) and then with the CLI. The CLI is a
thin client over the same `/v1` API.

## 1. Prerequisites

- Python 3.12
- Docker (the orchestrator needs a daemon to launch agent/validator
  containers)
- PostgreSQL 16 (or `docker compose`); the default database URL is
  `postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox`

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start Postgres (the compose file lives in docker/)
docker compose -f docker/compose.yaml up -d postgres

# Build the images the sandbox manager launches
docker/agent-base/build.sh
docker build -t devsandbox/validator:latest docker/validator-base
```

## 2. Configure

All settings use the `DEVSANDBOX_` prefix (see
[config.md](config.md)). For a local demo:

```bash
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
export DEVSANDBOX_DATABASE_URL="postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"

# Optional: free local model (see docs/agents-opencode.md for hosted free tiers)
export DEVSANDBOX_AGENT__DEFAULT_MODEL="ollama/qwen2.5-coder:7b"
```

The API refuses to start without `DEVSANDBOX_API_TOKEN`. The token is compared
in constant time and must be sent as `Authorization: Bearer <token>`. The API
binds to `127.0.0.1` by default and has no CORS middleware.

The free local model endpoint is configured per project spec / model config.
The example in [agents-opencode.md](agents-opencode.md#option-a--local-ollama-fully-free-no-api-key)
uses Ollama at `http://host.docker.internal:11434/v1` and `network: full`; the
container never receives host credentials.

## 3. Create the schema and start the server

```bash
devsandbox init-db
devsandbox serve            # API + orchestration engine on 127.0.0.1:8000
```

For HTTP-only inspection (no engine loop), use `devsandbox serve --no-engine`.
Bind elsewhere deliberately with `--host`; exposing the API beyond loopback is
an explicit operator decision.

## 4. Example spec

Save as `todo.yaml` and adjust the repository path/URL:

```yaml
version: 1
name: todo-cli
description: "CLI todo app"
mode: supervised                 # human approves the plan before execution
repo:
  path: /absolute/path/to/repo   # or use `url:`
  ref: main
goals:
  - "Add a `todo done <id>` command"
requirements:
  - "Persist tasks to a JSON file"
  - "Keep the existing CLI behaviour"
validation:
  commands:
    - name: tests
      run: "pytest -q"
      required: true
      kind: test
    - name: lint
      run: "ruff check ."
      required: false
      kind: lint
limits:
  max_retries: 1
  task_timeout_seconds: 900
```

Validate offline (no database, Docker, or network):

```bash
devsandbox validate-spec todo.yaml
```

## 5. Full supervised flow with curl

```bash
API=http://127.0.0.1:8000
AUTH="Authorization: Bearer $DEVSANDBOX_API_TOKEN"

# submit by sending the YAML body directly (Content-Type defaults to YAML here)
PROJECT=$(curl -sS -H "$AUTH" -H 'Content-Type: application/yaml' \
  --data-binary @todo.yaml "$API/v1/projects" | jq -r .id)

# analyze + baseline, then generate the first plan
curl -sS -H "$AUTH" -X POST "$API/v1/projects/$PROJECT/analyze" | jq '.status'
PLAN=$(curl -sS -H "$AUTH" -X POST "$API/v1/projects/$PROJECT/plans" | jq -r .id)

# review the plan (JSON or Markdown)
curl -sS -H "$AUTH" "$API/v1/plans/$PLAN?format=markdown"

# approve and start
curl -sS -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"approver":"alice"}' -X POST "$API/v1/plans/$PLAN/approve" | jq '.status'
curl -sS -H "$AUTH" -X POST "$API/v1/projects/$PROJECT/start" | jq '.status'

# watch status, tasks, and the audit trail
curl -sS -H "$AUTH" "$API/v1/projects/$PROJECT" | jq '{status,counts}'
curl -sS -H "$AUTH" "$API/v1/projects/$PROJECT/tasks" | jq
curl -sS -H "$AUTH" "$API/v1/projects/$PROJECT/events?limit=20" | jq

# final report (JSON, Markdown, or HTML-lite)
curl -sS -H "$AUTH" "$API/v1/projects/$PROJECT/report" | jq
curl -sS -H "$AUTH" "$API/v1/projects/$PROJECT/report?format=markdown"
```

`POST /v1/projects` also accepts JSON: `{"spec_yaml": "...", "repo": "/path"}`.
Specs larger than `DEVSANDBOX_API__MAX_SPEC_BYTES` (default 1 MB) return `413`.

## 6. Full supervised flow with the CLI

Against the running server (same `DEVSANDBOX_API_TOKEN` environment):

```bash
devsandbox submit todo.yaml
devsandbox plan PROJECT_ID
devsandbox show-plan PROJECT_ID --md
devsandbox approve PLAN_ID
devsandbox start PROJECT_ID
devsandbox status PROJECT_ID --watch
devsandbox tasks PROJECT_ID
devsandbox logs TASK_ID --attempt 1 --stream agent --tail 100
devsandbox events PROJECT_ID --follow
devsandbox report PROJECT_ID --out report.md
```

Use `--json` before the command for scripting, e.g.
`devsandbox --json tasks PROJECT_ID`. Exit code is `1` when the project is
`failed`/`cancelled`, `2` for API/input/config errors, and `0` otherwise.

`devsandbox --local ...` runs the same commands against an in-process ASGI app
backed by the real database and services, with no HTTP listener. Local mode
does not start the engine loop; use `serve` when tasks must execute.

## 7. Review and merge

DevSandbox never merges to `main`. When the project completes:

```bash
git -C /path/to/repo log main..devsandbox/PROJECT_ID/staging
git -C /path/to/repo diff main..devsandbox/PROJECT_ID/staging
git -C /path/to/repo checkout main
git -C /path/to/repo merge --no-ff devsandbox/PROJECT_ID/staging
```

Inspect `report.outstanding` / the Markdown "Outstanding work" section before
merging. Failed tasks require a replan; blocked tasks can be re-opened with
`devsandbox retry TASK_ID` (an explicit `--reset-budget` is required to reset
attempt counters).

## 8. Docker compose orchestrator

[`docker/compose.yaml`](../docker/compose.yaml) also defines an `orchestrator`
service. It needs the Docker daemon; read the warnings in that file before
using it. Running the orchestrator on the host (as above) is the preferred
deployment, because mounting `docker.sock` grants host-root-equivalent access.
Agent containers never receive the Docker socket.
