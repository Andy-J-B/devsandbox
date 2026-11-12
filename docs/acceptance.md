# Acceptance matrix (plan 11)

**Last full run:** 2026-11-07 (local macOS host)
**Live free-model smoke test:** 2026-11-09 — see
[Live free-model smoke test](#live-free-model-smoke-test).
**Scope:** DevSandbox MVP, plans 01–11, exercised through the real Docker
sandbox manager, real Git manager, real PostgreSQL, and the real orchestrator.
The agent is the deterministic `FakeScriptAdapter`/`ScriptedAgent`; no LLM is
required for the acceptance suite.

The 13 criteria below are reconstructed from the E1–E15 → criteria mapping in
[`plans/11-testing-e2e-acceptance.md`](../plans/11-testing-e2e-acceptance.md)
(the original spec §13 is not checked into this repository). Each row names the
primary test(s); the E2E footer lists the run evidence.

## How to reproduce

```bash
# static checks: ruff + ruff format + mypy strict + unit/contract
scripts/check.sh

# PostgreSQL-backed integration suite (same command as CI's `integration` job)
python3 -m pytest -q -m "integration and not docker"

# real PostgreSQL + Docker + Git end-to-end scenarios
DEVSANDBOX_E2E=1 python3 -m pytest -q tests/e2e

# randomized SIGKILL soak (default 50 runs)
DEVSANDBOX_E2E=1 DEVSANDBOX_SOAK_RUNS=50 \
  python3 -m pytest -q tests/e2e/test_e2e_crash_soak.py -k soak

# security + cross-module contract suites
DEVSANDBOX_E2E=1 python3 -m pytest -q tests/security tests/contract
```

Prerequisites: Docker daemon reachable by the current user,
`devsandbox/validator:latest` available, and PostgreSQL 16 reachable via
`DEVSANDBOX_DATABASE_URL` (the suite creates isolated tables and drops them per
test). To reuse an existing PostgreSQL service, set
`DEVSANDBOX_TEST_DATABASE_URL` to its `postgresql+asyncpg://...` URL; CI uses
this to share the job's Postgres service. Without that override, tests start an
ephemeral PostgreSQL container via testcontainers when Docker is available.

## 13-criteria matrix

| # | Criterion | Primary evidence | Result |
|---|---|---|---|
| 1 | Accept a validated project spec plus a **local or remote** repository, persist the project, and analyze the repo. | `tests/e2e/test_e2e_core.py::test_e1_submit_local_repo_and_generate_plan`, `::test_e2_submit_file_remote_and_run`; `tests/unit/test_spec_loader.py`; `tests/integration/test_api.py` | Pass |
| 2 | Produce a versioned, dependency-aware task plan that is reviewable and does not execute before approval. | `test_e1_submit_local_repo_and_generate_plan` (goal + dependent `final-verification`, all `PENDING`, `AWAITING_APPROVAL`); `tests/unit/test_validate_graph.py`; `tests/integration/test_planner_service.py` | Pass |
| 3 | In supervised mode, wait for human approval; after approval, execute the plan. | `test_e3_supervised_does_not_execute_before_approval` (no agent requests, no attempts, no containers before approval; completes after it) | Pass |
| 4 | Run agents only in isolated, resource-limited, non-root containers with no Docker socket, host mounts, or default network. | `test_e4_agent_runs_in_locked_down_container`; `tests/security/test_container_isolation.py` (5 tests: socket, host files, write-outside, config, metadata/loopback); `test_network_defaults_to_off` | Pass |
| 5 | Complete a small task end-to-end: agent edit → independent validation → validated commit on staging. | `test_e5_small_task_completes_and_lands_on_staging` (exactly one `integrated` row; staging head equals `staging_commit_after`; commit reachable from staging) | Pass |
| 6 | Never trust agent claims: validation runs independently and no task completes without a passed report. | `test_e6_lying_agent_is_rejected_by_independent_validation` (exit 0, no changes, `FAILED`, `blame=agent`); soak invariants 1–2; `tests/unit/test_validation_*`; `tests/contract/test_contract_semantics.py::test_validator_returns_typed_report` | Pass |
| 7 | Enforce restricted paths and policy non-escalation, with no unbounded retry loop. | `test_e15_restricted_path_is_rejected_without_unbounded_retries` (`attempts <= max_retries + 1`, `task.exhausted`); `tests/security/test_policy.py`; `tests/security/test_git_hostile.py` | Pass |
| 8 | Preserve validated work on the staging branch, including conflict retries on the new staging head. | `test_e5_small_task_completes_and_lands_on_staging`; `test_e7_main_ref_is_byte_for_byte_unchanged`; `test_e12_conflict_is_retried_on_new_staging_head`; soak invariant 1 | Pass |
| 9 | Never modify `main` (no push, no automatic merge). | `test_e7_main_ref_is_byte_for_byte_unchanged` (ref byte-identical, staging advanced); `tests/security/test_policy.py::test_push_is_denied_by_default`, `::test_push_never_allows_main_even_when_enabled`; soak invariant 3 | Pass |
| 10 | Recover from real process crashes mid-run, mid-validation, and mid-integration without duplicate commits or orphan containers/workspaces. | `tests/e2e/test_e2e_crash_soak.py::test_e8_sigkill_mid_run_recovers_without_duplicates`, `::test_e8_sigkill_checkpoint_recovers[after_validation_report-mid-validation]`, `::test_e8_sigkill_checkpoint_recovers[after_staging_advance_before_db-mid-integration]`; soak (50 randomized SIGKILL cycles, invariants 1–7); `tests/integration/test_orchestrator_recovery.py` | Pass |
| 11 | Retry within budget with failure context; exhausted retries fail the task, block dependents, and preserve diagnostics; conflicts retry on a fresh head. | `test_e9_flaky_agent_retries_then_succeeds_with_failure_context`; `test_e10_exhausted_retries_fail_task_and_block_dependents`; `test_e12_conflict_is_retried_on_new_staging_head`; soak invariant 5 (`attempt_count <= max_retries + 1 + infra allowance`) | Pass |
| 12 | Respect timeouts, OOM limits, host-pressure deferrals, and the concurrency cap. | `test_e11_agent_timeout_is_killed_and_fails`; `test_e11_memory_hog_is_oom_killed`; `test_e11_pressure_defers_scheduling` (`PRESSURE`); `test_e12_conflict_is_retried_on_new_staging_head` (`peak_active == 2`); `tests/security/test_resource_abuse.py` | Pass |
| 13 | Run autonomous mode across a multi-task DAG with a bounded replan, and emit a final report containing changes, tests, failures, outstanding work, and split costs. | `test_e13_autonomous_dag_with_bounded_replan` (4-task DAG, one failure, `max_replans=1`); `test_e14_report_contains_changes_failures_and_costs`; `tests/integration/test_reporting_golden.py` | Pass |

### Invariants enforced after every E2E/soak run

`tests/e2e/invariants.py` checks all seven plan-11 invariants:

1. Each `COMPLETED` task has exactly one integration and a validated commit
   reachable from staging head.
2. No task is `COMPLETED` without a passed validation report.
3. `refs/heads/main` is unchanged.
4. No managed containers remain and no orphan workspaces exist beyond the
   retention policy (diagnostics workspaces are allowed only for failed or
   abandoned attempts).
5. `attempt_count <= max_retries + 1 + infra allowance`.
6. Event transitions form legal paths per the state machine.
7. No canary secret appears in the DB, logs, prompts, reports, or Git history.

## Run evidence

| Suite | Command | Result |
|---|---|---|
| Whole repository (default env) | `python3 -m pytest -q` | **571 passed**, 39 skipped (66.6 s); Docker/PostgreSQL integration tests included, E2E/security Docker gates skipped by design |
| Unit + contract + lint/types | `scripts/check.sh` | ruff/format clean, mypy strict clean, **435 passed**, 175 deselected (29.5 s) |
| Unit + contract + lint/types (re-run 2026-11-09) | `scripts/check.sh` | ruff/format clean, mypy strict clean, **436 passed**, 175 deselected (33.4 s) |
| E2E E1–E7, E9–E15 — run 1 | `DEVSANDBOX_E2E=1 pytest -q tests/e2e/test_e2e_core.py tests/e2e/test_e2e_retries_autonomy.py tests/e2e/test_e2e_crash_soak.py -k "not soak"` | **16 passed**, 4 deselected (130.9 s) |
| E2E E1–E7, E9–E15 — run 2 | same | **16 passed**, 4 deselected (128.8 s) |
| E2E E1–E7, E9–E15 — run 3 | same | **16 passed**, 4 deselected (129.8 s) |
| E8 crash recovery — run 1 | `pytest -q tests/e2e/test_e2e_crash_soak.py::test_e8_sigkill_mid_run_recovers_without_duplicates tests/e2e/test_e2e_crash_soak.py::test_e8_sigkill_checkpoint_recovers` | **3 passed** (25.8 s) |
| E8 crash recovery — run 2 | same | **3 passed** (25.5 s) |
| E8 crash recovery — run 3 | same | **3 passed** (25.6 s) |
| Randomized soak | `DEVSANDBOX_SOAK_RUNS=50 pytest -q tests/e2e/test_e2e_crash_soak.py -k soak` | **4 passed** (3 E8 + soak test; 50 randomized crash/restart cycles, zero invariant violations; 415.2 s) |
| Security + contract | `DEVSANDBOX_E2E=1 pytest -q tests/security tests/contract` | **65 passed** (32.4 s) |

Environment: macOS (Darwin), Python 3.12, Docker Desktop server 29.1.3,
`devsandbox/validator:latest` (uid 1000, Python 3.12.16, pytest 9.2.0, git
2.47.3), PostgreSQL 16 on `localhost:5432`. Soak seed `20261106`.

Notes:

- The three "non-soak" E2E runs deliberately exclude the crash/soak module
  because `-k "not soak"` matches the module node id. E8 was therefore run
  three additional consecutive times, and the soak was run once. Together this
  covers all E1–E15 for three consecutive runs.
- The E2E harness uses a real PostgreSQL database and isolated tables, real Git
  repositories under a temporary data root, and real Docker containers. The
  deterministic agent makes the runs reproducible.

### Live free-model smoke test

Run on **2026-11-09** through the real `OpenCodeAdapter` and Docker sandbox
manager against a local Ollama OpenAI-compatible endpoint. OpenCode Zen free
models were tried first, but the account's free quota was exhausted (HTTP 429
`FreeUsageLimitError`), so the run used a local free model instead.

