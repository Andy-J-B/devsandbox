"""Sandbox manager protocol (implementation owned by plan 02)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, SecretStr

from ..common.types import AttemptId, NetworkPolicy, ResourceProfile


class ExecResult(BaseModel):
    """Result of an exec inside a sandbox container."""

    exit_code: int | None
    timed_out: bool
    oom_killed: bool


class ManagedContainer(BaseModel):
    """A DevSandbox-managed container as observed by the orchestrator."""

    container_id: str
    state: str
    labels: dict[str, str] = {}


class SandboxRequest(BaseModel):
    """A sandbox execution request."""

    attempt_id: AttemptId
    image: str
    workspace_path: Path
    command: list[str]
    env: dict[str, str] = {}
    secret_env: dict[str, SecretStr] = {}
    resources: ResourceProfile = ResourceProfile()
    network: NetworkPolicy = NetworkPolicy()
    timeout_seconds: int
    labels: dict[str, str] = {}


class SandboxResult(BaseModel):
    """Result of a sandbox run."""

    container_id: str
    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    stdout_path: Path
    stderr_path: Path
    started_at: datetime
    finished_at: datetime
    peak_memory_mb: int | None


@runtime_checkable
class SandboxManager(Protocol):
    """Protocol implemented by the Docker sandbox manager."""

    async def run(self, req: SandboxRequest) -> SandboxResult: ...

    async def exec_in(
        self, container_id: str, cmd: list[str], timeout_seconds: int
    ) -> ExecResult: ...

    async def kill(self, container_id: str) -> None: ...

    async def destroy(self, container_id: str) -> None: ...

    async def list_managed(self) -> list[ManagedContainer]: ...

    async def inspect(self, container_id: str) -> ManagedContainer | None: ...
