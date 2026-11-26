# DevSandbox — context for AI coding agents

**Read this first.** This is the highest-signal orientation for an AI coding
agent (or human) about to change DevSandbox. It is a map, not the specification:
follow the links for depth and read only what the task needs. When this file
disagrees with code, code wins — update this file in the same change.

> **Current state:** the MVP described by plans 01–11 is implemented. Treat
> `plans/` as the design record (some details are superseded by code); use
> `plans/TODO.md` and recent commits for current work.

## Agent quick start

1. Read this file, then check `git status` and `git log -5` for work in flight.
2. Find your area in [Change recipes](#change-recipes); read the matching module
   doc and plan.
3. Read the relevant `src/devsandbox/*/interfaces.py` and the closest tests
   before editing.
4. Make the smallest coherent change; never weaken an invariant or a test to
   make it pass.
5. Run `make check` plus the targeted suite. Update docs (and
   `docs/openapi.json` for API changes) when behavior changes.

## 60-second model

DevSandbox is a Python 3.12 control plane that turns:

```
spec.yaml + Git repo
  → read-only repo analysis
  → versioned, dependency-aware task DAG
  → optional human approval (supervised mode)
  → OpenCode agent in a disposable Docker container
  → independent validation in fresh containers
  → one commit per validated task onto devsandbox/<project>/staging
  → deterministic report (changes, tests, failures, outstanding work, costs)
```

It is **not** a chat UI, an auto-merger, or a general CI system. It never merges
to `main`. Push is off by default; when explicitly enabled, only
`devsandbox/*` branches are pushed and `main` is always refused.

Core nouns:

| Noun | Meaning |
| --- | --- |
| **Project** | A spec + repository + lifecycle state. |
| **Plan** | Versioned DAG of tasks; supervised plans wait for approval. |
| **Task** | One unit of agent work with dependencies, retries, and budget. |
| **Attempt** | One container run for a task; ids, branches, and paths are deterministic. |
| **ValidationReport** | Independent evidence; the only path to `COMPLETED`. |
| **Integration** | A validated commit applied to staging exactly once. |

The orchestrator is the only owner of task/project state. Modules depend on the
Protocols in `src/devsandbox/*/interfaces.py`; the orchestrator wires concrete
implementations. Leaf modules do not call each other directly.

## Invariants — never break these

1. **Agent output is untrusted.** Exit code 0, summaries, and token counts prove
   nothing. `COMPLETED` requires an independent validation pass *and* a committed
   integration. Validation never reuses the agent container.
2. **`main` is never modified.** Integration is staging-only. There is no
   main-merge path or setting. `security.allow_push` defaults to `false`; when
   enabled it only permits `devsandbox/*` branches and still refuses `main`.
3. **Policy comes from settings, not specs.** `SecurityPolicy` is derived only
   from `config/settings.py`. Specs and plans may narrow network, resources,
   retries, concurrency, and mode; they can never widen them. Specs cannot grant
   push, network, image, or autonomous-mode policy.
4. **Containers are locked down.** Non-root uid 1000, read-only rootfs,
   `cap_drop=ALL`, `no-new-privileges`, pids/memory/CPU limits, no Docker
   socket, only the attempt workspace mounted, `network=none` by default, and a
   hardened seccomp profile. Docker daemon access is host-root equivalent; never
   expose it to an agent container.
5. **Validation is independent.** Fresh validator containers, no agent secrets,
   the agent workspace is copied and never mutated, and required checks must
   exist before a task can pass.
6. **Secrets never leak.** Only the provider key named by the model config
   reaches the agent container, via a private `0700` read-only secret-file mount.
   Values never appear in prompts, config, commands, logs, reports, or Git.
   Redaction runs at every boundary.
7. **State changes go through the state machine.** Only `TaskRepo.transition`
   and `ProjectRepo.transition` write status; legal edges live in
   `orchestrator/state_machine.py`. Persist intent before any external side
   effect. Attempt ids, container names, workspace paths, and branches are
   deterministic so recovery is idempotent.
8. **Integration is serialized.** One integration per project at a time, using
   an in-process lock plus the git manager's staging-ref CAS. A crash must not
   create duplicate commits, orphan containers, or corrupt staging.
9. **Schema changes need a migration.** Add an Alembic revision under
   `db/migrations/versions/`; `serve` applies the chain under an advisory lock.
10. **Do not weaken the guardrails.** If an invariant test fails, fix the owner
    of the invariant, not the assertion.

## Architecture at a glance

```
POST /v1/projects (spec.yaml)
        │
        ▼
 ProjectService ──► analyze repo ──► plan (versioned DAG) ──► approve
        │                                                        │
        └──────────────────────────────► engine loop (DB lease) ◄┘
                                             │
                                             ▼
                  scheduler ──► sandbox ──► agent (OpenCode)
                                             │
                                             ▼
                                  git.collect_changes
                                             │
                                             ▼
                                  validator (fresh containers)
                                             │ pass
                                             ▼
                       git integration (serialized) ──► staging branch
                                             │
                                             ▼
                                  report + append-only events
```

Project lifecycle:
`LOADED → ANALYZING → PLANNING → AWAITING_APPROVAL → EXECUTING → COMPLETED | FAILED | CANCELLED`,
with `PAUSED` as an orchestration state. Autonomous mode skips
`AWAITING_APPROVAL`.

Task lifecycle:
`PENDING → READY → SCHEDULED → RUNNING → VALIDATING → VALIDATED → INTEGRATING → COMPLETED`,
plus `RETRY_WAIT`, `BLOCKED`, `FAILED`, and `CANCELLED`. `BLOCKED` is terminal for
automatic execution but can be re-opened by an explicit user action or replan.

## Where things live

| Area | Owns | Start at |
| --- | --- | --- |
| `config/` | Settings, `SecurityPolicy`, policy narrowing | `config/settings.py`, `config/policy.py` |
| `spec/` | YAML spec models + `load_spec` | `spec/models.py` |
| `db/` | SQLAlchemy models, repositories, Alembic migrations | `db/models.py`, `db/repositories/` |
| `sandbox/` | Docker isolation, resource limits, logs, orphan reaping | `sandbox/manager.py` (`_build_container_kwargs`) |
| `gitmgr/` | Bare mirror, workspaces, change collection, staging integration, push guard | `gitmgr/manager.py` |
| `context/` | Read-only repo analysis, artifacts, task context bundles | `context/manager.py` |
| `planner/` | Deterministic/LLM planning, DAG validation, replans | `planner/service.py`, `planner/validate.py` |
| `scheduler/` | Concurrency, pressure, fairness, retry/backoff decisions | `scheduler/scheduler.py` |
| `orchestrator/` | Lifecycle, state machine, engine, failures, recovery | `orchestrator/engine.py`, `state_machine.py` |
| `agents/` | Agent Protocol/registry, OpenCode + fake adapters, prompts, secrets, budgets | `agents/opencode.py`, `agents/interfaces.py` |
| `validation/` | Pre-checks, tampering heuristics, independent check runner | `validation/validator.py` |
| `reporting/` | Deterministic JSON/Markdown/HTML reports | `reporting/builder.py` |
| `api/` | FastAPI app and `/v1` routes | `api/app.py`, `api/routes/` |
| `cli/` | Typer CLI (thin HTTP client) | `cli/app.py`, `cli/client.py` |
| `common/` | Ids, errors, logging, redaction, shared types | `common/types.py`, `common/errors.py` |
| `storage/`, `audit/` | Retention/GC and audit shipping | `storage/janitor.py`, `audit/shipper.py` |

Other top-level paths:

| Path | Contents |
| --- | --- |
| `tests/{unit,integration,e2e,security,contract,drills}/` | Test layers and shared helpers/fakes |
| `docker/agent-base/` | Agent image (OpenCode + tools), build/smoke scripts |
| `docker/validator-base/` | Validator image |
| `docker/egress-proxy/` | Allowlist-mode HTTP/HTTPS egress proxy |
| `docker/apparmor/`, `docker/socket-proxy/`, `docker/api-proxy/` | Optional hardening and ingress assets |
| `deploy/systemd/` | systemd unit and deployment assets |
| `docs/` | Module docs and operator guides (see [Read next](#read-next)) |
| `plans/` | Design record per module; `plans/00-overview-and-contracts.md` has shared contracts |

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
docker/egress-proxy/build.sh   # only for allowlist network mode
```

### Start the control plane

```bash
export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
devsandbox init-db      # optional; serve also migrates at startup
devsandbox serve        # API + orchestration engine on 127.0.0.1:8000
```

`devsandbox serve --no-engine` serves HTTP without executing tasks.
`devsandbox --local ...` uses an in-process ASGI app and also does **not** start
the engine. Tasks only execute in a `serve` process with the engine enabled.

### Make it code an existing project

```bash
devsandbox submit path/to/project.yaml   # prints PROJECT_ID
devsandbox plan PROJECT_ID               # analyzes repo + creates plan
devsandbox show-plan PROJECT_ID --md
devsandbox approve PLAN_ID               # plan id, not project id
devsandbox start PROJECT_ID
devsandbox status PROJECT_ID --watch
devsandbox report PROJECT_ID --out report.md
```

For a brand-new project, point a spec at a directory. A non-git directory is
imported as an initial `main` commit. Validate offline first with
`devsandbox validate-spec path/to/project.yaml`.

### Review and merge (manual)

Validated work is on `devsandbox/PROJECT_ID/staging` in the per-project bare
mirror under the data root, not automatically in the source repository:

```bash
MIRROR="$DEVSANDBOX_DATA_ROOT/repos/PROJECT_ID/repo.git"
BRANCH="devsandbox/PROJECT_ID/staging"
git -C /path/to/repo fetch "$MIRROR" "${BRANCH}:refs/heads/${BRANCH}"
git -C /path/to/repo log main.."$BRANCH"
git -C /path/to/repo diff main.."$BRANCH"
git -C /path/to/repo merge --no-ff "$BRANCH"
```

If you submitted a non-git directory, clone the mirror instead
(`git clone "$MIRROR" project-review`).

### Configuration essentials

- All settings use the `DEVSANDBOX_` prefix; nested keys use `__`
  (`DEVSANDBOX_API__PORT`, `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT`).
- `.env` is read, but only prefixed keys become settings. Reference provider
  credentials from `[secrets.<name>]` in `devsandbox.toml`.
- Defaults: data root `/var/lib/devsandbox`; database
  `postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox`.
- A live agent needs `agent.default_model`; there is no implicit fallback. For a
  local OpenAI-compatible endpoint also set `agent.base_url`, and for a host
  model set `security.allow_network_default=true` (specs cannot widen this).
- `agent.default_runtime` is `opencode`; `fake-script` is a deterministic
  adapter used by tests and drills and needs no model.
- Production should pin agent, validator, and egress-proxy images by digest and
  set `security.require_pinned_images=true`; see [`images.md`](images.md).
- Full settings list: [`config.md`](config.md).

## Change recipes

| If you are changing… | Touch | Prove with |
| --- | --- | --- |
| A setting or security policy | `config/settings.py`, `config/policy.py`, `docs/config.md` | `tests/unit/test_config_settings.py`, `test_config_policy.py`, `tests/security/test_policy.py` |
| The project spec schema | `spec/models.py`, `docs/spec.md` | `tests/unit/test_spec_loader.py` |
| An HTTP route | `api/routes/`, `api/app.py`, `api/schemas.py`, `docs/api.md`; regenerate `docs/openapi.json` | `tests/integration/test_api.py`, `tests/unit/test_openapi_schema.py` |
| A CLI command | `cli/app.py`, `cli/client.py`, `docs/cli.md` | `tests/integration/test_cli.py` |
| Task/project lifecycle or recovery | `orchestrator/state_machine.py`, `engine.py`, `project_service.py`, `recovery.py`, `docs/orchestrator.md`, `docs/recovery.md` | `tests/unit/test_orchestrator_state_machine.py`, `tests/integration/test_orchestrator_*`, E2E |
| An agent adapter or prompt | `agents/interfaces.py`, `registry.py`, `opencode.py`, `prompt.py`, `docs/agents-opencode.md` | `tests/unit/test_agent_*`, opt-in OpenCode smoke |
| Container isolation or resources | `sandbox/manager.py`, `sandbox/profiles/`, `docs/sandbox.md` | `tests/unit/test_sandbox.py`, `tests/security/test_container_isolation.py` |
| Git, workspaces, or integration | `gitmgr/manager.py`, `docs/gitmgr.md` | `tests/unit/test_gitmgr.py`, `tests/security/test_git_hostile.py`, E2E invariants |
| Validation checks or policy | `validation/`, `docs/validation.md` | `tests/unit/test_validation_*`, `tests/security/*`, E2E |
| Planning or replans | `planner/`, `docs/planner.md` | `tests/unit/test_validate_graph.py`, `tests/integration/test_planner_service.py` |
| Scheduling, pressure, or fairness | `scheduler/`, `docs/scheduler.md` | `tests/unit/test_scheduler_*`, `tests/integration/test_scheduler_*` |
| Repo analysis or context bundles | `context/`, `docs/context.md` | `tests/unit/test_context_*`, `tests/integration/test_artifact_repo.py` |
| Reports | `reporting/`, `docs/reporting.md` | `tests/integration/test_reporting_golden.py` |
| The database schema | `db/models.py`, new `db/migrations/versions/` revision, `docs/db.md` | `tests/integration/test_db_migrations.py`, `test_task_repo.py` |
| Retention, audit, or deployment | `storage/`, `audit/`, `docker/`, `deploy/`, matching docs | `tests/unit/test_storage_janitor.py`, `test_audit.py`, `test_deployment_assets.py` |

## Tests and checks

| Layer | Command | Notes |
| --- | --- | --- |
| Fast gate | `make check` | ruff + format check + mypy strict + unit/contract |
| All pytest | `make test` | Full suite; Docker tests skip when no daemon |
| Unit | `pytest -q -m unit` | Fakes, no external services |
| Integration | `make integration` | PostgreSQL-backed; no Docker tests |
| Security | `make security` | Docker isolation, secrets, policy, hostile git |
| E2E | `make e2e` | Real Docker + Git + PostgreSQL; excludes randomized soak |
| Soak | `make soak` | Randomized crash/restart cycles (default 50) |
| Full gate | `make check-all` | All stages with a pass/fail summary; non-zero on failure |
| Drills | `make drills` | Opt-in Docker/PostgreSQL/disk/idle drills |
| Audit / supply chain | `make audit` / `make supply-chain` | pip-audit, gitleaks, SBOM, Grype; optional tools |

`scripts/check-all.sh` runs every stage even when an earlier one fails. Use
`CHECK_ALL_FAIL_FAST=1` to stop at the first failure, `CHECK_ALL_DRY_RUN=1` to
print the plan, and `DEVSANDBOX_SOAK_RUNS` to shorten the soak. The E2E suite
asserts the seven invariants in `tests/e2e/invariants.py`; see
[`acceptance.md`](acceptance.md) for the criteria-to-test matrix.

## Conventions and traps

**Conventions**

- Python 3.12 only; full type hints; `mypy --strict` on `devsandbox`.
- Ruff line length 100; imports sorted; modern syntax (`StrEnum`, `|` unions).
- Async SQLAlchemy 2.x and async service methods. Do not hold a DB transaction
  across planning, Docker, or git operations.
- Pydantic v2 models; specs use `extra="forbid"` and reject inline secret
  literals.
- Raise typed errors from `common/errors.py`; no bare `Exception` across module
  boundaries.
- Route every log write through `common/redaction.redact()`.
- New modules ship with unit tests, a fake in `tests/fakes/`, and a short
  `docs/<module>.md`.
- Commit messages follow the existing style: `feat(scope): ...`,
  `fix(scope): ...`, `docs: ...`, `test(scope): ...`.

**Traps**

- `main` is untouched by design. The staging branch lives in the mirror under
  the data root; fetch it before reviewing.
- `serve --no-engine` and `--local` never run tasks. Use `serve`.
- A spec's `agent` block is stored but does not override runtime/model; those
  come from `DEVSANDBOX_AGENT__*`.
- `default_model` alone is not enough for a local endpoint — set `base_url` and
  allow network. There is no fallback model.
- `approve` takes a plan id; `start` takes a project id.
- `FAILED` tasks require a replan; `BLOCKED` tasks can be re-opened with
  `devsandbox retry TASK_ID` (`--reset-budget` is explicit and audited).
- A database with DevSandbox tables but no `alembic_version` cannot auto-upgrade;
  recreate it or stamp the matching revision first.
- Push failure after integration is non-fatal: local staging remains
  authoritative and a later integration retries the non-force push.
- The agent workspace and its `.git` are hostile input. Dangerous git config,
  hooks, symlink escapes, submodules, `.git/**` edits, and oversized blobs are
  rejected before integration.
- Docker tests skip when no daemon is available; a green `make check` does not
  prove container isolation. Run the security/E2E suites for those changes.

## Read next

Read this file, one module doc, and the matching plan. Do not read every plan;
`plans/` is the design record and some details have been superseded by code.

| Your task | Read |
| --- | --- |
| First run / demo | [`quickstart.md`](quickstart.md) |
| Settings and policy | [`config.md`](config.md), [`security-review.md`](security-review.md), `plans/01-foundation.md` |
| Specs | [`spec.md`](spec.md), `plans/01-foundation.md` |
| Sandbox and isolation | [`sandbox.md`](sandbox.md), [`security-review.md`](security-review.md), [`deployment.md`](deployment.md) |
| Agent / OpenCode | [`agents-opencode.md`](agents-opencode.md), `plans/04-agent-adapter-opencode.md` |
| Repo analysis and context | [`context.md`](context.md), `plans/05-context-manager.md` |
| Planning | [`planner.md`](planner.md), `plans/06-planner.md` |
| Scheduling | [`scheduler.md`](scheduler.md), `plans/07-scheduler-resources.md` |
| Orchestrator and recovery | [`orchestrator.md`](orchestrator.md), [`recovery.md`](recovery.md), `plans/08-orchestrator-core.md` |
| Validation | [`validation.md`](validation.md), `plans/09-validation-integration.md` |
| API, CLI, reports | [`api.md`](api.md), [`cli.md`](cli.md), [`reporting.md`](reporting.md), `plans/10-api-cli-reporting.md` |
| Database, backup, restore | [`db.md`](db.md) |
| Storage, audit, supply chain | [`storage.md`](storage.md), [`audit.md`](audit.md), [`supply-chain.md`](supply-chain.md) |
| Deployment and operations | [`deployment.md`](deployment.md), [`drills.md`](drills.md) |
| Tests and acceptance | [`acceptance.md`](acceptance.md), `plans/11-testing-e2e-acceptance.md` |
| Shared contracts | `plans/00-overview-and-contracts.md`, `plans/README.md` |

## Hand-off brief

When delegating a change to another agent, give it this file plus the relevant
module doc and plan, and say:

> Read `docs/context-quick-start.md` first. For this change, also read
> `docs/<module>.md` and `plans/<NN>-<module>.md`. Preserve the invariants, keep
> policy in settings, add tests at the closest layer, update the docs, and run
> `make check` before reporting back. Do not modify `main`, push, or widen policy.

## Definition of done

- [ ] The behavior change is covered by a test at the closest layer (unit →
  integration → security → E2E).
- [ ] `make check` passes (ruff, format, mypy strict, unit/contract).
- [ ] Targeted integration/security/E2E suites pass for the touched area;
  `make check-all` for cross-cutting, security, or recovery changes.
- [ ] Invariants and existing tests are untouched or strengthened, never
  weakened.
- [ ] Relevant docs are updated; API changes regenerate `docs/openapi.json`
  with `python3 scripts/export_openapi.py`.
- [ ] No secret, token, or credential appears in code, config, fixtures, logs,
  or Git.

## Keep this file honest

This file is only useful while it is short and true. When you change the
architecture, an invariant, a primary command, or the module map, update it in
the same change. Prefer deleting stale detail over adding more.