| Field | Value |
|---|---|
| Provider / model | `ollama` / `qwen3:8b` |
| Endpoint | `http://host.docker.internal:11434/v1` |
| Network policy | `full` (the container must reach host Ollama) |
| Exit status | `exited_ok`, exit code `0` |
| Token usage | input **4,094**, output **1,702**, total **5,796** |
| Cost | **$0.00** |
| Duration | **160.05 s** |
| Result | **Pass** — image built, OpenCode ran, `hello.txt` contained `hello from devsandbox\n`, and the integration assertion passed |

```bash
DEVSANDBOX_RUN_OPENCODE_INTEGRATION=1 \
DEVSANDBOX_OPENCODE_LIVE=1 \
DEVSANDBOX_OPENCODE_PROVIDER=ollama \
DEVSANDBOX_OPENCODE_MODEL=qwen3:8b \
DEVSANDBOX_OPENCODE_BASE_URL=http://host.docker.internal:11434/v1 \
DEVSANDBOX_OPENCODE_NETWORK=full \
DEVSANDBOX_AGENT_IMAGE=devsandbox/agent:test \
python3 -m pytest -q -s \
  tests/integration/test_agent_image.py::test_live_free_model_small_task
```

The same live test is wired into the manual `opencode-smoke` job in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml). It builds the
agent image once with the tag the test fixture uses
(`devsandbox/agent:test`), defaults to the free
`opencode/deepseek-v4.1-flash-free` model, and reads the API key from the
`OPENCODE_API_KEY` repository secret. Override the provider/model/base URL
with the job's `workflow_dispatch` inputs or repository variables.

