"""Fake script adapter: runs the deterministic fake agent in a real sandbox.

Plans 08/11 use this to exercise the complete orchestrator, validation, and
integration path without any LLM or network access.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from ..common.time import utcnow
from ..common.types import AttemptId
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings
from ..sandbox.interfaces import SandboxManager, SandboxRequest
from .fake_agent_script import FAKE_AGENT_SOURCE
from .interfaces import AgentRunRequest, AgentRunResult
from .sandbox_result import (
    build_labels,
    clean_summary,
    map_exit_status,
    read_log_text,
    resolve_project_id,
    sanitize_log_file,
)

FakeAgentMode = Literal[
    "success",
    "fail",
    "hang",
    "no-commit",
    "touch-restricted",
    "lie",
    "leak-secret",
]

_MODE_FLAGS: dict[str, list[str]] = {
    "success": [],
    "fail": ["--fail"],
    "hang": ["--hang"],
    "no-commit": ["--no-commit"],
    "touch-restricted": ["--touch-restricted"],
    "lie": ["--lie"],
    "leak-secret": ["--leak-secret"],
}


class FakeScriptAdapter:
    """AgentAdapter that runs the deterministic fake agent via a sandbox."""

    name = "fake-script"

    def __init__(
        self,
        sandbox: SandboxManager,
        settings: DevSandboxSettings,
        *,
        image: str = "devsandbox/agent:latest",
        mode: FakeAgentMode = "success",
        extra_args: Sequence[str] = (),
        secret_env: dict[str, SecretStr] | None = None,
    ) -> None:
        if mode not in _MODE_FLAGS:
            raise ValueError(f"unknown fake agent mode: {mode}")
        self._sandbox = sandbox
        self._policy = SecurityPolicy(settings)
        self._image = image
        self._mode = mode
        self._extra_args = list(extra_args)
        self._secret_env = dict(secret_env or {})

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        """Run the fake agent for ``req`` and map the container result."""
        project_id = resolve_project_id(req)
        command = [
            "python3",
            "-c",
            FAKE_AGENT_SOURCE,
            *_MODE_FLAGS[self._mode],
            *self._extra_args,
        ]
        env = {
            "DEVSANDBOX_ATTEMPT": str(req.workspace.attempt_id),
            "DEVSANDBOX_TASK": req.task.id,
            "DEVSANDBOX_WORKSPACE": "/workspace",
        }
        sandbox_request = SandboxRequest(
            attempt_id=req.workspace.attempt_id,
            image=self._image,
            workspace_path=Path(req.workspace.path).resolve(),
            command=command,
            env=env,
            secret_env=self._secret_env,
            resources=self._policy.effective_resources(req.resources),
            network=self._policy.effective_network(req.network),
            timeout_seconds=req.timeout_seconds,
            labels=build_labels(project_id, req.task.id, req.workspace.attempt_id),
        )

        started_at = utcnow()
        result = await self._sandbox.run(sandbox_request)
        duration = (utcnow() - started_at).total_seconds()
        stdout = read_log_text(result.stdout_path)
        stderr = read_log_text(result.stderr_path)
        sanitize_log_file(result.stdout_path)
        sanitize_log_file(result.stderr_path)
        summary = clean_summary(stdout.strip() or stderr.strip())

        return AgentRunResult(
            exit_status=map_exit_status(result),
            exit_code=result.exit_code,
            log_path=result.stdout_path,
            summary=summary,
            duration_seconds=duration,
        )

    async def cancel(self, attempt_id: AttemptId) -> None:
        """Kill the deterministic container for ``attempt_id``."""
        await self._sandbox.kill(f"ds-{attempt_id}")


__all__ = ["FakeAgentMode", "FakeScriptAdapter"]
