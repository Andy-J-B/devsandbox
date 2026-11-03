# Crash recovery (plan 08)

The orchestrator can die at any point between "intent persisted" and "side
effect completed". `RecoveryService` reconciles persisted rows with the outside
world (Docker containers, git workspaces, integration rows) **before** the
execution loop starts.

## Startup order

1. Acquire the exclusive orchestrator lease (`leases` row). No recovery runs
   without it, so two processes never reconcile concurrently.
2. Run `RecoveryService.recover()`.
3. Resume the returned actions in the engine's attempt runners.
4. Enter the scheduling loop.

Every recovery action is derived from *persisted* state plus observations, and
is idempotent: crashing during recovery and running it again converges on the
same result. Actions are persisted as `RECOVERY_ACTION` events; the pass ends
with one `RECOVERY_COMPLETED` event carrying a summary, the destroyed container
ids, and the removed workspace paths.

## Inputs

- All non-terminal tasks (`SCHEDULED`, `RUNNING`, `VALIDATING`, `VALIDATED`,
  `INTEGRATING`) and their current/latest attempts.
- Active attempts with no active task (orphans).
- `sandbox.inspect(container_id)` and `sandbox.list_managed()`.
- Workspace existence, recorded `head_commit`, and
  `git.collect_changes(workspace)`.
- Persisted `attempt.validation_report_json`.
- `integrations` rows (`staging_commit_before`, `staging_commit_after`,
  status) and the current staging head (`git.staging_head`).

A project that is not `EXECUTING`/`PAUSED` (terminal or awaiting approval) is
not resumed: its attempt is abandoned, its container destroyed, and the action
is recorded as `NONE`.

## Decision table

Every row is covered by `tests/integration/test_orchestrator_recovery.py`,
including crash injection at the boundaries below.

| # | Persisted task state | Observation | Action | Result |
|---|---|---|---|---|
| 1 | `SCHEDULED` | no container, no agent work | `REVERT_READY` | attempt `abandoned`; `SCHEDULED → READY` (does not consume the retry budget) |
| 2 | `RUNNING` | container still running | `KILL_AND_RETRY` | container killed, attempt abandoned, `RETRY_WAIT` as an **infra** failure (does not burn the retry budget) |
| 3 | `RUNNING` | container exited/dead/created | `RESUME_COLLECT` | continue at `git.collect_changes` |
| 4 | `RUNNING` | container gone but workspace has commits/changes | `RESUME_COLLECT` | continue at `git.collect_changes` |
| 5 | `RUNNING` | nothing recoverable | `RETRY_INFRA` | `RETRY_WAIT` with an infra failure recorded |
| 6 | `VALIDATING` | validation report persisted | `RESUME_VALIDATION_DECISION` | skip re-running validation and continue the pass/fail decision |
| 7 | `VALIDATING` | no report | `RERUN_VALIDATION` | re-run validation (idempotent) and persist the report |
| 8 | `INTEGRATING` | candidate prepared, not committed | `RESET_AND_RETRY` | abort the interrupted candidate and restart the integration step |
| 9 | `INTEGRATING` | staging contains the attempt's commit | `MARK_COMPLETED` | integration marked `integrated`, attempt `completed`, task `COMPLETED` (ancestry check) |
| 10 | `INTEGRATING` | staging advanced but DB has no committed integration | `RESET_AND_RETRY` | reset staging to the recorded `staging_before`, abort the candidate, retry integration |

### MVP policy for row 2

Plan 08 allows either re-attaching to a still-running container or killing and
retrying. The MVP chooses **kill + retry**, because re-attaching would require
tracking a remaining timeout and risk two writers in the same workspace. The
retry is classified as an infra failure, so it is bounded by
`max_infra_failures` rather than the task's model-retry budget.

### Row 9 ancestry check

`MARK_COMPLETED` requires evidence that staging really contains the attempt's
integration commit: either the staging head equals the recorded
`staging_commit_after`, or `git.is_ancestor(repo, candidate, head)` returns
true. A moved staging head without that evidence falls through to row 10, which
prefers rolling back to the recorded pre-integration commit over guessing.

## Engine resumption

`RESUME_COLLECT`, `RERUN_VALIDATION`, `RESUME_VALIDATION_DECISION`,
`RESUME_INTEGRATION`, and `RESET_AND_RETRY` are handed to the engine, which
continues the attempt from the right pipeline stage using the persisted
workspace, change set, validation report, and integration row. `REVERT_READY`,
`KILL_AND_RETRY`, `RETRY_INFRA`, `MARK_COMPLETED`, and `NONE` need no further
resumption because recovery already persisted their outcome.

The integration step is still serialized per project and still uses the git
manager's CAS, so a recovered integration cannot duplicate a commit.

## Orphans

After the task pass, recovery:

- **reaps containers** returned by `sandbox.list_managed()` that no active
  attempt owns (`labels["devsandbox.attempt"]` is unknown or the container is a
  stale `ds-<attempt>`), destroying them and reporting their ids;
- **removes workspaces** older than `workspace_retention_hours` (default 72 h)
  when they are not owned by an active attempt;
- **abandons active attempts** whose task is no longer active, marking them
  `abandoned` and emitting `attempt.abandoned`.

Postgres is the source of truth: recovery never infers task completion from a
container alone, and never trusts an agent's self-report.
