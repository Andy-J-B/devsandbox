# Scheduler & resource monitor (plan 07)

The scheduler decides **which ready tasks may launch now**. It is split into a
pure decision function, an I/O monitor, DB-backed admission bookkeeping, and a
runtime pressure guard. Planning, execution, and state transitions remain the
orchestrator's job (plan 08).

Modules:

- `scheduler/models.py` — `SchedulingSnapshot`, `ReadyTask`, `RunningTask`,
  `HostResources`, `ScheduleDecision`, `DeferReason`, `PressureState`,
  `SchedulingLimits`, `ModelBudgetState`, `GuardDecision`
- `scheduler/scheduler.py` — pure `select_decisions()` plus the async
  `DeterministicScheduler` adapter
- `scheduler/monitor.py` — `PsutilResourceMonitor` (sampling, pressure
  hysteresis, history ring buffer, background sampler)
- `scheduler/admission.py` — `AdmissionController` (`reserve`/`release` via
  non-terminal `attempts`, peak usage recording)
- `scheduler/guard.py` — `ResourceGuard` (stop launching / preempt signal and
  `RESOURCE_PRESSURE` event)
- `scheduler/config.py` — `SchedulerConfig` derived from `DEVSANDBOX_LIMITS_*`
- `scheduler/interfaces.py` — `Scheduler` and `ResourceMonitor` protocols

## Deterministic decision algorithm

`select_decisions(snapshot)` depends only on the snapshot (no clock, DB, Docker,
or randomness). The orchestrator builds a snapshot once per scheduling round and
gets exactly one decision per ready task.

1. **Pressure gate.** `critical` defers everything with `PRESSURE`. `elevated`
   defers everything when a task is already running (or when
   `pressure_blocks_elevated` is set); when nothing is running, exactly one task
   may start.
2. **Filters.** Paused projects → `PROJECT_PAUSED`; `retry_not_before > now` →
   `BACKOFF`.
3. **Ordering.** Effective priority (priority + aging) descending, critical-path
   length descending, `created_order` ascending, `task_id` ascending. The order
   is total, so the same snapshot always yields the same decision list.
4. **Greedy admission.** A task launches only while all of these hold:
   - global running + admitted < `max_global_concurrency`
   - project running + admitted < min(`max_project_concurrency`, per-project
     override)
   - cumulative CPU fits `min(measured idle, total − committed) × (1 − headroom)`
   - cumulative memory fits `min(available, total − committed) × (1 − headroom)`
   - free disk ≥ `disk_min_free_mb + task.disk_mb` (tighter of data/docker roots)
   - provider model budget covers the task estimate; admitted usage is deducted
     for later tasks in the same round
5. **Aging.** A task deferred for resources `N` rounds gains
   `min(N × aging_per_round, aging_cap)` priority. The counter is carried in the
   snapshot; `update_deferral_counts()` returns the values to persist for the
   next round.

Defer reason codes (all recorded in the decision and therefore in audit events):

`CONCURRENCY_GLOBAL`, `CONCURRENCY_PROJECT`, `INSUFFICIENT_CPU`,
`INSUFFICIENT_MEMORY`, `INSUFFICIENT_DISK`, `PRESSURE`, `MODEL_BUDGET`,
`BACKOFF`, `PROJECT_PAUSED`.

The MVP ships with global concurrency = 1, but the algorithm and tests exercise
N > 1.

Example:

```python
from devsandbox.scheduler import DeterministicScheduler, SchedulingSnapshot

snapshot = SchedulingSnapshot(
    now=now,
    host=await monitor.sample(),
    pressure=await monitor.pressure_state(),
    ready_tasks=ready_tasks,
    running=running_tasks,
    limits=config.scheduling_limits(project_limits),
    model_budget={"opencode": ModelBudgetState(remaining_tokens=50_000)},
    paused_projects=paused,
)
decisions = await DeterministicScheduler(config).select(snapshot)
for decision in decisions:
    if decision.launched:
        ...  # persist intent, then act (orchestrator)
    else:
        ...  # record decision.reason_code in the event payload
```

