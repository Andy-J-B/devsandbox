"""Sandbox manager protocol (implementation owned by plan 02)."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, SecretStr, field_validator, model_validator

from ..common.types import AttemptId, NetworkPolicy, ResourceProfile

# Secret names become environment variable names inside the container, so keep
# them to a conservative shell/env identifier subset.  This also prevents path
# traversal when they are written as files under /run/secrets.
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """A sandbox execution request.

    Secrets are delivered as files in a private directory that is bind-mounted
    read-only at ``/run/secrets`` and exported by a small wrapper before the
    real command starts.  The legacy ``secret_env`` field is still accepted and
    is converted to the same file-based path by the sandbox manager; it is
    never placed in the container environment at create time, so it is not
    visible through ``docker inspect``.  Set ``security.secret_dir`` to a host
    tmpfs (for example ``/dev/shm/devsandbox-secrets``) to keep the files in
    memory.
    """

    attempt_id: AttemptId
    image: str
    workspace_path: Path
    command: list[str]
    env: dict[str, str] = {}
    secret_files: dict[str, SecretStr] = {}
    # Deprecated alias for secret_files; kept for callers migrating from the
    # original env-injection API.
    secret_env: dict[str, SecretStr] = {}
    resources: ResourceProfile = ResourceProfile()
    network: NetworkPolicy = NetworkPolicy()
    timeout_seconds: int
    labels: dict[str, str] = {}

    @field_validator("secret_files", "secret_env")
    @classmethod
    def _validate_secret_names(cls, value: dict[str, SecretStr]) -> dict[str, SecretStr]:
        for name in value:
            if not _SECRET_NAME_RE.match(name):
                raise ValueError(f"invalid secret name {name!r}; use [A-Za-z_][A-Za-z0-9_]*")
        return value

    @model_validator(mode="after")
    def _secret_names_must_not_collide(self) -> SandboxRequest:
        overlap = set(self.secret_files) & set(self.secret_env)
        if overlap:
            raise ValueError(
                "secret names appear in both secret_files and secret_env: "
                + ", ".join(sorted(overlap))
            )
        env_overlap = (set(self.secret_files) | set(self.secret_env)) & set(self.env)
        if env_overlap:
            raise ValueError(
                "secret names must not also be plain env values: " + ", ".join(sorted(env_overlap))
            )
        return self


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
