"""Allowlist network mode: positive and negative egress tests (real Docker).

These tests start a local origin server on the default bridge, then run the
agent on a per-attempt *internal* network with an allowlist egress proxy.  They
prove both directions of the policy:

* an allowlisted host is reachable through the proxy;
* an unlisted host is denied by the proxy;
* direct egress from the agent network is not possible.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.security.conftest import DEFAULT_IMAGE, docker_ready, make_request, read_log

from devsandbox.common.types import NetworkPolicy

pytestmark = pytest.mark.security

PROXY_IMAGE = "devsandbox/egress-proxy:latest"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_proxy_image() -> None:
    ready, reason = docker_ready()
    if not ready:
        pytest.skip(reason)
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    inspected = subprocess.run(
        ["docker", "image", "inspect", PROXY_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspected.returncode == 0:
        return
    built = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "docker/egress-proxy/Dockerfile",
            "-t",
            PROXY_IMAGE,
            ".",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        pytest.skip(f"could not build {PROXY_IMAGE}: {built.stderr[-500:]}")


@pytest.fixture(scope="module", autouse=True)
def proxy_image() -> None:
    _ensure_proxy_image()


@pytest.fixture
def origin(sandbox: Any) -> Iterator[tuple[Any, str]]:
    """Start a plain-HTTP origin on the default bridge and return its IP."""
    name = f"ds-allowlist-origin-{int(time.time() * 1000)}"
    container = sandbox._client.containers.run(
        DEFAULT_IMAGE,
        command=["python3", "-m", "http.server", "8080", "--bind", "0.0.0.0"],
        name=name,
        detach=True,
        network_mode="bridge",
    )
    try:
        container.reload()
        ip = container.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        assert ip, "origin container has no bridge IP"
        for _ in range(100):
            probe = container.exec_run(
                [
                    "python3",
                    "-c",
                    "import socket; "
                    "socket.create_connection(('127.0.0.1', 8080), timeout=1).close()",
                ]
            )
            if probe.exit_code == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("origin HTTP server did not start")
        yield container, ip
    finally:
        with contextlib.suppress(Exception):
            container.remove(force=True)


def _enable_allowlist(sandbox: Any, hosts: list[str]) -> None:
    """Enable allowlist policy on the test manager (settings, not the spec)."""
    sandbox._settings.security.allow_network_allowlist = True
    sandbox._settings.security.allowlist_hosts = list(hosts)


async def test_allowlisted_host_is_reachable_through_proxy(
    sandbox: Any, origin: tuple[Any, str], tmp_path: Path
) -> None:
    _origin, ip = origin
    entry = f"{ip}:8080"
    _enable_allowlist(sandbox, [entry])
    workspace = tmp_path / "workspace"
    code = (
        "import urllib.request\n"
        f"data = urllib.request.urlopen('http://{ip}:8080/', timeout=10).read()\n"
        "print('origin-bytes', len(data))\n"
    )
    request = make_request(
        workspace,
        ["python3", "-c", code],
        attempt_id="sec-allowlist-positive",
        network=NetworkPolicy(mode="allowlist", allowed_hosts=[entry]),
        timeout_seconds=60,
    )

    result = await sandbox.run(request)

    stdout = read_log(result.stdout_path)
    stderr = read_log(result.stderr_path)
    assert result.exit_code == 0, stdout + stderr
    assert "origin-bytes" in stdout
    assert "403" not in stdout + stderr


async def test_unlisted_host_is_denied_by_proxy(
    sandbox: Any, origin: tuple[Any, str], tmp_path: Path
) -> None:
    _origin, ip = origin
    _enable_allowlist(sandbox, ["allowed.example"])
    workspace = tmp_path / "workspace"
    code = (
        "import urllib.error, urllib.request\n"
        "try:\n"
        f"    urllib.request.urlopen('http://{ip}:8080/', timeout=10)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print('denied', exc.code)\n"
        "    raise SystemExit(0 if exc.code == 403 else 1)\n"
        "raise SystemExit(1)\n"
    )
    request = make_request(
        workspace,
        ["python3", "-c", code],
        attempt_id="sec-allowlist-negative",
        network=NetworkPolicy(mode="allowlist", allowed_hosts=["allowed.example"]),
        timeout_seconds=60,
    )

    result = await sandbox.run(request)

    stdout = read_log(result.stdout_path)
    stderr = read_log(result.stderr_path)
    assert result.exit_code == 0, stdout + stderr
    assert "denied 403" in stdout


async def test_direct_egress_is_blocked_on_internal_network(
    sandbox: Any, origin: tuple[Any, str], tmp_path: Path
) -> None:
    _origin, ip = origin
    _enable_allowlist(sandbox, [f"{ip}:8080"])
    workspace = tmp_path / "workspace"
    code = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('{ip}', 8080), timeout=3).close()\n"
        "except OSError as exc:\n"
        "    print('direct-blocked', type(exc).__name__)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    request = make_request(
        workspace,
        ["python3", "-c", code],
        attempt_id="sec-allowlist-direct",
        network=NetworkPolicy(mode="allowlist", allowed_hosts=[f"{ip}:8080"]),
        timeout_seconds=60,
    )

    result = await sandbox.run(request)

    stdout = read_log(result.stdout_path)
    stderr = read_log(result.stderr_path)
    assert result.exit_code == 0, stdout + stderr
    assert "direct-blocked" in stdout


async def test_proxy_is_locked_down_and_agent_has_no_other_route(
    sandbox: Any, origin: tuple[Any, str], tmp_path: Path
) -> None:
    _origin, ip = origin
    entry = f"{ip}:8080"
    _enable_allowlist(sandbox, [entry])
    workspace = tmp_path / "workspace"
    attempt_id = "sec-allowlist-inspect"
    request = make_request(
        workspace,
        ["python3", "-c", "import time; time.sleep(10)"],
        attempt_id=attempt_id,
        network=NetworkPolicy(mode="allowlist", allowed_hosts=[entry]),
        timeout_seconds=30,
    )

    run_task = asyncio.create_task(sandbox.run(request))
    try:
        proxy = None
        agent = None
        for _ in range(100):
            with contextlib.suppress(Exception):
                proxy = sandbox._client.containers.get(f"ds-{attempt_id}-proxy")
                agent = sandbox._client.containers.get(f"ds-{attempt_id}")
            if proxy is not None and agent is not None:
                break
            await asyncio.sleep(0.1)
        assert proxy is not None, "egress proxy never started"
        assert agent is not None, "agent container never started"

        proxy_host = proxy.attrs["HostConfig"]
        assert proxy_host.get("ReadonlyRootfs") is True
        assert "ALL" in (proxy_host.get("CapDrop") or [])
        assert any(
            "no-new-privileges" in option for option in (proxy_host.get("SecurityOpt") or [])
        )
        user = proxy.attrs["Config"].get("User") or ""
        assert not user.startswith("0:")

        networks = proxy.attrs["NetworkSettings"]["Networks"]
        assert f"ds-net-{attempt_id}" in networks
        assert "bridge" in networks
        assert agent.attrs["HostConfig"]["NetworkMode"] == f"ds-net-{attempt_id}"
    finally:
        if not run_task.done():
            await sandbox.kill(f"ds-{attempt_id}")
        await run_task