`qwen3:8b` completed the task in two OpenCode steps; the adapter summed both
step token counts. It created the required file, but its follow-up attempt to
commit used OpenCode's `task` tool instead of `bash`, so the temporary
workspace still had only the initial commit. This smoke test asserts the real
agent edit and exit status; independent validation and staging integration are
covered by the deterministic E2E suite.

## Security acceptance

`docs/security-review.md` records the threat model, findings F1–F16, and the
accepted residual risks. No high-severity finding remains unresolved:

- F1 (host code execution from workspace Git config) and F2 (network default
  on) are fixed with regression tests.
- F3 (Docker daemon is host-root equivalent), F5 (env secrets visible via
  `docker inspect`), F6 (no disk quota), and F15 (LLM provider egress) are
  documented as accepted MVP risks with follow-up recommendations.

The security suite passes with Docker enabled:
`DEVSANDBOX_E2E=1 python3 -m pytest -q tests/security` → 39 passed. The full
security + contract run → 65 passed.

## Non-functional checks

| Check | Status | Evidence / note |
|---|---|---|
| Cold start | **Measured** | Fresh interpreter import of `devsandbox.api.app`: **0.78 s**, ~85 MB peak RSS on this host. Not a gated benchmark. |
| Idle resource usage | **Inspected, not benchmarked** | The engine uses `idle_sleep_seconds=0.1` and blocks on a wake event up to `tick_seconds=1.0` when idle (`orchestrator/models.py`, `engine.run_forever`); there is no busy loop. No long-duration RSS baseline was recorded. |
| Docker daemon restart | **Not exercised** | A daemon outage surfaces as a sandbox error; startup reconciliation reaps managed containers after the orchestrator restarts (`docs/recovery.md`). Stopping the developer's Docker daemon was not done in this run; treat as a manual pre-production check. |
| PostgreSQL restart | **Not exercised** | `db/session.py` configures `pool_pre_ping=True`, so pooled connections are validated on checkout and replaced after a restart. Lost lease refresh raises `OrchestratorAlreadyRunning`, and the process is expected to be restarted by the service manager. Restarting the shared local PostgreSQL was not done in this run; treat as a manual pre-production check. |
| Disk-full handling | **Not contained (accepted)** | There is no per-workspace disk quota (residual risk F6). Diff size and changed-file caps run in validation pre-checks, logs are capped, and the scheduler gates on `disk_min_free_mb`; an agent can still fill the host filesystem through its bind-mounted workspace. |
| Fresh-machine quickstart | **Not reproduced on a brand-new host** | The E2E harness exercises the same real modules and commands as `docs/quickstart.md`, but the documented quickstart was not replayed verbatim on a clean machine in this run. Recommended before release sign-off. |
| Real OpenCode smoke test | **Pass (free local model)** | 2026-11-09: `ollama/qwen3:8b` through `OpenCodeAdapter` + Docker, 4,094 input / 1,702 output / 5,796 total tokens, **$0.00**, exit 0; `hello.txt` assertion passed. OpenCode Zen free models returned HTTP 429 `FreeUsageLimitError` (quota exhausted) on this key, so the run used local Ollama. Command and detail: [Live free-model smoke test](#live-free-model-smoke-test). |

## Sign-off

The automated acceptance suite covers all 13 criteria with real Docker, real
Git, and real PostgreSQL, and the 50-run randomized crash soak found zero
invariant violations. The opt-in real-OpenCode smoke test has now passed once
with a free local model (`ollama/qwen3:8b`, $0.00, 5,796 tokens). Remaining
sign-off items are operational rather than code-level: replay the
fresh-machine quickstart, and perform the Docker/PostgreSQL restart and
disk-full drills on a dedicated pre-production host. Re-run the live smoke
test against the intended hosted provider once its free quota resets or paid
credentials are configured.
