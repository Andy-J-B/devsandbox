# Orchestrator core (plan 08)

The orchestrator is the only component that owns authoritative task/project
state and drives work from "a plan exists" to "validated code is committed to
staging". It coordinates the scheduler, git manager, context manager, agent
adapter, validator, sandbox manager, and cost ledger through the Protocols
defined by plans 00–07/09; leaf modules never call each other directly through
the orchestrator's code paths.

Modules:

- `orchestrator/state_machine.py` — task/project transitions (00 §7) as data,
  guard conditions, terminal-state handling, project status derivation
- `orchestrator/project_service.py` — submit/analyze/plan/approve, start/pause/
  resume/cancel, mode changes, needs-attention
- `orchestrator/engine.py` — lease, scheduling loop, per-attempt pipeline,
  failure/retry handling, policy and budget enforcement, observability,
  graceful shutdown
- `orchestrator/failures.py` — failure classification, retry decisions,
  backoff, repeated-failure and infra-failure limits
- `orchestrator/recovery.py` — startup reconciliation and orphan cleanup
  (decision table in [`recovery.md`](recovery.md))
- `orchestrator/models.py` — `OrchestratorConfig`, `EngineStatus`,
  `RecoveryAction`, `RecoveryReport`
- `orchestrator/events.py` — audit event vocabulary for the engine

## State machines

`TASK_TRANSITIONS` and `PROJECT_TRANSITIONS` are the single source of truth.
`is_legal` / `is_legal_project` are injected into the DB repositories, so every
status write is a compare-and-swap that also validates the edge. `tasks.status`
is only written by `TaskRepo.transition` (enforced by
`tests/unit/test_orchestrator_acceptance.py`).

Guard conditions are modelled separately (`TransitionContext`,
`TASK_GUARDS`) and exhaustively tested. The repository's structural check is
the hard gate; the engine enforces retry/infra budgets before it attempts a
retry edge, so a guarded edge is never taken with an exhausted budget.

Task edges of note:

- `SCHEDULED → READY` is a documented recovery edge: an attempt that died
  before any container work is reverted to the queue instead of failing the
  task (recovery row 1).
- `BLOCKED → PENDING` is the only outgoing edge from a terminal task state and
  exists for explicit user re-opening/replanning (00 §7). `COMPLETED`, `FAILED`,
  and `CANCELLED` have no outgoing edges.
- Retry edges (`RUNNING|VALIDATING|INTEGRATING → RETRY_WAIT`) are guarded by
  the remaining task retry budget; the recovery revert is guarded by the
  remaining infra-failure cap.

Project status derivation (`derive_project_status`):

1. no tasks → `COMPLETED`
2. any non-terminal task → `EXECUTING`
3. otherwise any `FAILED`/`BLOCKED` task → `FAILED`
4. otherwise any `CANCELLED` task → `CANCELLED`
5. otherwise → `COMPLETED`

Paused/cancelled project states are orchestration decisions and are never
derived from task rows.

## Project lifecycle

`ProjectService` (one instance per engine) implements spec §4 steps 1–6/15:

- `submit_project(spec)` validates and persists the spec → `LOADED`
- `analyze()` initializes the canonical repo, analyzes it, and runs baseline
  validation, leaving the project in `ANALYZING`
- `plan()` moves `ANALYZING → PLANNING` and asks the planner for a task graph.
  Supervised projects stop at `AWAITING_APPROVAL`; autonomous projects
  auto-approve and go straight to `EXECUTING`.
- `approve_plan()` activates the plan and moves `AWAITING_APPROVAL → EXECUTING`
- `start/pause/resume/cancel` are persisted commands; pause is idempotent,
  resume is a no-op unless the project is paused, and cancel transitions every
  non-terminal task to `CANCELLED`.
- `set_mode()` allows autonomous → supervised automatically; supervised →
  autonomous requires `requested_by="user"` (never a planner/agent).
- `needs_attention()` emits `project.needs_attention` for supervised work that
  cannot proceed without a human.

Each method owns its transaction through the injected session factory, so no
transaction is held open across planning or git operations.

## Execution loop

One `asyncio` loop per process, guarded by a DB lease row. `run_forever()` is
the daemon form; `run_until_idle()` is the deterministic test/CLI form.

Each round:

1. Refresh the lease (`lease_ttl_seconds / 2` cadence); a lost lease raises
   `OrchestratorAlreadyRunning` and the process must stop.
2. Refresh readiness for executing projects: `PENDING → READY` when every
   dependency is `COMPLETED`; failed/cancelled dependencies propagate
   `BLOCKED` with a reason; due `RETRY_WAIT` rows return to `READY`.
3. Build a `SchedulingSnapshot` from DB rows, host resources, pressure,
   per-project limits, and remaining model budget.
4. Ask the scheduler for decisions. Launch decisions spawn one attempt task;
   defer decisions are recorded as `SCHEDULE_DECISION` events, deduplicated
   while the reason is unchanged.
5. Wait for a completion, a command, or the tick.

`_launch` has an in-process `_launching` guard plus the task's
`READY → SCHEDULED` CAS, so a concurrent launch of the same task creates at
most one attempt.

### Per-attempt pipeline (persist before act)

1. `READY → SCHEDULED` (CAS) and insert the `attempts` row, committed before
   any external side effect. Attempt ids are deterministic
   (`<task_id>-a<n>`, `n` = existing attempts + 1), and so are branch names.
2. `git.create_workspace`, persist the workspace path/branch/base commit, then
   `context.get_task_context`.
