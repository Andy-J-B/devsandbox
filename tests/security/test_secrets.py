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
        secret_env={"CANARY": CANARY},
    )
    # The secret may only be present in the container environment, never in
    # command/labels (docker inspect of env is an accepted daemon-access risk).
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
        secret_env={"CANARY": CANARY},
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
            secret_env={"ARBITRARY": arbitrary},
        )
        result = await sandbox.run(request)
        stdout = read_log(result.stdout_path)
        assert arbitrary not in stdout
        assert "[REDACTED]" in stdout
    finally:
        reset_redactor()
