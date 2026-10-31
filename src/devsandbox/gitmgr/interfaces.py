"""Git manager protocol (implementation owned by plan 03)."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..common.types import AttemptId, RepoHandle, RepoSource


class Workspace(BaseModel):
    """An isolated git worktree/clone for one attempt."""

    attempt_id: AttemptId
    path: str
    base_commit: str
    branch: str


class CommitInfo(BaseModel):
    """A single commit in a change set."""

    sha: str
    message: str


class ChangeSet(BaseModel):
    """Collected changes for one attempt workspace."""

    base_commit: str
    head_commit: str | None = None
    commits: list[CommitInfo] = []
    files_changed: list[str] = []
    diff_stat: str = ""
    uncommitted_committed_by_orchestrator: bool = False


class IntegrationResult(BaseModel):
    """Result of integrating a change set into staging."""

    status: Literal["integrated", "conflict", "rejected", "noop"]
    staging_commit: str | None = None
    conflicting_files: list[str] = []
    detail: str = ""


class PushResult(BaseModel):
    """Result of pushing a staging branch."""

    pushed: bool = False
    remote_ref: str | None = None
    remote_commit: str | None = None
    detail: str = ""


@runtime_checkable
class GitManager(Protocol):
    """Protocol implemented by the git manager."""

    async def init_project_repo(self, project_id: str, source: RepoSource) -> RepoHandle: ...

    async def create_workspace(
        self, repo: RepoHandle, attempt_id: AttemptId, base_ref: str
    ) -> Workspace: ...

    async def collect_changes(self, ws: Workspace) -> ChangeSet: ...

    async def check_restricted_paths(self, cs: ChangeSet, restricted: list[str]) -> list[str]: ...

    async def integrate_to_staging(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationResult: ...

    async def staging_head(self, repo: RepoHandle) -> str | None: ...

    async def reset_staging_to(self, repo: RepoHandle, commit: str) -> None: ...

    async def remove_workspace(self, ws: Workspace, keep_for_diagnostics: bool) -> None: ...

    async def push_branch(self, repo: RepoHandle, branch: str) -> PushResult: ...
