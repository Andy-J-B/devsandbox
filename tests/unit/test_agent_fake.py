"""Tests for the deterministic fake agent script and FakeScriptAdapter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from tests.agent_helpers import (
    git_status,
    make_request,
    make_settings,
    make_workspace,
    sandbox_result,
    write_logs,
)
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.agents import fake_agent_script
from devsandbox.agents.fake import FakeScriptAdapter

SCRIPT = Path(fake_agent_script.__file__)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _run_script(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def _commit_count(repo: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_fake_agent_success_edits_and_commits(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo)
    assert result.returncode == 0
    assert (repo / "AGENT_OUTPUT.md").exists()
    assert _commit_count(repo) == 2
    assert "FAKE_AGENT_SUMMARY: committed" in result.stdout


def test_fake_agent_no_commit_leaves_dirty_tree(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo, "--no-commit")
    assert result.returncode == 0
    assert (repo / "AGENT_OUTPUT.md").exists()
    assert "AGENT_OUTPUT.md" in git_status(repo)
    assert _commit_count(repo) == 1


def test_fake_agent_lie_makes_no_changes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo, "--lie")
    assert result.returncode == 0
    assert not (repo / "AGENT_OUTPUT.md").exists()
    assert git_status(repo) == ""
    assert "all tests pass" in result.stdout


def test_fake_agent_fail_exits_nonzero(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo, "--fail")
    assert result.returncode == 1
    assert "requested failure" in result.stderr


def test_fake_agent_touches_restricted_path(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo, "--touch-restricted")
    assert result.returncode == 0
    assert (repo / ".github" / "workflows" / "devsandbox-fake.yml").exists()
    assert _commit_count(repo) == 2


def test_fake_agent_leak_secret_prints_canary(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(
        repo, "--leak-secret", env={"DEVSANDBOX_FAKE_SECRET": "canary-from-secret-env"}
    )
    assert result.returncode == 0
    assert "canary-from-secret-env" in result.stdout


def test_fake_agent_hang_mode_is_available(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    result = _run_script(repo, "--hang", "--hang-seconds", "0.01")
    assert result.returncode == 0
    assert "FAKE_AGENT_HANG" in result.stdout


async def test_fake_script_adapter_builds_sandbox_request(tmp_path: Path) -> None:
    workspace, workspace_path = make_workspace(tmp_path)
    sandbox = FakeSandboxManager()
    adapter = FakeScriptAdapter(
        sandbox,
        make_settings(),
        image="agent-test:latest",
        mode="touch-restricted",
        secret_env={"DEVSANDBOX_FAKE_SECRET": SecretStr("canary")},
    )
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path, stdout="FAKE_AGENT_SUMMARY: ok\n")))

    result = await adapter.run(make_request(workspace))

    assert result.exit_status == "exited_ok"
    call = sandbox.calls[0]
    assert call.image == "agent-test:latest"
    assert call.workspace_path == workspace_path.resolve()
    assert call.command[0:2] == ["python3", "-c"]
    assert "--touch-restricted" in call.command
    assert call.env["DEVSANDBOX_ATTEMPT"] == "attempt-1"
    assert call.env["DEVSANDBOX_TASK"] == "task-1"
    assert call.secret_env["DEVSANDBOX_FAKE_SECRET"].get_secret_value() == "canary"
    assert call.secret_env["DEVSANDBOX_FAKE_SECRET"].get_secret_value() not in " ".join(
        call.command
    )


async def test_fake_script_adapter_maps_failure_and_cancel(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    sandbox = FakeSandboxManager()
    adapter = FakeScriptAdapter(sandbox, make_settings(), mode="fail")
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path), exit_code=1))

    result = await adapter.run(make_request(workspace))
    assert result.exit_status == "exited_error"

    await adapter.cancel("attempt-9")
    assert sandbox.killed == ["ds-attempt-9"]


def test_fake_script_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FakeScriptAdapter(FakeSandboxManager(), make_settings(), mode="nonsense")  # type: ignore[arg-type]
