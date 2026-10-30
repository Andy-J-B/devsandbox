"""Fake sandbox manager implementing the SandboxManager protocol."""

from __future__ import annotations

from devsandbox.sandbox.interfaces import (
    ExecResult,
    ManagedContainer,
    SandboxRequest,
    SandboxResult,
)


class FakeSandboxManager:
    """Scriptable fake sandbox manager."""

    def __init__(self) -> None:
        self.results: list[SandboxResult] = []
        self.errors: list[BaseException] = []
        self.killed: list[str] = []
        self.destroyed: list[str] = []
        self.calls: list[SandboxRequest] = []
        self.exec_calls: list[tuple[str, list[str], int]] = []
        self.managed: list[ManagedContainer] = []

    def queue_result(self, result: SandboxResult) -> None:
        """Queue a successful result for the next run."""
        self.results.append(result)

    def fail(self, error: BaseException) -> None:
        """Queue an error for the next run."""
        self.errors.append(error)

    async def run(self, req: SandboxRequest) -> SandboxResult:
        """Run a sandboxed command."""
        self.calls.append(req)
        if self.errors:
            raise self.errors.pop(0)
        return self.results.pop(0)

    async def exec_in(self, container_id: str, cmd: list[str], timeout_seconds: int) -> ExecResult:
        """Exec inside a container."""
        self.exec_calls.append((container_id, list(cmd), timeout_seconds))
        return ExecResult(exit_code=0, timed_out=False, oom_killed=False)

    async def kill(self, container_id: str) -> None:
        """Kill a container."""
        self.killed.append(container_id)

    async def destroy(self, container_id: str) -> None:
        """Destroy a container."""
        self.destroyed.append(container_id)

    async def list_managed(self) -> list[ManagedContainer]:
        """List managed containers."""
        return list(self.managed)

    async def inspect(self, container_id: str) -> ManagedContainer | None:
        """Inspect a managed container."""
        for container in self.managed:
            if container.container_id == container_id:
                return container
        return None
