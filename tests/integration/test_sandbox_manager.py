"""Integration tests for ``DockerSandboxManager``.

These require a real Docker daemon and are skipped otherwise.  They exercise
the isolation, resource, timeout, and OOM behaviour of :class:`DockerSandboxManager`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import docker
import pytest

from devsandbox.common.types import AttemptId, ResourceProfile
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.interfaces import NetworkPolicy, SandboxRequest
from devsandbox.sandbox.manager import DockerSandboxManager


def _docker_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        client.close()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker daemon unavailable"),
]

VALIDATION_IMAGE = "python:3.12-slim"


def _make_manager(tmp_path: Path, **kwargs) -> DockerSandboxManager:
    settings = DevSandboxSettings()
    return DockerSandboxManager(settings, tmp_path / "data", **kwargs)


def _make_request(
    tmp_path: Path,
    command: list[str],
    *,
    attempt_id: str = "attempt-1",
    network: NetworkPolicy | None = None,
    resources=None,
    timeout_seconds: int = 60,
) -> SandboxRequest:
    from devsandbox.common.types import ResourceProfile

    return SandboxRequest(
        attempt_id=AttemptId(attempt_id),
        image=VALIDATION_IMAGE,
        workspace_path=tmp_path / "workspace",
        command=command,
        env={},
        secret_env={},
        resources=resources or ResourceProfile(),
        network=network or NetworkPolicy(mode="none"),
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_run_echo_captures_logs(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    manager = _make_manager(tmp_path)
    req = _make_request(tmp_path, ["echo", "hello-sandbox"])
    try:
        result = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result.container_id)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.oom_killed is False
    stdout = result.stdout_path.read_text()
    assert "hello-sandbox" in stdout


@pytest.mark.asyncio
async def test_run_timeout_kills_container(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    manager = _make_manager(tmp_path)
    req = _make_request(tmp_path, ["sleep", "600"], timeout_seconds=3)
    try:
        result = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result.container_id)

    assert result.timed_out is True
    # Container must not be left running.
    managed = await manager.list_managed()
    assert result.container_id not in {c.container_id for c in managed}


@pytest.mark.asyncio
async def test_run_oom_killed(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    manager = _make_manager(tmp_path)
    req = _make_request(
        tmp_path,
        ["python", "-c", "x=bytearray(1_000_000_000)"],
        resources=ResourceProfile(memory_mb=128),
        timeout_seconds=60,
    )
    try:
        result = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result.container_id)

    assert result.oom_killed is True


@pytest.mark.asyncio
async def test_run_network_none_blocks_egress(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    manager = _make_manager(tmp_path)
    req = _make_request(
        tmp_path,
        ["python", "-c", "import socket; socket.create_connection(('example.com', 80), 2)"],
        network=NetworkPolicy(mode="none"),
        timeout_seconds=30,
    )
    try:
        result = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result.container_id)

    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_run_isolation_no_host_access(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    # A file outside the workspace that the container must not be able to read.
    secret_file = tmp_path / "host-secret.txt"
    secret_file.write_text("do-not-read")

    manager = _make_manager(tmp_path)
    isolation_cmd = (
        "id -u && test ! -r /host-secret.txt && test ! -S /var/run/docker.sock && echo ISOLATED"
    )
    req = _make_request(
        tmp_path,
        ["sh", "-c", isolation_cmd],
        timeout_seconds=30,
    )
    try:
        result = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result.container_id)

    assert result.exit_code == 0
    stdout = result.stdout_path.read_text()
    assert "ISOLATED" in stdout
    assert "1000" in stdout  # non-root user


@pytest.mark.asyncio
async def test_run_idempotent_same_attempt_id(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    # keep_on_failure simulates a crash leaving the container behind.
    manager = _make_manager(tmp_path, keep_on_failure=True)
    req = _make_request(tmp_path, ["echo", "first"])
    try:
        result1 = await manager.run(req)
        # Simulate a crash: container left in place.  Run again with the same
        # attempt id; it must be replaced, not duplicated.
        result2 = await manager.run(req)
    finally:
        await asyncio.to_thread(manager.destroy, result2.container_id)

    assert result1.container_id == result2.container_id
    managed = await manager.list_managed()
    attempts = [c for c in managed if c.labels.get("devsandbox.attempt") == "attempt-1"]
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_reap_orphans_removes_stale_container(tmp_path: Path) -> None:
    tmp_path.joinpath("workspace").mkdir(parents=True, exist_ok=True)
    manager = _make_manager(tmp_path, keep_on_failure=True)
    req = _make_request(tmp_path, ["echo", "orphan"])
    try:
        result = await manager.run(req)
    finally:
        pass  # leave container behind to simulate a crash

    known: set = set()
    removed = await manager.reap_orphans(known, max_age_seconds=0.0)
    assert result.container_id in removed


def test_docker_info_returns_daemon_totals(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    info = manager.docker_info()
    assert "NCPU" in info or "MemTotal" in info
