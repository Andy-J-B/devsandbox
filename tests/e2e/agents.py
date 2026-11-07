"""Deterministic, task-aware agent adapter for E2E scenarios.

The production ``FakeScriptAdapter`` runs one fixed mode for its whole
lifetime.  E2E scenarios need per-task behaviour (a task that fails once, two
tasks that edit the same file, a task that touches a restricted path), so this
adapter selects a :class:`Script` per request and delegates the real container
execution to ``FakeScriptAdapter``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from devsandbox.agents.fake import FakeAgentMode, FakeScriptAdapter
from devsandbox.agents.interfaces import AgentRunRequest, AgentRunResult
from devsandbox.common.types import AttemptId
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.interfaces import SandboxManager, SandboxRequest


@dataclass(frozen=True)
class Script:
    """One deterministic agent execution description."""

    mode: FakeAgentMode = "success"
    file: str | None = None
    content: str | None = None
    extra_args: tuple[str, ...] = ()
    command: tuple[str, ...] | None = None

    def args(self) -> list[str]:
        args = list(self.extra_args)
        if self.file is not None:
            args.extend(["--file", self.file])
        if self.content is not None:
            args.extend(["--content", self.content])
        return args


def _script_for_task(title: str) -> Script:
    """Default: write one unique markdown file per task."""
    from .harness import _slug

    return Script(file=f"docs/{_slug(title)}.md")


class ScriptedAgent:
    """Agent adapter driven by per-task scripts.

    ``scripts`` maps a task *title* to either a single :class:`Script` or a
    list consumed one entry per attempt.  Unmatched tasks use ``default`` (or
    write a unique markdown file).
    """

    name = "e2e-scripted"

    def __init__(
        self,
        sandbox: SandboxManager,
        settings: DevSandboxSettings,
        *,
        image: str,
        scripts: dict[str, Script | Sequence[Script]] | None = None,
        default: Script | None = None,
        secret_env: dict[str, object] | None = None,
        faults: object | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.sandbox = sandbox
        self.settings = settings
        self._image = image
        self._scripts = dict(scripts or {})
        self._default = default
        self._secret_env = secret_env or {}
        self._faults = faults
        self._delay_seconds = delay_seconds
        self.requests: list[AgentRunRequest] = []
        self._counts: dict[str, int] = {}
        self.active = 0
        self.peak_active = 0

    def _next_script(self, req: AgentRunRequest) -> Script:
        key = req.task.title
        configured = self._scripts.get(key)
        if configured is None:
            return self._default or _script_for_task(key)
        index = self._counts.get(key, 0)
        self._counts[key] = index + 1
        if isinstance(configured, Script):
            return configured
        if index < len(configured):
            return configured[index]
        return configured[-1]

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        """Record the request, select a script, and run it in a real sandbox."""
        self.requests.append(req.model_copy(deep=True))
        script = self._next_script(req)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self._delay_seconds:
                import asyncio

                await asyncio.sleep(self._delay_seconds)
            if script.command is not None:
                return await self.raw_run(req, command=list(script.command))
            adapter = FakeScriptAdapter(
                self.sandbox,
                self.settings,
                image=self._image,
                mode=script.mode,
                extra_args=script.args(),
                secret_env=self._secret_env,  # type: ignore[arg-type]
                faults=self._faults,
            )
            return await adapter.run(req)
        finally:
            self.active -= 1

    async def cancel(self, attempt_id: AttemptId) -> None:
        """Cancel a running attempt (delegates to the sandbox)."""

    async def raw_run(
        self,
        req: AgentRunRequest,
        *,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> AgentRunResult:
        """Run an arbitrary command in the same isolated container shape.

        Used by resource-abuse scenarios (OOM, fork bomb, log flood) that need
        a command the fake agent script does not model.
        """
        from devsandbox.agents.sandbox_result import (
            build_labels,
            clean_summary,
            map_exit_status,
            read_log_text,
            resolve_project_id,
            sanitize_log_file,
        )
        from devsandbox.common.time import utcnow
        from devsandbox.config.policy import SecurityPolicy

        policy = SecurityPolicy(self.settings)
        started_at = utcnow()
        request = SandboxRequest(
            attempt_id=req.workspace.attempt_id,
            image=self._image,
            workspace_path=Path(req.workspace.path).resolve(),
            command=command,
            env={
                "DEVSANDBOX_ATTEMPT": str(req.workspace.attempt_id),
                "DEVSANDBOX_TASK": req.task.id,
                "DEVSANDBOX_WORKSPACE": "/workspace",
                **(env or {}),
            },
            secret_env=self._secret_env,  # type: ignore[arg-type]
            resources=policy.effective_resources(req.resources),
            network=policy.effective_network(req.network),
            timeout_seconds=req.timeout_seconds,
            labels=build_labels(resolve_project_id(req), req.task.id, req.workspace.attempt_id),
        )
        result = await self.sandbox.run(request)
        stdout = read_log_text(result.stdout_path)
        stderr = read_log_text(result.stderr_path)
        sanitize_log_file(result.stdout_path)
        sanitize_log_file(result.stderr_path)
        return AgentRunResult(
            exit_status=map_exit_status(result),
            exit_code=result.exit_code,
            log_path=result.stdout_path,
            summary=clean_summary(stdout.strip() or stderr.strip()),
            duration_seconds=(utcnow() - started_at).total_seconds(),
        )


__all__ = ["Script", "ScriptedAgent"]
