"""Fixtures for the security hardening suite."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from devsandbox.common.types import AttemptId, NetworkPolicy, ResourceProfile
from devsandbox.config.settings import DevSandboxSettings, SecurityConfig, ValidationSettings
from devsandbox.sandbox.interfaces import SandboxRequest
from devsandbox.sandbox.manager import DockerSandboxManager

DEFAULT_IMAGE = os.environ.get("DEVSANDBOX_VALIDATOR_IMAGE", "devsandbox/validator:latest")


def docker_ready() -> tuple[bool, str]:
    """Return whether the real-Docker security tests can run."""
    if os.environ.get("DEVSANDBOX_E2E") != "1":
        return False, "set DEVSANDBOX_E2E=1 to run real-Docker security tests"
    if shutil.which("docker") is None:
        return False, "docker CLI not found"
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        return False, "Docker daemon is not reachable"
    image = subprocess.run(
        ["docker", "image", "inspect", DEFAULT_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if image.returncode != 0:
        return False, f"{DEFAULT_IMAGE} is not built"
    return True, ""


@pytest.fixture
def security_settings(tmp_path: Path) -> DevSandboxSettings:
    return DevSandboxSettings(
        data_root=str(tmp_path / "data"),
        security=SecurityConfig(
            allow_network_default=False,
            allow_push=False,
            allow_main_merge=False,
            allowed_images=[DEFAULT_IMAGE],
        ),
        validation=ValidationSettings(image=DEFAULT_IMAGE),
    )


@pytest.fixture
async def sandbox(security_settings: DevSandboxSettings, tmp_path: Path) -> Any:
    ready, reason = docker_ready()
    if not ready:
        pytest.skip(reason)
    manager = DockerSandboxManager(
        security_settings,
        tmp_path / "data",
        log_cap_bytes=4096,
    )
    try:
        yield manager
    finally:
        with contextlib.suppress(Exception):
            for container in await manager.list_managed():
                await manager.destroy(container.container_id)
        with contextlib.suppress(Exception):
            manager._client.close()  # type: ignore[attr-defined]


def make_request(
    workspace: Path,
    command: list[str],
    *,
    attempt_id: str = "sec-attempt",
    network: NetworkPolicy | None = None,
    resources: ResourceProfile | None = None,
    secret_env: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
) -> SandboxRequest:
    workspace.mkdir(parents=True, exist_ok=True)
    return SandboxRequest(
        attempt_id=AttemptId(attempt_id),
        image=DEFAULT_IMAGE,
        workspace_path=workspace.resolve(),
        command=command,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        secret_env=secret_env or {},
        resources=resources or ResourceProfile(cpu_cores=0.5, memory_mb=256, pids_limit=64),
        network=network or NetworkPolicy(mode="none"),
        timeout_seconds=timeout_seconds,
        labels={"devsandbox.project": "security", "devsandbox.task": "isolation"},
    )


def read_log(path: Path) -> str:
    """Read a sandbox log file tolerantly."""
    return path.read_text(encoding="utf-8", errors="replace")
