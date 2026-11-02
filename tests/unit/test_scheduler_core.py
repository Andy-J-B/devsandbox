"""Table-driven and property tests for the pure scheduler."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from devsandbox.common.types import ResourceProfile
from devsandbox.scheduler.models import (
    DecisionAction,
    DeferReason,
    HostResources,
    ModelBudgetState,
    PressureState,
    ReadyTask,
    RunningTask,
    SchedulingLimits,
    SchedulingSnapshot,
)
from devsandbox.scheduler.scheduler import (
    DeterministicScheduler,
    effective_priority,
    ordered_tasks,
    select_decisions,
    update_deferral_counts,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_host(**overrides: object) -> HostResources:
    values: dict[str, object] = {
        "cpu_percent": 10.0,
        "cpu_count": 8,
        "memory_total_mb": 16384.0,
        "memory_available_mb": 14000.0,
        "disk_free_mb": 200_000,
        "sampled_at": NOW,
    }
    values.update(overrides)
    return HostResources(**values)  # type: ignore[arg-type]


def make_task(task_id: str, **overrides: object) -> ReadyTask:
    values: dict[str, object] = {
        "project_id": "p1",
        "priority": 0,
        "created_order": 0,
        "resources": ResourceProfile(cpu_cores=1.0, memory_mb=1024, disk_mb=1024),
    }
    values.update(overrides)
    return ReadyTask(task_id=task_id, **values)  # type: ignore[arg-type]


def make_limits(**overrides: object) -> SchedulingLimits:
    values: dict[str, object] = {
        "max_global_concurrency": 2,
        "max_project_concurrency": 2,
        "cpu_headroom_pct": 0.0,
        "mem_headroom_pct": 0.0,
        "disk_min_free_mb": 1024,
    }
    values.update(overrides)
    return SchedulingLimits(**values)  # type: ignore[arg-type]


def make_snapshot(**overrides: object) -> SchedulingSnapshot:
    values: dict[str, object] = {
        "now": NOW,
        "host": make_host(),
        "pressure": PressureState.OK,
        "limits": make_limits(),
    }
    values.update(overrides)
    return SchedulingSnapshot(**values)  # type: ignore[arg-type]


def only_decision(snapshot: SchedulingSnapshot):
    decisions = select_decisions(snapshot)
    assert len(decisions) == 1
    return decisions[0]


# ---------------------------------------------------------------- reasons table
@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            make_snapshot(
                limits=make_limits(max_global_concurrency=1),
                running=[RunningTask(task_id="run", project_id="p1")],
                ready_tasks=[make_task("t1")],
            ),
            DeferReason.CONCURRENCY_GLOBAL,
        ),
        (
            make_snapshot(
                limits=make_limits(max_global_concurrency=5, max_project_concurrency=1),
                running=[RunningTask(task_id="run", project_id="p1")],
                ready_tasks=[make_task("t1")],
            ),
            DeferReason.CONCURRENCY_PROJECT,
        ),
        (
            make_snapshot(
                host=make_host(cpu_count=2, cpu_percent=0.0),
                ready_tasks=[
                    make_task("t1", resources=ResourceProfile(cpu_cores=3.0, memory_mb=64))
                ],
            ),
            DeferReason.INSUFFICIENT_CPU,
        ),
        (
            make_snapshot(
                host=make_host(memory_available_mb=1024.0),
                ready_tasks=[
                    make_task("t1", resources=ResourceProfile(cpu_cores=1.0, memory_mb=2048))
                ],
            ),
            DeferReason.INSUFFICIENT_MEMORY,
        ),
        (
            make_snapshot(
                host=make_host(disk_free_mb=1024),
                limits=make_limits(disk_min_free_mb=1024),
                ready_tasks=[make_task("t1", resources=ResourceProfile(cpu_cores=1.0, disk_mb=1))],
            ),
            DeferReason.INSUFFICIENT_DISK,
        ),
        (
            make_snapshot(
                pressure=PressureState.CRITICAL,
                ready_tasks=[make_task("t1")],
            ),
            DeferReason.PRESSURE,
        ),
        (
            make_snapshot(
                model_budget={"opencode": ModelBudgetState(remaining_tokens=5)},
                ready_tasks=[
                    make_task(
                        "t1",
                        model_provider="opencode",
                        estimated_tokens=10,
                    )
                ],
            ),
            DeferReason.MODEL_BUDGET,
        ),
        (
            make_snapshot(
                ready_tasks=[make_task("t1", retry_not_before=NOW + timedelta(seconds=30))],
            ),
            DeferReason.BACKOFF,
        ),
        (
            make_snapshot(
                paused_projects=["p1"],
                ready_tasks=[make_task("t1")],
            ),
            DeferReason.PROJECT_PAUSED,
        ),
    ],
)
def test_every_defer_reason_is_reachable(snapshot: SchedulingSnapshot, reason: DeferReason) -> None:
    decision = only_decision(snapshot)
    assert decision.action is DecisionAction.DEFER
    assert decision.reason_code is reason
    assert decision.reason


def test_all_reason_codes_have_a_test() -> None:
    tested = {
        DeferReason.CONCURRENCY_GLOBAL,
        DeferReason.CONCURRENCY_PROJECT,
        DeferReason.INSUFFICIENT_CPU,
        DeferReason.INSUFFICIENT_MEMORY,
        DeferReason.INSUFFICIENT_DISK,
        DeferReason.PRESSURE,
        DeferReason.MODEL_BUDGET,
        DeferReason.BACKOFF,
        DeferReason.PROJECT_PAUSED,
    }
    assert tested == set(DeferReason)


# -------------------------------------------------------------------- pressure
def test_critical_pressure_defers_everything_even_if_running_allowed() -> None:
    snapshot = make_snapshot(
        pressure=PressureState.CRITICAL,
        limits=make_limits(max_global_concurrency=10),
        ready_tasks=[make_task("t1"), make_task("t2")],
    )
    decisions = select_decisions(snapshot)
    assert [decision.reason_code for decision in decisions] == [
        DeferReason.PRESSURE,
        DeferReason.PRESSURE,
    ]


def test_elevated_pressure_with_running_task_defers_all() -> None:
    snapshot = make_snapshot(
        pressure=PressureState.ELEVATED,
        running=[RunningTask(task_id="run", project_id="p1")],
        ready_tasks=[make_task("t1")],
    )
    assert only_decision(snapshot).reason_code is DeferReason.PRESSURE


def test_elevated_pressure_with_nothing_running_allows_exactly_one() -> None:
    snapshot = make_snapshot(
        pressure=PressureState.ELEVATED,
        limits=make_limits(max_global_concurrency=5),
        ready_tasks=[make_task("t1"), make_task("t2")],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].action is DecisionAction.LAUNCH
    assert decisions[1].reason_code is DeferReason.PRESSURE


def test_pressure_blocks_elevated_can_be_enabled() -> None:
    snapshot = make_snapshot(
        pressure=PressureState.ELEVATED,
        limits=make_limits(pressure_blocks_elevated=True),
        ready_tasks=[make_task("t1")],
    )
    assert only_decision(snapshot).reason_code is DeferReason.PRESSURE


# ------------------------------------------------------------------- ordering
def test_ordering_priority_then_critical_path_then_created_order_then_id() -> None:
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=10, max_project_concurrency=10),
        ready_tasks=[
            make_task("t-low", priority=1, created_order=0),
            make_task("t-high", priority=9, created_order=9),
            make_task("t-cp-short", priority=5, critical_path_length=1, created_order=2),
            make_task("t-cp-long", priority=5, critical_path_length=4, created_order=2),
            make_task("t-tie-b", priority=3, created_order=5),
            make_task("t-tie-a", priority=3, created_order=5),
        ],
    )
    decisions = select_decisions(snapshot)
    assert all(decision.action is DecisionAction.LAUNCH for decision in decisions)
    assert [decision.task_id for decision in decisions] == [
        "t-high",
        "t-cp-long",
        "t-cp-short",
        "t-tie-a",
        "t-tie-b",
        "t-low",
    ]


def test_aging_promotes_long_deferred_task_and_respects_cap() -> None:
    limits = make_limits(aging_per_round=2, aging_cap=4)
    old = make_task("t-old", priority=0, consecutive_deferrals=3)
    young = make_task("t-young", priority=4, consecutive_deferrals=0)
    assert effective_priority(old, limits) == 4
    assert effective_priority(young, limits) == 4
    capped = make_task("t-capped", priority=0, consecutive_deferrals=99)
    assert effective_priority(capped, limits) == 4

    snapshot = make_snapshot(
        limits=limits,
        ready_tasks=[
            make_task("t-high", priority=5, created_order=0),
            make_task("t-aged", priority=0, consecutive_deferrals=3, created_order=1),
        ],
    )
    assert ordered_tasks(snapshot)[0].task_id == "t-high"

    stronger_aging = make_snapshot(
        limits=make_limits(aging_per_round=1, aging_cap=10),
        ready_tasks=[
            make_task("t-high", priority=5, created_order=0),
            make_task("t-aged", priority=1, consecutive_deferrals=5, created_order=1),
        ],
    )
    assert ordered_tasks(stronger_aging)[0].task_id == "t-aged"


# ---------------------------------------------------------------- concurrency
def test_global_and_project_limits_with_n_greater_than_one() -> None:
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=3, max_project_concurrency=2),
        ready_tasks=[
            make_task("a1", project_id="a", created_order=1),
            make_task("a2", project_id="a", created_order=2),
            make_task("a3", project_id="a", created_order=3),
            make_task("b1", project_id="b", created_order=4),
            make_task("c1", project_id="c", created_order=5),
        ],
    )
    decisions = select_decisions(snapshot)
    actions = {decision.task_id: decision for decision in decisions}
    assert actions["a1"].action is DecisionAction.LAUNCH
    assert actions["a2"].action is DecisionAction.LAUNCH
    assert actions["a3"].reason_code is DeferReason.CONCURRENCY_PROJECT
    assert actions["b1"].action is DecisionAction.LAUNCH
    assert actions["c1"].reason_code is DeferReason.CONCURRENCY_GLOBAL


def test_project_override_uses_min_of_setting_and_spec() -> None:
    snapshot = make_snapshot(
        limits=make_limits(
            max_global_concurrency=5,
            max_project_concurrency=3,
            project_max_concurrency={"p1": 1},
        ),
        running=[RunningTask(task_id="run", project_id="p1")],
        ready_tasks=[make_task("t1")],
    )
    assert only_decision(snapshot).reason_code is DeferReason.CONCURRENCY_PROJECT


# ------------------------------------------------------------------ resources
def test_cumulative_cpu_budget_prevents_over_admission() -> None:
    snapshot = make_snapshot(
        host=make_host(cpu_count=2, cpu_percent=0.0),
        limits=make_limits(max_global_concurrency=5),
        ready_tasks=[
            make_task("t1", resources=ResourceProfile(cpu_cores=1.5, memory_mb=64)),
            make_task("t2", resources=ResourceProfile(cpu_cores=1.5, memory_mb=64)),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].action is DecisionAction.LAUNCH
    assert decisions[1].reason_code is DeferReason.INSUFFICIENT_CPU


def test_cumulative_memory_budget_prevents_over_admission() -> None:
    snapshot = make_snapshot(
        host=make_host(memory_total_mb=4096.0, memory_available_mb=4096.0),
        limits=make_limits(max_global_concurrency=5),
        ready_tasks=[
            make_task("t1", resources=ResourceProfile(cpu_cores=0.1, memory_mb=3000)),
            make_task("t2", resources=ResourceProfile(cpu_cores=0.1, memory_mb=3000)),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].action is DecisionAction.LAUNCH
    assert decisions[1].reason_code is DeferReason.INSUFFICIENT_MEMORY


def test_committed_resources_are_respected_even_if_host_omits_them() -> None:
    snapshot = make_snapshot(
        host=make_host(cpu_count=2, cpu_percent=0.0, committed_cpu_cores=0.0),
        limits=make_limits(max_global_concurrency=5),
        running=[
            RunningTask(
                task_id="run",
                project_id="p2",
                resources=ResourceProfile(cpu_cores=1.5, memory_mb=64),
            )
        ],
        ready_tasks=[make_task("t1", resources=ResourceProfile(cpu_cores=1.0, memory_mb=64))],
    )
    assert only_decision(snapshot).reason_code is DeferReason.INSUFFICIENT_CPU


def test_headroom_is_applied() -> None:
    snapshot = make_snapshot(
        host=make_host(cpu_count=2, cpu_percent=0.0),
        limits=make_limits(cpu_headroom_pct=25.0, max_global_concurrency=5),
        ready_tasks=[make_task("t1", resources=ResourceProfile(cpu_cores=1.6, memory_mb=64))],
    )
    # 2 cores * 0.75 = 1.5 usable, 1.6 requested.
    assert only_decision(snapshot).reason_code is DeferReason.INSUFFICIENT_CPU


def test_disk_uses_tighter_of_data_and_docker_roots() -> None:
    snapshot = make_snapshot(
        host=make_host(disk_free_mb=200_000, docker_disk_free_mb=1100),
        limits=make_limits(disk_min_free_mb=1024),
        ready_tasks=[make_task("t1", resources=ResourceProfile(cpu_cores=1.0, disk_mb=100))],
    )
    assert only_decision(snapshot).reason_code is DeferReason.INSUFFICIENT_DISK


# --------------------------------------------------------------- model budget
def test_model_budget_is_consumed_by_admitted_tasks() -> None:
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=5),
        model_budget={"opencode": ModelBudgetState(remaining_tokens=100)},
        ready_tasks=[
            make_task("t1", model_provider="opencode", estimated_tokens=60),
            make_task("t2", model_provider="opencode", estimated_tokens=60),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].action is DecisionAction.LAUNCH
    assert decisions[1].reason_code is DeferReason.MODEL_BUDGET


def test_model_budget_requests_are_enforced() -> None:
    snapshot = make_snapshot(
        model_budget={"opencode": ModelBudgetState(remaining_requests=1)},
        ready_tasks=[
            make_task("t1", model_provider="opencode", estimated_requests=1),
            make_task("t2", model_provider="opencode", estimated_requests=1),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].action is DecisionAction.LAUNCH
    assert decisions[1].reason_code is DeferReason.MODEL_BUDGET


def test_unknown_provider_or_unlimited_budget_does_not_block() -> None:
    snapshot = make_snapshot(
        model_budget={"other": ModelBudgetState(remaining_tokens=0)},
        ready_tasks=[
            make_task("t1", model_provider="opencode", estimated_tokens=10),
            make_task("t2", model_provider=None, estimated_tokens=10),
        ],
    )
    assert all(decision.action is DecisionAction.LAUNCH for decision in select_decisions(snapshot))


# --------------------------------------------------------------- determinism
def test_same_snapshot_shuffled_input_is_deterministic() -> None:
    tasks = [
        make_task(
            f"task-{index:02d}",
            project_id=f"p{index % 3}",
            priority=index % 5,
            created_order=index // 2,
            critical_path_length=index % 4,
        )
        for index in range(15)
    ]
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=4, max_project_concurrency=2),
        ready_tasks=tasks,
    )
    expected = select_decisions(snapshot)
    rng = random.Random(42)
    for _ in range(20):
        shuffled = list(tasks)
        rng.shuffle(shuffled)
        candidate = snapshot.model_copy(update={"ready_tasks": shuffled})
        assert select_decisions(candidate) == expected


def test_property_never_exceeds_limits_or_launches_under_critical_pressure() -> None:
    rng = random.Random(7)
    for _ in range(200):
        pressure = rng.choice(list(PressureState))
        limits = make_limits(
            max_global_concurrency=rng.randint(1, 5),
            max_project_concurrency=rng.randint(1, 3),
        )
        running = [
            RunningTask(
                task_id=f"run-{index}",
                project_id=f"p{rng.randint(0, 2)}",
                resources=ResourceProfile(
                    cpu_cores=rng.uniform(0.1, 1.0),
                    memory_mb=rng.randint(64, 512),
                ),
            )
            for index in range(rng.randint(0, 3))
        ]
        ready = [
            make_task(
                f"t{index}",
                project_id=f"p{rng.randint(0, 2)}",
                priority=rng.randint(0, 10),
                created_order=index,
                resources=ResourceProfile(
                    cpu_cores=rng.uniform(0.1, 1.5),
                    memory_mb=rng.randint(64, 2048),
                    disk_mb=rng.randint(1, 4096),
                ),
            )
            for index in range(rng.randint(0, 8))
        ]
        snapshot = make_snapshot(
            pressure=pressure,
            limits=limits,
            running=running,
            ready_tasks=ready,
        )
        decisions = select_decisions(snapshot)
        assert len(decisions) == len(ready)
        launched = [decision for decision in decisions if decision.action is DecisionAction.LAUNCH]
        if pressure is PressureState.CRITICAL:
            assert launched == []
        remaining_slots = max(0, limits.max_global_concurrency - len(running))
        assert len(launched) <= remaining_slots
        per_project: dict[str, int] = {}
        for task in running:
            per_project[task.project_id] = per_project.get(task.project_id, 0) + 1
        by_id = {task.task_id: task for task in ready}
        for decision in launched:
            project = by_id[decision.task_id].project_id
            per_project[project] = per_project.get(project, 0) + 1
            assert per_project[project] <= limits.max_project_concurrency
        for decision in decisions:
            if decision.action is DecisionAction.DEFER:
                assert decision.reason_code is not None


def test_deferral_counts_reset_on_launch_and_increment_on_defer() -> None:
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=1),
        ready_tasks=[
            make_task("t1", created_order=0),
            make_task("t2", created_order=1),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].task_id == "t1"
    assert update_deferral_counts(snapshot, decisions) == {"t1": 0, "t2": 1}


def test_deferral_counts_accumulate_and_aging_is_deterministic() -> None:
    snapshot = make_snapshot(
        limits=make_limits(max_global_concurrency=1, aging_per_round=1, aging_cap=10),
        ready_tasks=[
            make_task("t1", created_order=0),
            make_task("t2", created_order=1, consecutive_deferrals=3),
        ],
    )
    decisions = select_decisions(snapshot)
    assert decisions[0].task_id == "t2"
    assert update_deferral_counts(snapshot, decisions) == {"t2": 0, "t1": 1}


def test_backoff_and_pause_defers_do_not_build_aging_credit() -> None:
    snapshot = make_snapshot(
        paused_projects=["p-paused"],
        ready_tasks=[
            make_task(
                "t-backoff",
                project_id="p2",
                consecutive_deferrals=2,
                retry_not_before=NOW + timedelta(seconds=30),
            ),
            make_task("t-paused", project_id="p-paused", consecutive_deferrals=4),
        ],
    )
    decisions = select_decisions(snapshot)
    assert update_deferral_counts(snapshot, decisions) == {
        "t-backoff": 2,
        "t-paused": 4,
    }


def test_async_scheduler_adapter_is_pure() -> None:
    import asyncio

    snapshot = make_snapshot(ready_tasks=[make_task("t1")])
    scheduler = DeterministicScheduler()
    first = asyncio.run(scheduler.select(snapshot))
    second = asyncio.run(scheduler.select(snapshot))
    assert first == second == [match for match in first if match.action is DecisionAction.LAUNCH]
