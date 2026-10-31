"""Fake git manager implementing the GitManager protocol.

Owned by plan 01 stubs, completed by plan 03.
"""

from __future__ import annotations

from typing import Any

from devsandbox.common.ids import AttemptId
from devsandbox.common.types import RepoHandle, RepoSource
from devsandbox.gitmgr import IntegrationCandidate
from devsandbox.gitmgr.interfaces import ChangeSet, IntegrationResult, PushResult, Workspace


class FakeGitManager:
    """Scriptable fake git manager.

    Holds an in-memory "staging head" per project and supports scriptable
    change sets, integration outcomes (including conflicts), restricted-path
    violations, and CAS-style commit failures.
    """

    def __init__(self) -> None:
        self.workspaces: list[Workspace] = []
        self.change_sets: list[ChangeSet] = []
        self.integrations: list[IntegrationResult] = []
        self.staging_heads: dict[str, list[str | None]] = {}
        self.commit_failures: dict[str, int] = {}  # project_id -> remaining failures
        self.calls: list[dict[str, Any]] = []

    def _staging_heads(self, project_id: str) -> list[str | None]:
        return self.staging_heads.setdefault(project_id, [None])

    async def init_project_repo(self, project_id: str, source: RepoSource) -> RepoHandle:
        """Create a project repo handle."""
        self.calls.append({"op": "init_project_repo", "project_id": project_id})
        self._staging_heads(project_id)
        return RepoHandle(
            project_id=project_id,
            path=f"/tmp/fake/{project_id}/repo.git",
            url=source.url,
            ref=source.ref,
            main_branch="main",
            staging_branch=f"devsandbox/{project_id}/staging",
        )

    async def create_workspace(
        self, repo: RepoHandle, attempt_id: AttemptId, base_ref: str
    ) -> Workspace:
        """Create a fake workspace."""
        self.calls.append({"op": "create_workspace", "attempt_id": str(attempt_id)})
        workspace = Workspace(
            attempt_id=attempt_id,
            path=f"/tmp/fake/{repo.project_id}/ws-{attempt_id}",
            base_commit="abc123",
            branch=f"devsandbox/{repo.project_id}/{attempt_id}/a1",
        )
        self.workspaces.append(workspace)
        return workspace

    async def collect_changes(self, ws: Workspace) -> ChangeSet:
        """Collect a scripted change set."""
        self.calls.append({"op": "collect_changes", "attempt_id": str(ws.attempt_id)})
        if self.change_sets:
            return self.change_sets.pop(0)
        return ChangeSet(base_commit=ws.base_commit, head_commit="deadbeef")

    async def check_restricted_paths(self, cs: ChangeSet, restricted: list[str]) -> list[str]:
        """Return restricted paths touched by the change set."""
        self.calls.append({"op": "check_restricted_paths"})
        return [f for f in restricted if f in cs.files_changed]

    async def integrate_to_staging(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationResult:
        """Integrate into staging, honoring scripted CAS failures."""
        self.calls.append({"op": "integrate_to_staging", "attempt_id": str(ws.attempt_id)})
        if self.integrations:
            return self.integrations.pop(0)
        project_id = repo.project_id
        failures = self.commit_failures.get(project_id, 0)
        if failures > 0:
            self.commit_failures[project_id] = failures - 1
            return IntegrationResult(
                status="rejected",
                staging_commit=None,
                detail="staging moved during integration (simulated CAS failure)",
            )
        heads = self._staging_heads(project_id)
        new_head = "new-staging-commit"
        if heads and heads[0] is not None:
            new_head = heads[0] + "-child"
        heads[0] = new_head
        return IntegrationResult(status="integrated", staging_commit=new_head)

    async def staging_head(self, repo: RepoHandle) -> str | None:
        """Return the staging head."""
        self.calls.append({"op": "staging_head"})
        heads = self._staging_heads(repo.project_id)
        return heads[0] if heads else None

    async def reset_staging_to(self, repo: RepoHandle, commit: str) -> None:
        """Reset staging to a commit."""
        self.calls.append({"op": "reset_staging_to", "commit": commit})
        self._staging_heads(repo.project_id)[0] = commit

    async def remove_workspace(self, ws: Workspace, keep_for_diagnostics: bool) -> None:
        """Remove a workspace."""
        self.calls.append({"op": "remove_workspace", "keep": keep_for_diagnostics})

    async def push_branch(self, repo: RepoHandle, branch: str) -> PushResult:
        """Push a branch."""
        self.calls.append({"op": "push_branch", "branch": branch})
        return PushResult(pushed=True, remote_ref=branch, remote_commit="cafe1234")

    async def prepare_integration(self, repo: RepoHandle, ws: Workspace, cs: ChangeSet) -> Any:
        """Prepare a fake integration candidate (scriptable conflict)."""
        self.calls.append({"op": "prepare_integration", "attempt_id": str(ws.attempt_id)})
        if self.integrations:
            result = self.integrations.pop(0)
            return IntegrationCandidate(
                attempt_id=ws.attempt_id,
                repo=repo,
                staging_before=await self.staging_head(repo),
                staging_after=result.staging_commit or "candidate",
                conflicting_files=result.conflicting_files,
                conflicted=result.status == "conflict",
                integration_ref="refs/devsandbox/integration-candidate/fake",
            )
        heads = self._staging_heads(repo.project_id)
        before = heads[0] if heads else None
        after = before + "-child" if before else "candidate-commit"
        return IntegrationCandidate(
            attempt_id=ws.attempt_id,
            repo=repo,
            staging_before=before,
            staging_after=after,
            conflicting_files=[],
            conflicted=False,
            integration_ref="refs/devsandbox/integration-candidate/fake",
        )

    async def commit_integration(self, candidate: Any) -> IntegrationResult:
        """Commit a fake integration candidate."""
        self.calls.append({"op": "commit_integration", "attempt_id": str(candidate.attempt_id)})
        if candidate.conflicted:
            return IntegrationResult(
                status="conflict",
                conflicting_files=list(candidate.conflicting_files),
                detail="merge conflict; staging unchanged",
            )
        heads = self._staging_heads(candidate.repo.project_id)
        if heads and heads[0] != candidate.staging_before:
            return IntegrationResult(
                status="rejected",
                staging_commit=None,
                detail="staging moved during integration (simulated CAS failure)",
            )
        heads[0] = candidate.staging_after
        return IntegrationResult(
            status="integrated",
            staging_commit=candidate.staging_after,
            detail="staging advanced via CAS update-ref",
        )

    async def abort_integration(self, candidate: Any) -> None:
        """Abort a fake integration candidate."""
        self.calls.append({"op": "abort_integration", "attempt_id": str(candidate.attempt_id)})
