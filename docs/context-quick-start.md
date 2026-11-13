# DevSandbox quick context

Read this first when you (human or coding agent) need fast context for a
development task. It is a map, not the specification: follow the links for
details. Deeper docs live in [`docs/`](.) and the authoritative build plan in
[`plans/`](../plans).

## What it is

DevSandbox accepts a YAML project spec + a local/remote Git repository, analyzes
the repo, generates a dependency-aware task plan, optionally waits for approval,
runs coding agents (OpenCode) in disposable Docker containers, validates each
change independently, and integrates validated work onto a per-project
`devsandbox/<project>/staging` branch. It never merges or pushes to `main`.

## Golden rules

- **Never trust the agent.** Completion requires an independent validation
  report; agent claims, summaries, and token counts are untrusted inputs.
- **Never touch `main`.** Integration is staging-only; push is denied by
  default (`security.allow_push`), and there is no main-merge path or setting.
- **Policy comes from settings, not specs.** Specs can narrow limits; they can
  never widen network, resources, retries, push, or main-merge policy.
- **Containers are locked down.** Non-root uid 1000, read-only rootfs, no Docker
  socket, no host mounts except the workspace, `network=none` by default.
- **Secrets never go into prompts, config, commands, logs, or Git.** Only the
  provider key named by the model config is injected into the container env.
- **Recovery must be idempotent.** Crashes during run/validation/integration must
  not create duplicate commits, orphan containers, or corrupt staging.

## Runtime flow

```
spec.yaml + repo
   │  POST /v1/projects (or `devsandbox submit`)
   ▼
analyze repo ──► plan (versioned DAG) ──► approve (supervised)
   ▼
scheduler ──► OpenCode attempt in Docker ──► independent validator
   ▼                                             │
   └──────── retry with failure context ◄────────┘
   ▼
merge validated commit onto staging (conflict retry on new head)
   ▼
final report: changes, tests, failures, outstanding work, split costs
```

## Repository map

| Path | Contents |
| --- | --- |
| `src/devsandbox/spec/` | Project spec models + YAML loader/validator |
| `src/devsandbox/config/` | Pydantic settings, security policy, secret refs |
| `src/devsandbox/db/` | SQLAlchemy models, repositories, Alembic migrations |
| `src/devsandbox/sandbox/` | Docker sandbox manager (isolation, logs, limits) |
| `src/devsandbox/gitmgr/` | Bare repo mirror, workspaces, staging integration |
| `src/devsandbox/agents/` | Agent protocol/registry, OpenCode + fake adapters, prompt, secrets, budgets |
| `src/devsandbox/context/` | Read-only repo analysis, artifacts, task context bundles |
| `src/devsandbox/planner/` | Deterministic/LLM planning, DAG validation, replans |
| `src/devsandbox/scheduler/` | Concurrency, pressure, retry/backoff, circuit breakers |
| `src/devsandbox/orchestrator/` | Engine state machine, recovery, project service |
| `src/devsandbox/validation/` | Independent validation runner/policy |
| `src/devsandbox/reporting/` | Deterministic JSON/Markdown/HTML reports |
| `src/devsandbox/api/` | FastAPI app + `/v1` routes |
| `src/devsandbox/cli/` | Typer CLI (thin HTTP client) |
| `tests/unit|integration|e2e|security|contract/` | Test layers |
| `docker/agent-base/` | Agent image (OpenCode + tools), build/smoke scripts |
| `docker/validator-base/` | Validator image |
| `docs/` | Module docs; start with `quickstart.md`, `cli.md`, `config.md` |
| `plans/` | Authoritative build plan per module |

## Commands you will actually use

### One-time setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

