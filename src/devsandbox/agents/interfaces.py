"""Agent adapter protocol and request/result models (plan 04).

The protocol is runtime-agnostic: OpenCode is the first adapter, but any
runtime that can run non-interactively inside a sandbox container can be
added by implementing :class:`AgentAdapter` and registering it with
:class:`~devsandbox.agents.registry.AgentRegistry`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..common.types import (
    AttemptId,
    CheckSpec,
    ContextBundle,
    ModelConfig,
    NetworkPolicy,
    ResourceProfile,
    TaskSpec,
    TokenUsage,
)
from ..gitmgr.interfaces import Workspace

AgentExitStatus = Literal["exited_ok", "exited_error", "timeout", "oom", "crashed", "sandbox_error"]


class AgentRunRequest(BaseModel):
    """A single agent run request.

    ``project_id`` is optional for backwards compatibility with plan 01; when
    omitted the adapter derives it from the deterministic branch name
    (``devsandbox/<project>/<task>/a<n>``).  ``validation_checks`` are the
    independent checks the orchestrator will run, surfaced to the agent in the
    prompt so it can self-check.
    """

    model_config = ConfigDict(extra="forbid")

    task: TaskSpec
    context_bundle: ContextBundle
    workspace: Workspace
    attempt_number: int
    prior_failure_summary: str | None = None
    resources: ResourceProfile = Field(default_factory=ResourceProfile)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    timeout_seconds: int
    model: ModelConfig = Field(default_factory=ModelConfig)
    project_id: str | None = None
    validation_checks: list[CheckSpec] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """Result of a single agent run.

    ``summary`` is the agent's self-report and is therefore **untrusted**;
    completion decisions must be based on independent validation.  The
    orchestrator stores the result after redaction.
    """

    model_config = ConfigDict(extra="forbid")

    exit_status: AgentExitStatus
    exit_code: int | None = None
    log_path: Path | None = None
    summary: str | None = None
    token_usage: TokenUsage | None = None
    duration_seconds: float = 0.0


@runtime_checkable
class AgentAdapter(Protocol):
    """Protocol implemented by agent runtimes (OpenCode first)."""

    name: str

    async def run(self, req: AgentRunRequest) -> AgentRunResult: ...

    async def cancel(self, attempt_id: AttemptId) -> None: ...


__all__ = ["AgentAdapter", "AgentExitStatus", "AgentRunRequest", "AgentRunResult"]
