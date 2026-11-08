"""Resource-abuse containment: pids, memory, and log flooding.

Disk usage is *not* quota-enforced in the MVP (the workspace is a bind mount),
so disk-fill abuse is documented as a residual risk in ``docs/security-review.md``
rather than tested as a containment guarantee.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.security.conftest import make_request, read_log

from devsandbox.common.types import ResourceProfile
from devsandbox.sandbox.manager import _TRUNCATION_MARKER

pytestmark = pytest.mark.security


async def test_fork_bomb_is_stopped_by_pids_limit(sandbox: Any, tmp_path: Path) -> None:
    code = (
        "import os, sys\n"
        "pids = []\n"
        "try:\n"
        "    for _ in range(500):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            os._exit(0)\n"
        "        pids.append(pid)\n"
        "except OSError:\n"
        "    print('FORK-LIMIT')\n"
        "    sys.exit(0)\n"
        "for pid in pids:\n"
        "    os.waitpid(pid, 0)\n"
        "print('FORKED', len(pids))\n"
    )
    result = await sandbox.run(
        make_request(
            tmp_path / "workspace",
            ["python3", "-c", code],
            attempt_id="sec-fork-bomb",
            resources=ResourceProfile(cpu_cores=0.5, memory_mb=256, pids_limit=32),
            timeout_seconds=60,
        )
    )

    stdout = read_log(result.stdout_path)
    assert "FORK-LIMIT" in stdout, stdout + read_log(result.stderr_path)
    assert "FORKED 500" not in stdout


async def test_memory_bomb_is_oom_killed(sandbox: Any, tmp_path: Path) -> None:
    result = await sandbox.run(
        make_request(
            tmp_path / "workspace",
            ["python3", "-c", "x = bytearray(1_000_000_000); print(len(x))"],
            attempt_id="sec-memory-bomb",
            resources=ResourceProfile(cpu_cores=0.5, memory_mb=128, pids_limit=64),
            timeout_seconds=60,
        )
    )

    assert result.oom_killed is True
    assert result.exit_code != 0


async def test_log_flood_is_capped(sandbox: Any, tmp_path: Path) -> None:
    result = await sandbox.run(
        make_request(
            tmp_path / "workspace",
            ["python3", "-c", "import sys; sys.stdout.write('x' * 1_000_000)"],
            attempt_id="sec-log-flood",
            timeout_seconds=60,
        )
    )

    assert result.exit_code == 0
    raw = result.stdout_path.read_bytes()
    marker = _TRUNCATION_MARKER
    assert marker in raw
    assert len(raw) <= 4096 + len(marker)
