"""Fake context manager implementing the ContextManager protocol."""

from __future__ import annotations

from devsandbox.common.types import (
    ContextBundle,
    ProjectContext,
    RepoProfile,
    TaskOutcome,
    TaskSpec,
)


class FakeContextManager:
    """Scriptable fake context manager."""

    def __init__(self) -> None:
        self.repo_profiles: list[RepoProfile] = []
        self.project_contexts: list[ProjectContext] = []
        self.task_contexts: list[ContextBundle] = []
        self.outcomes: list[tuple[str, str, TaskOutcome]] = []

    async def analyze_repo(self, repo, revision: str) -> RepoProfile:
        """Analyze a repo."""
        self.repo_profiles.append(RepoProfile(name="fake-repo"))
        return RepoProfile(name="fake-repo")

    async def get_project_context(self, project_id: str, revision: str) -> ProjectContext:
        """Return project context."""
        ctx = ProjectContext(goals=["g1"])
        self.project_contexts.append(ctx)
        return ctx

    async def get_task_context(
        self, project_id: str, task: TaskSpec, revision: str
    ) -> ContextBundle:
        """Return task context."""
        bundle = ContextBundle(task_id=task.id, task=task, context_files=["src/main.py"])
        self.task_contexts.append(bundle)
        return bundle

    async def is_stale(self, artifact_id: str, current_revision: str) -> bool:
        """Return staleness."""
        return False

    async def record_task_outcome(
        self, project_id: str, task_id: str, outcome: TaskOutcome
    ) -> None:
        """Record task outcome."""
        self.outcomes.append((project_id, task_id, outcome))
