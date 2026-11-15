"""Secret handling: canaries must not reach logs, labels, commands, or reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.security.conftest import make_request, read_log

pytestmark = pytest.mark.security

CANARY = "sk-canary-9f3a2b1c4d5e6f708192"


async def test_secret_is_redacted_from_logs_and_metadata(sandbox: Any, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    request = make_request(
        workspace,
        ["python3", "-c", "import os; print('canary=' + os.environ['CANARY'])"],
        attempt_id="sec-secret",
        secret_files={"CANARY": CANARY},
    )
    # The secret is delivered to a container tmpfs after start; it is never in
    # the command, labels, or the container environment visible to inspect.
    assert CANARY not in " ".join(request.command)
    assert CANARY not in str(request.labels)

    result = await sandbox.run(request)
    stdout = read_log(result.stdout_path)
    stderr = read_log(result.stderr_path)

    assert "canary=" in stdout
    assert CANARY not in stdout
    assert CANARY not in stderr
    assert "[REDACTED]" in stdout


async def test_secret_is_not_written_to_workspace_by_harness(sandbox: Any, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    request = make_request(
        workspace,
        ["python3", "-c", "import os; print(len(os.environ.get('CANARY', '')))"],
        attempt_id="sec-secret-length",
        secret_files={"CANARY": CANARY},
    )
    await sandbox.run(request)
    for path in workspace.rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_text(encoding="utf-8", errors="ignore")


async def test_registered_secret_is_redacted_even_without_known_pattern(
    sandbox: Any, tmp_path: Path
) -> None:
    """Arbitrary provider secrets are redacted via the runtime registry."""
    from devsandbox.common.redaction import get_redactor, reset_redactor

    arbitrary = "plain-provider-token-with-no-prefix-42"
    redactor = get_redactor()
    redactor.register_secret(arbitrary)
    try:
        workspace = tmp_path / "workspace"
        request = make_request(
            workspace,
            ["python3", "-c", "import os; print(os.environ['ARBITRARY'])"],
            attempt_id="sec-registered-secret",
            secret_files={"ARBITRARY": arbitrary},
        )
        result = await sandbox.run(request)
        stdout = read_log(result.stdout_path)
        assert arbitrary not in stdout
        assert "[REDACTED]" in stdout
    finally:
        reset_redactor()


async def test_secret_is_not_visible_through_docker_inspect(sandbox: Any, tmp_path: Path) -> None:
    """The secret must not appear in Config.Env/Cmd, only in the container tmpfs."""
    import asyncio

    from docker.errors import NotFound

    workspace = tmp_path / "workspace"
    request = make_request(
        workspace,
        ["python3", "-c", "import time; time.sleep(5)"],
        attempt_id="sec-secret-inspect",
        secret_files={"CANARY": CANARY},
    )

    run_task = asyncio.create_task(sandbox.run(request))
    try:
        container = None
        for _ in range(100):
            try:
                container = sandbox._client.containers.get("ds-sec-secret-inspect")
                break
            except NotFound:
                await asyncio.sleep(0.05)
        assert container is not None
        assert CANARY not in str(container.attrs)
        assert CANARY not in str(container.labels)
        assert CANARY not in " ".join(container.attrs.get("Config", {}).get("Cmd", []))
        assert CANARY not in str(container.attrs.get("Config", {}).get("Env", []))
        # The value is only in the read-only /run/secrets bind mount.
        secret_mounts = [
            mount
            for mount in container.attrs.get("Mounts", [])
            if mount.get("Destination") == "/run/secrets"
        ]
        assert len(secret_mounts) == 1
        assert secret_mounts[0].get("RW") is False
    finally:
        if not run_task.done():
            await sandbox.kill("ds-sec-secret-inspect")
        await run_task
