"""Shared helpers for agent adapter tests (plan 04)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from devsandbox.agents.interfaces import AgentRunRequest
from devsandbox.common.types import (
    AttemptId,
    CheckSpec,
    ContextBundle,
    ModelConfig,
    NetworkPolicy,
    ResourceProfile,
    TaskSpec,
)
from devsandbox.config.settings import (
    AgentConfig,
    DevSandboxSettings,
    ModelBudget,
    SecretRef,
    SecurityConfig,
)
from devsandbox.gitmgr.interfaces import Workspace
from devsandbox.sandbox.interfaces import SandboxResult


def make_workspace(
    tmp_path: Path,
    *,
    attempt_id: str = "attempt-1",
    project_id: str = "proj-1",
    task_id: str = "task-1",
    initialise_git: bool = False,
) -> tuple[Workspace, Path]:
    """Create a workspace-shaped directory and return (Workspace, path)."""
    path = tmp_path / "workspaces" / project_id / attempt_id
    path.mkdir(parents=True, exist_ok=True)
    if initialise_git:
        _git(path, "init", "-b", "main")
        (path / "README.md").write_text("initial\n", encoding="utf-8")
        _git(path, "add", "README.md")
        _git(path, "commit", "-m", "initial")
    workspace = Workspace(
        attempt_id=AttemptId(attempt_id),
        path=str(path),
        base_commit="0" * 40,
        branch=f"devsandbox/{project_id}/{task_id}/a1",
    )
    return workspace, path


def make_request(
    workspace: Workspace,
    *,
    task: TaskSpec | None = None,
    model: ModelConfig | None = None,
    prior_failure_summary: str | None = None,
    attempt_number: int = 1,
    timeout_seconds: int = 60,
    network: NetworkPolicy | None = None,
    resources: ResourceProfile | None = None,
    validation_checks: list[CheckSpec] | None = None,
    context_instructions: str = "",
) -> AgentRunRequest:
    """Build a complete AgentRunRequest for tests."""
    task = task or TaskSpec(
        id="task-1",
        title="Implement the feature",
        description="Add the requested behaviour.",
        instructions="Edit the repository and commit.",
        success_criteria=["tests pass"],
        expected_files=["src/feature.py"],
        restricted_files=[".github/workflows/**"],
    )
    return AgentRunRequest(
        task=task,
        context_bundle=ContextBundle(
            task_id=task.id,
            task=task,
            context_files=["src/main.py", "README.md"],
            instructions=context_instructions,
        ),
        workspace=workspace,
        attempt_number=attempt_number,
        prior_failure_summary=prior_failure_summary,
        resources=resources or ResourceProfile(),
        network=network or NetworkPolicy(mode="none"),
        timeout_seconds=timeout_seconds,
        model=model or ModelConfig(provider="openrouter", model="free/model"),
        project_id="proj-1",
        validation_checks=validation_checks
        or [CheckSpec(name="unit", run="pytest -q", timeout_seconds=120)],
    )


def make_settings(
    *,
    max_tokens: int | None = None,
    max_requests: int | None = None,
    secrets: dict[str, SecretRef] | None = None,
    allowed_images: list[str] | None = None,
    allow_network_default: bool = True,
) -> DevSandboxSettings:
    """Build settings suitable for unit tests."""
    security = SecurityConfig(
        allow_network_default=allow_network_default,
        allow_push=False,
        allow_main_merge=False,
        allowed_images=allowed_images or [],
    )
    return DevSandboxSettings(
        security=security,
        agent=AgentConfig(
            model_budget=ModelBudget(max_tokens=max_tokens, max_requests=max_requests)
        ),
        secrets=secrets or {},
    )


def write_logs(tmp_path: Path, stdout: str = "", stderr: str = "") -> tuple[Path, Path]:
    """Write fake sandbox logs and return their paths."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return stdout_path, stderr_path


def sandbox_result(
    stdout_path: Path,
    stderr_path: Path,
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    oom_killed: bool = False,
    container_id: str = "container-1",
) -> SandboxResult:
    """Build a SandboxResult pointing at fake logs."""
    now = datetime.now(UTC)
    return SandboxResult(
        container_id=container_id,
        exit_code=exit_code,
        timed_out=timed_out,
        oom_killed=oom_killed,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=now,
        finished_at=now,
        peak_memory_mb=None,
    )


def git_status(path: Path) -> str:
    """Return ``git status --porcelain`` for ``path``."""
    return _git(path, "status", "--porcelain").stdout


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    )


__all__ = [
    "git_status",
    "make_request",
    "make_settings",
    "make_workspace",
    "sandbox_result",
    "write_logs",
]
