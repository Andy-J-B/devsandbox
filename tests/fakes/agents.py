"""Fake agent adapter implementing the AgentAdapter protocol."""

from __future__ import annotations

from devsandbox.agents.interfaces import AgentRunRequest, AgentRunResult
from devsandbox.common.ids import AttemptId


class FakeAgentAdapter:
    """Scriptable fake agent adapter."""

    def __init__(self) -> None:
        self.results: list[AgentRunResult] = []
        self.errors: list[BaseException] = []
        self.cancelled: list[AttemptId] = []
        self.calls: list[AgentRunRequest] = []

    def queue_result(self, result: AgentRunResult) -> None:
        """Queue a successful result for the next run."""
        self.results.append(result)

    def fail(self, error: BaseException) -> None:
        """Queue an error for the next run."""
        self.errors.append(error)

    @property
    def name(self) -> str:
        """Adapter name."""
        return "fake-opencode"

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        """Run the agent against a task."""
        self.calls.append(req)
        if self.errors:
            raise self.errors.pop(0)
        return self.results.pop(0)

    async def cancel(self, attempt_id: AttemptId) -> None:
        """Cancel an attempt."""
        self.cancelled.append(attempt_id)
