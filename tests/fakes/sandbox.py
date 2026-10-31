"""Fake sandbox manager implementing the SandboxManager protocol.

Owned by plan 01 as a stub and completed by plan 02.  Scriptable so other
plans (orchestrator, validation, ...) can unit-test against it without a
Docker daemon.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

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
        self._delay_seconds: float = 0.0

    # -- scripting ---------------------------------------------------------

    def queue_result(self, result: SandboxResult) -> None:
        """Queue a successful result for the next ``run``."""
        self.results.append(result)

    def queue_exit_code(
        self,
        exit_code: int,
        *,
        timed_out: bool = False,
        oom_killed: bool = False,
    ) -> None:
        """Queue a result with the given exit code for the next ``run``."""
        self.results.append(
            SandboxResult(
                container_id=f"container-{len(self.calls) + 1}",
                exit_code=exit_code,
                timed_out=timed_out,
                oom_killed=oom_killed,
                stdout_path=Path(f"/tmp/fake/{len(self.calls) + 1}.out"),
                stderr_path=Path(f"/tmp/fake/{len(self.calls) + 1}.err"),
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                peak_memory_mb=None,
            )
        )

    def queue_timeout(self) -> None:
        """Queue a timeout result for the next ``run``."""
        self.queue_exit_code(0, timed_out=True)

    def queue_oom(self) -> None:
        """Queue an OOM-killed result for the next ``run``."""
        self.queue_exit_code(137, oom_killed=True)

    def fail(self, error: BaseException) -> None:
        """Queue an error for the next ``run``."""
        self.errors.append(error)

    def set_delay(self, seconds: float) -> None:
        """Make every ``run`` sleep for ``seconds`` before returning."""
        self._delay_seconds = seconds

    # -- protocol ----------------------------------------------------------

    async def run(self, req: SandboxRequest) -> SandboxResult:
        """Run a sandboxed command."""
        self.calls.append(req)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self.errors:
            raise self.errors.pop(0)
        if self.results:
            return self.results.pop(0)
        # Default: successful run.
        return SandboxResult(
            container_id=f"container-{len(self.calls)}",
            exit_code=0,
            timed_out=False,
            oom_killed=False,
            stdout_path=Path(f"/tmp/fake/{len(self.calls)}.out"),
            stderr_path=Path(f"/tmp/fake/{len(self.calls)}.err"),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            peak_memory_mb=None,
        )

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
