"""Drill: the engine survives a temporary Docker daemon outage.

The real daemon is not restarted here (that would disrupt the host and any
other containers).  Instead the sandbox boundary fails exactly like an
unavailable daemon for the first attempt, then recovers.  ``docs/drills.md``
documents the manual real-daemon procedure.
"""

from __future__ import annotations

import pytest
from tests.drills.faults import FlakySandbox

from devsandbox.common.types import ProjectStatus, TaskStatus
from devsandbox.orchestrator.models import OrchestratorConfig

pytestmark = [pytest.mark.drill, pytest.mark.docker, pytest.mark.slow]

OUTAGE_MESSAGE = "Cannot connect to the Docker daemon (drill: simulated outage)"


async def test_engine_recovers_after_a_docker_outage(drill_env: object) -> None:
    """One unavailable-daemon failure becomes an infra retry, not a stall."""
    env = drill_env
    real_sandbox = env.sandbox()  # type: ignore[attr-defined]
    flaky = FlakySandbox(real_sandbox, failures=1, message=OUTAGE_MESSAGE)
    agent = env.agent(flaky)  # type: ignore[attr-defined]
    context = env.context(  # type: ignore[attr-defined]
        agent,
        sandbox=real_sandbox,
        orchestrator_config=OrchestratorConfig(
            tick_seconds=0.05,
            idle_sleep_seconds=0.01,
            shutdown_timeout_seconds=10.0,
        ),
    )

    project_id, _ = await context.submit_and_run(env.spec(), timeout=300)  # type: ignore[attr-defined]

    project = await context.project(project_id)
    tasks = await context.tasks(project_id)
    events = await context.events(project_id)

    assert flaky.failures_served == 1
    assert project.status == ProjectStatus.COMPLETED
    assert tasks
    assert all(task.status == TaskStatus.COMPLETED for task in tasks)
    infra_retries = [
        event
        for event in events
        if event.type == "task.retry_scheduled"
        and (event.payload_json or {}).get("kind") == "infra_error"
    ]
    assert infra_retries, "the outage should be recorded as an infra retry"
