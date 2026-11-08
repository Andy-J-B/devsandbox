"""SandboxManager contract: the fake and the real Docker manager agree.

These tests run in the Docker CI job.  Without a daemon the real case is
skipped, while the fake case still runs as part of the unit/contract suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes import FakeSandboxManager

from devsandbox.common.types import AttemptId, NetworkPolicy, ResourceProfile
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.interfaces import SandboxRequest, SandboxResult
from devsandbox.sandbox.manager import DockerSandboxManager

pytestmark = pytest.mark.contract

IMAGE = os.environ.get("DEVSANDBOX_CONTRACT_IMAGE", "python:3.12-slim")


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _request(tmp_path: Path) -> SandboxRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return SandboxRequest(
        attempt_id=AttemptId("contract-attempt"),
        image=IMAGE,
        workspace_path=workspace,
        command=["sh", "-c", "echo contract"],
        resources=ResourceProfile(cpu_cores=0.5, memory_mb=256, pids_limit=64),
        network=NetworkPolicy(mode="none"),
        timeout_seconds=60,
    )


def _fake_result(tmp_path: Path) -> SandboxResult:
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("contract\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    now = datetime.now(UTC)
    return SandboxResult(
        container_id="fake-container",
        exit_code=0,
        timed_out=False,
        oom_killed=False,
        stdout_path=stdout,
        stderr_path=stderr,
        started_at=now,
        finished_at=now,
        peak_memory_mb=1,
    )


@pytest.mark.parametrize("kind", ["fake", "real"])
async def test_run_echo_returns_success_result(kind: str, tmp_path: Path) -> None:
    request = _request(tmp_path)
    if kind == "fake":
        sandbox = FakeSandboxManager()
        sandbox.queue_result(_fake_result(tmp_path))
    else:
        if not _docker_available():
            pytest.skip("Docker daemon is not reachable")
        sandbox = DockerSandboxManager(DevSandboxSettings(), tmp_path / "data")

    result = await sandbox.run(request)

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.oom_killed is False
    assert "contract" in result.stdout_path.read_text(encoding="utf-8")

    managed = await sandbox.list_managed()
    assert isinstance(managed, list)
