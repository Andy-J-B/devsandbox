"""Drill: disk-full behaviour at the sandbox and orchestrator boundaries.

A real full filesystem is destructive, so the automatic drill injects the same
error the sandbox reports when ``mkdir``/log writes fail with ``ENOSPC``.  The
manual procedure for a throwaway loopback filesystem is in ``docs/drills.md``.
"""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.drills.faults import FlakySandbox

from devsandbox.common.errors import SandboxError
from devsandbox.common.types import ProjectStatus, TaskStatus
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.sandbox.manager import DockerSandboxManager

pytestmark = [pytest.mark.drill, pytest.mark.docker, pytest.mark.slow]

DISK_FULL_MESSAGE = "SandboxError: [Errno 28] No space left on device (drill)"


def test_sandbox_translates_enospc_into_a_clear_error(tmp_path: Path) -> None:
    """Operators and the failure classifier see a storage error, not a traceback."""
    error = OSError(errno.ENOSPC, "No space left on device")
    with (
        patch.object(Path, "mkdir", side_effect=error),
        pytest.raises(SandboxError, match="disk full or quota exceeded"),
    ):
        DockerSandboxManager._prepare_directory(tmp_path / "logs", "log")


async def test_engine_retries_after_a_disk_full_failure(drill_env: object) -> None:
    """A transient disk-full failure retries and the task can still complete."""
    env = drill_env
    real_sandbox = env.sandbox()  # type: ignore[attr-defined]
    flaky = FlakySandbox(real_sandbox, failures=1, message=DISK_FULL_MESSAGE)
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

    assert flaky.failures_served == 1
    assert project.status == ProjectStatus.COMPLETED
    assert all(task.status == TaskStatus.COMPLETED for task in tasks)
