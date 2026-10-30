"""Fake git manager implementing the GitManager protocol."""

from __future__ import annotations

from devsandbox.common.ids import AttemptId
from devsandbox.common.types import RepoHandle, RepoSource
from devsandbox.gitmgr.interfaces import ChangeSet, IntegrationResult, PushResult, Workspace


class FakeGitManager:
    """Scriptable fake git manager."""

    def __init__(self) -> None:
        self.workspaces: list[Workspace] = []
        self.change_sets: list[ChangeSet] = []
        self.integrations: list[IntegrationResult] = []
        self.staging_heads: list[str | None] = [None]

    async def init_project_repo(self, project_id: str, source: RepoSource) -> RepoHandle:
        """Create a project repo handle."""
        return RepoHandle(
            project_id=project_id, path=f"/tmp/fake/{project_id}", url=source.url, ref=source.ref
        )

    async def create_workspace(
        self, repo: RepoHandle, attempt_id: AttemptId, base_ref: str
    ) -> Workspace:
        """Create a fake workspace."""
        workspace = Workspace(
            attempt_id=attempt_id,
            path=f"/tmp/fake/{repo.project_id}/ws-{attempt_id}",
            base_commit="deadbeef",
            branch=f"devsandbox/{repo.project_id}/{attempt_id}/a1",
        )
        self.workspaces.append(workspace)
        return workspace

    async def collect_changes(self, ws: Workspace) -> ChangeSet:
        """Collect a scripted change set."""
        if self.change_sets:
            return self.change_sets.pop(0)
        return ChangeSet(base_commit=ws.base_commit, head_commit=None)

    async def check_restricted_paths(self, cs: ChangeSet, restricted: list[str]) -> list[str]:
        """Return restricted paths touched by the change set."""
        return [f for f in restricted if f in cs.files_changed]

    async def integrate_to_staging(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationResult:
        """Integrate into staging."""
        if self.integrations:
            return self.integrations.pop(0)
        return IntegrationResult(status="noop")

    async def staging_head(self, repo: RepoHandle) -> str | None:
        """Return the staging head."""
        return self.staging_heads.pop(0) if self.staging_heads else None

    async def reset_staging_to(self, repo: RepoHandle, commit: str) -> None:
        """Reset staging to a commit."""

    async def remove_workspace(self, ws: Workspace, keep_for_diagnostics: bool) -> None:
        """Remove a workspace."""

    async def push_branch(self, repo: RepoHandle, branch: str) -> PushResult:
        """Push a branch."""
        return PushResult(pushed=True, remote_ref=branch, remote_commit="cafe1234")
