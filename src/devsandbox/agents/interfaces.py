"""Agent adapter protocol (OpenCode implementation owned by plan 04)."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..common.types import (
    AttemptId,
    ContextBundle,
    ModelConfig,
    NetworkPolicy,
    ResourceProfile,
    TaskSpec,
    TokenUsage,
)
from ..gitmgr.interfaces import Workspace


class AgentRunRequest(BaseModel):
    """A single agent run request."""

    task: TaskSpec
    context_bundle: ContextBundle
    workspace: Workspace
    attempt_number: int
    prior_failure_summary: str | None = None
    resources: ResourceProfile = ResourceProfile()
    network: NetworkPolicy = NetworkPolicy()
    timeout_seconds: int
    model: ModelConfig = ModelConfig()


class AgentRunResult(BaseModel):
    """Result of a single agent run."""

    exit_status: Literal["exited_ok", "exited_error", "timeout", "oom", "crashed", "sandbox_error"]
    exit_code: int | None = None
    log_path: str = ""
    summary: str | None = None
    token_usage: TokenUsage | None = None
    duration_seconds: float = 0.0


@runtime_checkable
class AgentAdapter(Protocol):
    """Protocol implemented by agent adapters (OpenCode first)."""

    name: str

    async def run(self, req: AgentRunRequest) -> AgentRunResult: ...

    async def cancel(self, attempt_id: AttemptId) -> None: ...
