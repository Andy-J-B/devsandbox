"""Container isolation checks that execute inside a real Docker container."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from tests.security.conftest import make_request, read_log

from devsandbox.sandbox.manager import container_identity

pytestmark = pytest.mark.security


async def _run(
    sandbox: Any, workspace: Path, command: list[str], **kwargs: Any
) -> tuple[int, str, str]:
    result = await sandbox.run(make_request(workspace, command, **kwargs))
    return (
        result.exit_code or 0,
        read_log(result.stdout_path),
        read_log(result.stderr_path),
    )


async def test_container_is_hardened(sandbox: Any, tmp_path: Path) -> None:
    """Inspect a live container for caps, rootfs, user, pids, and mounts."""
    from tests.security.conftest import make_request

    workspace = tmp_path / "workspace"
    request = make_request(
        workspace,
        ["python3", "-c", "import time; time.sleep(5)"],
        attempt_id="sec-inspect",
        resources=None,
    )
    run_task = asyncio.create_task(sandbox.run(request))
    container_id = None
    for _ in range(100):
        managed = await sandbox.list_managed()
        if managed:
            container_id = managed[0].container_id
            break
        await asyncio.sleep(0.1)
    assert container_id, "container never started"
    attrs = sandbox._client.containers.get(container_id).attrs
    await run_task

    host = attrs["HostConfig"]
    assert host.get("Privileged") in (None, False)
    assert "ALL" in (host.get("CapDrop") or [])
    assert any("no-new-privileges" in option for option in (host.get("SecurityOpt") or []))
    assert host.get("ReadonlyRootfs") is True
    assert host.get("PidsLimit")
    uid, gid = container_identity()
    assert attrs["Config"].get("User") == f"{uid}:{gid}"
    assert host.get("NetworkMode") == "none"
    mounts = host.get("Binds") or host.get("Mounts") or []
    assert "docker.sock" not in str(mounts)
    assert "/var/run" not in str(mounts)


async def test_cannot_read_host_files(sandbox: Any, tmp_path: Path) -> None:
    host_secret = tmp_path / "host-only-secret.txt"
    host_secret.write_text("HOST-CANARY", encoding="utf-8")
    code = f"print(open({str(host_secret)!r}).read())"
    exit_code, stdout, stderr = await _run(
        sandbox, tmp_path / "workspace", ["python3", "-c", code], attempt_id="sec-host-read"
    )
    assert exit_code != 0
    assert "HOST-CANARY" not in stdout + stderr


async def test_cannot_reach_docker_socket(sandbox: Any, tmp_path: Path) -> None:
    exit_code, stdout, stderr = await _run(
        sandbox,
        tmp_path / "workspace",
        [
            "python3",
            "-c",
            "import os, sys; sys.exit(0 if os.path.exists('/var/run/docker.sock') else 1)",
        ],
        attempt_id="sec-docker-sock",
    )
    assert exit_code != 0
    assert "docker.sock" not in stdout + stderr


async def test_network_none_blocks_metadata_and_loopback(sandbox: Any, tmp_path: Path) -> None:
    probe = (
        "import socket, sys\n"
        "for host in ('169.254.169.254', 'host.docker.internal'):\n"
        "    try:\n"
        "        socket.create_connection((host, 80), timeout=2)\n"
        "    except OSError:\n"
        "        continue\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    exit_code, stdout, stderr = await _run(
        sandbox, tmp_path / "workspace", ["python3", "-c", probe], attempt_id="sec-network"
    )
    assert exit_code != 0, stdout + stderr


async def test_cannot_write_outside_workspace(sandbox: Any, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    exit_code, _stdout, _stderr = await _run(
        sandbox,
        workspace,
        ["python3", "-c", "open('/etc/devsandbox-escape', 'w').write('x')"],
        attempt_id="sec-write-outside",
    )
    assert exit_code != 0

    exit_code, _stdout, _stderr = await _run(
        sandbox,
        workspace,
        ["python3", "-c", "open('/workspace/inside.txt', 'w').write('ok')"],
        attempt_id="sec-write-inside",
    )
    assert exit_code == 0
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "ok"
