"""Context manager protocol (implementation owned by plan 05)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.types import (
    ContextBundle,
    ProjectContext,
    RepoHandle,
    RepoProfile,
    TaskOutcome,
    TaskSpec,
)


@runtime_checkable
class ContextManager(Protocol):
    """Protocol implemented by the context manager / repo analyzer."""

    async def analyze_repo(self, repo: RepoHandle, revision: str) -> RepoProfile: ...

    async def get_project_context(self, project_id: str, revision: str) -> ProjectContext: ...

    async def get_task_context(
        self, project_id: str, task: TaskSpec, revision: str
    ) -> ContextBundle: ...

    async def is_stale(self, artifact_id: str, current_revision: str) -> bool: ...

    async def record_task_outcome(
        self, project_id: str, task_id: str, outcome: TaskOutcome
    ) -> None: ...
