# Quickstart

This walkthrough takes a fresh checkout to a supervised project run and a
report, first with the HTTP API (`curl`) and then with the CLI. The CLI is a
thin client over the same `/v1` API.

The example uses a local Git repository and a local Ollama model, so it needs no
cloud credentials. Any OpenAI-compatible endpoint can be used instead; see
[config.md](config.md) and [agents-opencode.md](agents-opencode.md).

## 1. Prerequisites

- Python 3.12
- Docker (the orchestrator needs a daemon to launch agent/validator
  containers)
- PostgreSQL 16 (or `docker compose`)
- `git`, `curl`, `jq`, and `openssl`

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

The default database URL is
`postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox`. If your
PostgreSQL listens elsewhere (a container port mapping, a native install with a
non-default port), set `DEVSANDBOX_DATABASE_URL` to the matching
`postgresql+asyncpg://...` URL.

`jq` is only used by the `curl` examples below. The CLI does not need it; the
equivalent JSON output is available with `devsandbox --json ...`.

## 2. Configure

All settings use the `DEVSANDBOX_` prefix (see [config.md](config.md)). Nested
keys use a double underscore. For a local demo with Ollama:

```bash
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
export DEVSANDBOX_DATABASE_URL="postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"

# Local model (see docs/agents-opencode.md for retained hosted options)
export DEVSANDBOX_AGENT__DEFAULT_MODEL="ollama/qwen3:8b"
export DEVSANDBOX_AGENT__BASE_URL="http://host.docker.internal:11434/v1"

# The sandbox blocks egress by default. Ollama runs on the host, so allow
# network access for this demo.
export DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT=true
```

On Linux, replace `host.docker.internal` with the host bridge address that is
reachable from containers (commonly `http://172.17.0.1:11434/v1`).

The Ollama model must be pulled before the first run:

```bash
ollama pull qwen3:8b
```

The API refuses to start without `DEVSANDBOX_API_TOKEN`. The token is compared
in constant time and must be sent as `Authorization: Bearer <token>`. The API
binds to `127.0.0.1` by default and has no CORS middleware.

Note that `DEVSANDBOX_AGENT__DEFAULT_MODEL` alone is not enough for a local
OpenAI-compatible endpoint: also set `DEVSANDBOX_AGENT__BASE_URL` (and
`DEVSANDBOX_AGENT__SECRET_REF` when the endpoint requires a key). Specs cannot
widen network or model policy; `security.allow_network_default` is the
authoritative switch.

## 3. Create the schema and start the server

Use an empty database (or a database that is already managed by Alembic):

```bash
devsandbox init-db
devsandbox serve            # API + orchestration engine on 127.0.0.1:8000
```

`init-db` applies every packaged migration to `DEVSANDBOX_DATABASE_URL`. A
database that already contains DevSandbox tables but has no `alembic_version`
row cannot be upgraded automatically; recreate it, or stamp the migration that
matches its schema before running `init-db`.

For HTTP-only inspection (no engine loop), use `devsandbox serve --no-engine`.
Bind elsewhere deliberately with `--host`; exposing the API beyond loopback is
an explicit operator decision.

Keep the configuration exported in the same shell that runs `serve`, or put the
same `DEVSANDBOX_*` keys in a `.env` file. Only prefixed keys become settings.

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

A non-git directory is imported as an initial `main` commit, so a brand-new
project only needs a directory and a spec file.

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
curl -sS -H "$AUTH" "$API/v1/plans/$PLAN" | jq
curl -sS -H "$AUTH" "$API/v1/plans/$PLAN?format=markdown"
echo "$PLAN"

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

Poll `GET /v1/projects/$PROJECT` until `status` is `completed` (or `failed`).
The engine runs in the `serve` process; `--no-engine` never executes tasks.

`POST /v1/projects` also accepts JSON: `{"spec_yaml": "...", "repo": "/path"}`.
Specs larger than `DEVSANDBOX_API__MAX_SPEC_BYTES` (default 1 MB) return `413`.