## Resource monitor

`PsutilResourceMonitor.sample()` produces `HostResources` with:

- smoothed CPU percentage (`psutil.cpu_percent`), logical CPU count, load average
- total/available memory and data-root free disk
- optional Docker-root free disk (tighter value is used for admission)
- committed CPU/memory/disk supplied by the admission controller callback

Availability is `min(measured available, total − committed)`; the scheduler then
applies headroom. The monitor keeps a bounded ring buffer (`history()`) for
reporting and can run a background sampler
(`start_background_sampler()` / `stop_background_sampler()`).

Pressure:

- `critical`: CPU or memory ≥ configured threshold, or free disk < reserve
- `elevated`: CPU or memory ≥ `threshold × pressure_elevated_ratio` (default 85%)
- `ok`: otherwise

The first sample bootstraps the state (so a host that is already critical never
gets a free launch window); later transitions require
`pressure_hysteresis_samples` (default 2) consecutive samples to avoid flapping.

## Admission and reservations

Reservations are derived from `attempts` whose status is **not** terminal
(`completed`, `succeeded`, `failed`, `cancelled`, `released`, `error`). There is
no separate in-memory reservation table, so reservations survive restarts.

```python
admission = AdmissionController(session)
await admission.reserve(attempt_id, task_id=task.id, number=attempt_number)
committed = await admission.committed()      # cpu/memory/disk/attempts
await admission.release(attempt_id)          # idempotent
await admission.record_peak(attempt_id, peak_memory_mb=1536, peak_cpu_percent=87.0)
```

`reserve()` is idempotent for an active attempt and rejects reopening terminal
attempts. Peak usage is stored on `attempts.peak_memory_mb` /
`attempts.peak_cpu_percent` (migration `0004`) for future estimation.

`DbCommittedProvider` adapts an async session factory into the callable that
`PsutilResourceMonitor` accepts as `committed_provider`.

## Runtime pressure guard

`ResourceGuard.evaluate(pressure=..., running=...)` returns:

- `CONTINUE` — no action
- `STOP_LAUNCHING` — critical pressure (or elevated when policy blocks it)
- `PREEMPT` — critical pressure and `cancel_lowest_priority_on_critical`, with a
  deterministic `victim_task_id` (lowest priority, then oldest, then id)

The guard only produces the signal. The orchestrator writes the state transition,
cancels the container, and marks the attempt retryable with
`RESOURCE_EXHAUSTION`. `ResourceGuard.emit()` appends the `RESOURCE_PRESSURE`
audit event.

## Configuration

Base limits and thresholds come from plan-01 settings:

```toml
[limits]
max_global_concurrency = 1
max_project_concurrency = 1
cpu_headroom_pct = 10
mem_headroom_pct = 10
disk_min_free_mb = 1024
pressure_cpu_pct = 90
pressure_mem_pct = 90
```

`SchedulerConfig` adds local defaults for `pressure_elevated_ratio`,
`pressure_hysteresis_samples`, `aging_per_round`, `aging_cap`,
`cancel_lowest_priority_on_critical`, sampler interval, and history size. These
are deliberately **not** read from the project spec, so neither a planner nor an
agent can loosen them.

## Tests

- `tests/unit/test_scheduler_core.py` — every reason code, ordering/ties, aging,
  N > 1 concurrency, cumulative CPU/memory/disk, model budget, backoff, paused
  projects, determinism under shuffled input, property checks for limits
- `tests/unit/test_scheduler_monitor.py` — mocked psutil, threshold transitions,
  2-sample hysteresis, history window, background sampler
- `tests/unit/test_scheduler_guard.py` — continue / stop / preempt decisions
- `tests/integration/test_scheduler_admission.py` — reservations from attempts,
  idempotent reserve/release, peak recording, pressure event
- `tests/integration/test_scheduler_monitor_psutil.py` — real psutil smoke test
- `tests/fakes/scheduler.py` — scriptable `FakeResourceMonitor`