make up                 # PostgreSQL via docker compose
make migrate            # alembic upgrade head
docker/agent-base/build.sh
docker build -t devsandbox/validator:latest docker/validator-base
```

### Start the control plane

```bash
export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
devsandbox serve        # API + orchestration engine on 127.0.0.1:8000
```

`devsandbox serve --no-engine` serves HTTP without executing tasks. Local CLI
mode (`devsandbox --local ...`) does **not** run the engine either.

### Make it code an existing project

```bash
devsandbox submit path/to/project.yaml   # prints PROJECT_ID
devsandbox plan PROJECT_ID               # analyzes repo + creates plan
devsandbox show-plan PROJECT_ID --md
devsandbox approve PLAN_ID
devsandbox start PROJECT_ID
devsandbox status PROJECT_ID --watch
devsandbox report PROJECT_ID --out report.md
```

### Start a brand-new project from scratch

There is no separate "new project" command. Point a spec at a directory (a
non-git directory is imported as an initial `main` commit):

```bash
mkdir -p ~/projects/hello && cd ~/projects/hello
printf '# Hello\n' > README.md
# Put a spec next to it (or anywhere) and submit that file:
devsandbox submit ~/projects/hello.yaml
devsandbox plan PROJECT_ID
devsandbox approve PLAN_ID
devsandbox start PROJECT_ID
```

Minimal spec (`hello.yaml`):

```yaml
version: 1
name: hello
description: "Tiny demo project"
mode: supervised                # or autonomous
repo:
  path: /Users/you/projects/hello
  ref: main
goals:
  - "Add a hello.py script that prints hello"
validation:
  commands:
    - name: smoke
      run: "python3 hello.py"
      required: true
      kind: test
limits:
  max_retries: 1
  task_timeout_seconds: 900
```

Validate the spec offline before submitting: `devsandbox validate-spec hello.yaml`.

### Review and merge (manual)

Validated work is on `devsandbox/PROJECT_ID/staging` in the per-project bare
mirror under the data root, not in the source repository. Fetch it first:

```bash
MIRROR="$DEVSANDBOX_DATA_ROOT/repos/PROJECT_ID/repo.git"
BRANCH="devsandbox/PROJECT_ID/staging"
git -C /path/to/repo fetch "$MIRROR" "${BRANCH}:refs/heads/${BRANCH}"
git -C /path/to/repo log main.."$BRANCH"
git -C /path/to/repo diff main.."$BRANCH"
git -C /path/to/repo merge --no-ff "$BRANCH"
```

If you submitted a non-git directory, clone the mirror instead and use the
staging branch there (`git clone "$MIRROR" project-review`).

## Configuration essentials

- All app settings use the `DEVSANDBOX_` prefix; nested keys use `__`
  (e.g. `DEVSANDBOX_API__PORT`, `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT`).
- `.env` is read, but **only prefixed keys** become settings. Unrelated keys
  such as provider credentials are ignored by DevSandbox settings; reference
  them from `[secrets.<name>]` in `devsandbox.toml` when a model needs them.
- `DEVSANDBOX_API_TOKEN` is required to serve the API.
- Defaults: data root `/var/lib/devsandbox`, PostgreSQL
  `postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox`.
- Agent model defaults to OpenCode; see
  [`agents-opencode.md`](agents-opencode.md) for free-model options.
- Agent/validator images default to local tags. For production, pin them by
  digest (`DEVSANDBOX_AGENT__IMAGE`, `DEVSANDBOX_VALIDATION__IMAGE`) and set
  `DEVSANDBOX_SECURITY__REQUIRE_PINNED_IMAGES=true`; see
  [`images.md`](images.md).
- Full settings list: [`config.md`](config.md).

## Tests and checks

```bash
make test                         # pytest (Docker tests skip if unavailable)
make check                        # ruff + format + mypy strict + unit/contract
DEVSANDBOX_E2E=1 pytest -q tests/e2e
DEVSANDBOX_E2E=1 pytest -q tests/security tests/contract
```

The real-OpenCode smoke test is opt-in and documented in
[`acceptance.md`](acceptance.md) and [`agents-opencode.md`](agents-opencode.md).

## When in doubt

1. Find the module doc in `docs/` and the matching `plans/NN-*.md`.
2. Check the Protocol/interface in `src/devsandbox/*/interfaces.py`.
3. Add or update the closest test layer: unit → integration → e2e → security.
4. Do not weaken an invariant to make a test pass; fix the invariant owner.