## 6. Full supervised flow with the CLI

Against the running server (same `DEVSANDBOX_API_TOKEN` environment):

```bash
PROJECT=$(devsandbox submit todo.yaml)     # prints the project id
devsandbox plan "$PROJECT"                 # prints the plan id
devsandbox show-plan "$PROJECT" --md
devsandbox approve PLAN_ID
devsandbox start "$PROJECT"
devsandbox status "$PROJECT" --watch
devsandbox tasks "$PROJECT"
devsandbox logs TASK_ID --attempt 1 --stream agent --tail 100
devsandbox events "$PROJECT" --follow
devsandbox report "$PROJECT" --out report.md
```

Take `PLAN_ID` from `devsandbox show-plan` output, and task ids from
`devsandbox tasks`. `devsandbox status --watch` exits when the project reaches a
terminal state.

The CLI can also use an explicit server/token:

```bash
devsandbox --api-url http://127.0.0.1:8000 --token "$DEVSANDBOX_API_TOKEN" tasks PROJECT_ID
```

Use `--json` before the command for scripting, e.g.
`devsandbox --json tasks PROJECT_ID`. Exit code is `1` when the project is
`failed`/`cancelled`, `2` for API/input/config errors, and `0` otherwise.

`devsandbox --local ...` runs the same commands against an in-process ASGI app
backed by the real database and services, with no HTTP listener. Local mode
does not start the engine loop; use `serve` when tasks must execute.

## 7. Review and merge

DevSandbox never merges to `main`. Validated work is committed on
`devsandbox/<PROJECT_ID>/staging` **inside the per-project bare mirror** under
the data root:

```text
$DEVSANDBOX_DATA_ROOT/repos/<PROJECT_ID>/repo.git
```

The source repository you submitted does not automatically contain that branch.
Fetch it from the mirror before reviewing:

```bash
DATA_ROOT=/absolute/path/to/.devsandbox
MIRROR="$DATA_ROOT/repos/$PROJECT_ID/repo.git"
BRANCH="devsandbox/$PROJECT_ID/staging"
REPO=/path/to/source/repo

git -C "$REPO" fetch "$MIRROR" "${BRANCH}:refs/heads/${BRANCH}"
git -C "$REPO" log main.."$BRANCH"
git -C "$REPO" diff main.."$BRANCH"
git -C "$REPO" checkout main
git -C "$REPO" merge --no-ff "$BRANCH"
```

If you submitted a non-git directory, it was only imported into the mirror;
there is no source repository to fetch into. Clone the mirror instead and use
the staging branch there:

```bash
git clone "$MIRROR" project-review
git -C project-review checkout "$BRANCH"
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

## Troubleshooting

- **`relation "projects" already exists` during `init-db`** — the database has
  DevSandbox tables but no Alembic version. Use a fresh database, or stamp the
  existing schema to the correct revision before upgrading.
- **The report warns that baseline validation failed** — for a new project this
  is expected when the project's checks depend on files the plan has not
  created yet (for example `python3 greet.py` before `greet.py` exists). The
  per-task validation after the agent runs is authoritative.
- **Tasks stay `blocked` with `INSUFFICIENT_MEMORY`** — the scheduler defers
  work when the host is under memory pressure. Stop idle containers/models
  (for example `ollama stop qwen3:8b`) or raise the host limits in
  `[limits]`.
- **Agent container cannot reach Ollama** — check
  `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT=true`,
  `DEVSANDBOX_AGENT__BASE_URL`, and that
  `http://host.docker.internal:11434/v1/models` is reachable from a container.
- **OpenCode uses the wrong model** — set `DEVSANDBOX_AGENT__DEFAULT_MODEL` and
  `DEVSANDBOX_AGENT__BASE_URL` in the `serve` process environment, not only in
  the project spec. A missing model configuration falls back to the OpenCode
  default.
- **The staging branch is missing from your source repo** — fetch it from the
  bare mirror as shown in section 7.
