"""Optional real-Docker validation tests (plan 09).

These build the validator base image and exercise the check runner against a
real daemon.  They are opt-in because building the image is slow::

    DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1 \
        pytest -m docker tests/integration/test_validation_docker.py

Without the environment variable (or without Docker) every test is skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devsandbox.common.types import CheckSpec, ResourceProfile
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.manager import DockerSandboxManager
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.runner import CheckRunner
from devsandbox.validation.workspace import make_writable

_ENABLED = os.environ.get("DEVSANDBOX_RUN_VALIDATION_INTEGRATION") == "1"
_IMAGE = os.environ.get("DEVSANDBOX_VALIDATOR_IMAGE", "devsandbox/validator:test")
_ROOT = Path(__file__).resolve().parents[2]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _skip_reason() -> str | None:
    if not _ENABLED:
        return "set DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1 to run Docker validation tests"
    if not _docker_available():
        return "Docker daemon unavailable"
    return None


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "Docker unavailable"),
]


@pytest.fixture(scope="module")
def validator_image() -> str:
    """Build the validator image once per module."""
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(_ROOT / "docker" / "validator-base" / "Dockerfile"),
            "-t",
            _IMAGE,
            str(_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"validator image build failed:\n{result.stdout}\n{result.stderr}")
    return _IMAGE


@pytest.fixture
def runner(tmp_path: Path, validator_image: str) -> CheckRunner:
    manager = DockerSandboxManager(DevSandboxSettings(), tmp_path / "data")
    config = ValidationConfig(
        image=validator_image,
        resources=ResourceProfile(cpu_cores=1.0, memory_mb=512, disk_mb=1024, pids_limit=256),
        max_output_bytes=8192,
    )
    return CheckRunner(manager, config)


def _workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, content in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    make_writable(workspace)
    return workspace


@pytest.mark.asyncio
async def test_passing_python_tests(runner: CheckRunner, tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        {"tests/test_sample.py": "def test_math():\n    assert 1 + 1 == 2\n"},
    )

    result = await runner.run_check(
        CheckSpec(
            name="pytest",
            run=["python", "-m", "pytest", "-q"],
            kind="test",
            timeout_seconds=120,
        ),
        workspace,
        attempt_id="docker-pass",
    )

    assert result.status == "passed", result.output
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_breaking_test_fails(runner: CheckRunner, tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        {"tests/test_sample.py": "def test_math():\n    assert 1 + 1 == 3\n"},
    )

    result = await runner.run_check(
        CheckSpec(
            name="pytest",
            run=["python", "-m", "pytest", "-q"],
            kind="test",
            timeout_seconds=120,
        ),
        workspace,
        attempt_id="docker-fail",
    )

    assert result.status == "failed"
    assert result.exit_code not in (None, 0)
    assert result.failure_kind is not None


@pytest.mark.asyncio
async def test_network_is_blocked(runner: CheckRunner, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"README.md": "network test\n"})

    result = await runner.run_check(
        CheckSpec(
            name="network",
            run=[
                "python",
                "-c",
                ("import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"),
            ],
            kind="test",
            timeout_seconds=30,
        ),
        workspace,
        attempt_id="docker-network",
    )

    assert result.status == "failed", result.output


@pytest.mark.asyncio
async def test_write_outside_workspace_fails(runner: CheckRunner, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"README.md": "write test\n"})

    outside = await runner.run_check(
        CheckSpec(
            name="escape",
            run=[
                "python",
                "-c",
                "open('/etc/devsandbox-escape', 'w').write('escaped')",
            ],
            kind="test",
            timeout_seconds=30,
        ),
        workspace,
        attempt_id="docker-escape",
    )
    inside = await runner.run_check(
        CheckSpec(
            name="write",
            run=["python", "-c", "open('inside.txt', 'w').write('ok')"],
            kind="test",
            timeout_seconds=30,
        ),
        workspace,
        attempt_id="docker-write",
    )

    assert outside.status == "failed", outside.output
    assert not Path("/etc/devsandbox-escape").exists()
    assert inside.status == "passed", inside.output
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_hanging_check_times_out(runner: CheckRunner, tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"README.md": "timeout test\n"})

    result = await runner.run_check(
        CheckSpec(
            name="hang",
            run=["python", "-c", "import time; time.sleep(600)"],
            timeout_seconds=3,
        ),
        workspace,
        attempt_id="docker-timeout",
    )

    assert result.status == "timeout"
    assert result.duration_seconds < 60