3. Build the `AgentRunRequest` through `SecurityPolicy` (see below), transition
   `SCHEDULED → RUNNING`, record the container intent, then call the agent.
   The agent result is persisted as soon as it is known.
4. `git.collect_changes`, then the restricted-path check.
5. `RUNNING → VALIDATING`, run independent `validator.validate_task`, persist
   the report.
6. On pass: `VALIDATING → VALIDATED → INTEGRATING`, then
   `git.prepare_integration` → `validator.validate_integration` →
   `git.commit_integration` (CAS) → `integrations` row → `COMPLETED` →
   `context.record_task_outcome` → remove the workspace.
7. On failure: classify and either `RETRY_WAIT` (with `retry_not_before`) or
   terminal `FAILED`; dependents become `BLOCKED`.

**Agent exit 0 is never completion.** `COMPLETED` requires an independent
validation pass *and* a committed integration. The engine wraps the attempt in
`try/finally`: cancellation and unexpected exceptions still persist state, and
critical persistence runs under `asyncio.shield`.

### Integration serialization

Only one integration may touch a project's staging branch at a time. The engine
holds an in-process `asyncio.Lock` per project around the integration step
(prepare → validate → commit), in addition to the git manager's own CAS on the
staging ref. With N > 1 attempts in a project, commits are serialized and
counted exactly once.

## Failures, retries, and circuit breakers

`FailureClassifier` maps every failure source to
`FailureRecord{kind, retryable, counts_against_budget, signature, summary,
diagnostics_path, detail}`. It covers agent crash/nonzero exit, timeout, OOM,
sandbox/infra errors, validation failures by kind, restricted-path/tampering
violations, git conflicts, interrupted integrations, budget deferrals, and
policy violations.

Retry policy:

- Budget is `task.attempt_count` vs `max_retries` clamped by `SecurityPolicy`
  (the spec/planner cannot raise it).
- `RETRY_WAIT` uses capped exponential backoff with jitter, stored in
  `tasks.retry_not_before`; readiness only requeues the task once the deadline
  passes.
- **Infra failures do not consume the task's retry budget.** They are bounded
  separately by `OrchestratorConfig.max_infra_failures` (default 8; see
  `failure_json.infra_failures`). Their backoff grows with the number of
  consecutive infra failures rather than the task attempt count, so a Docker
  daemon restart that takes tens of seconds does not exhaust the breaker before
  the daemon is available again. Git conflicts rebase on the new staging head
  and include the conflicting files in the next prompt.
- **Repeated identical failures stop early.** The signature is kind + normalized
  error hash; `repeat_failure_threshold` (default 2) consecutive identical
  failures end the task with `FAILED` and `requires_attention` instead of
  burning more model usage.
- A policy violation fails immediately, is never retried, and is surfaced for
  the user.
- A budget deferral is never a task failure; the task stays queued.
- On exhaustion, diagnostics (logs, validation report, diff, workspace path)
  are preserved, downstream tasks are `BLOCKED` with a reason, and (supervised)
  the project gets a `needs_attention` event.

Global circuit breakers:

- `max_total_attempts` per project (default 100): further launches are refused,
  a `CIRCUIT_BREAKER` event is written, and the project is paused.
- `max_replans` per project (default 2): autonomous replanning after failures
  is capped; exceeding it writes `CIRCUIT_BREAKER` and stops replanning.
- Model budget from settings/policy is checked before every launch; exhaustion
  is a `MODEL_BUDGET` deferral, not a failure.
- No path retries forever: retry budget, infra cap, repeated-failure threshold,
  attempt cap, and replan cap all bound loops.

## Policy enforcement

The orchestrator is the only component that constructs `SandboxRequest` /
`AgentRunRequest` from `SecurityPolicy`. When a task spec or agent asks for
more CPU/memory/disk/pids, a longer timeout, or more network access than policy
allows, the request is clamped to the policy limit and a `POLICY_DENIED` event
records the denial. Escalation is ignored, never honoured. Integration is
staging-only: there is no main-branch merge path and no setting that can enable
one. Unknown settings such as a stale `DEVSANDBOX_SECURITY__ALLOW_MAIN_MERGE`
are rejected at startup by `extra="forbid"` rather than silently ignored.

## Cost and usage ledger

`cost_entries` are recorded in two streams:

- **infra** — container-seconds × `infra_cost_per_second_usd` (default 0 for
  local runs)
- **inference** — derived from the agent's `TokenUsage`

`usage_counters` track tokens/requests per provider window. Per-project budget
usage is read by the scheduler guard and by `EngineStatus`; the pre-launch
budget check is a circuit breaker, not a retry.

## Observability and control

Every transition, scheduling decision, policy denial, circuit breaker, recovery
action, and lease event is an append-only audit event (`orchestrator/events.py`).
Payloads are redacted before storage.

`engine.status()` returns an `EngineStatus` snapshot: running attempt task ids,
ready queue, current deferrals, pressure, per-project token usage, total
attempts, paused flag, lease holder, and the last round time.

`request_shutdown()` stops launching; `shutdown()` waits up to
`shutdown_timeout_seconds` for in-flight attempts, cancels the remainder,
persists their state (shielded), emits `ENGINE_STOPPED`, and releases the lease.

## Configuration

`OrchestratorConfig` is orchestrator-local and cannot be changed from a project
spec: lease name/TTL, tick/idle/shutdown timings, retry/infra/attempt/replan
caps, backoff and jitter, repeated-failure threshold, workspace retention, and
the infra cost rate. Values that overlap with plan-01 settings are derived by
`OrchestratorConfig.from_settings`; concurrency and heavy resource limits come
from plan 01/07.
