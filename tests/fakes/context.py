"""Fake context manager implementing the ContextManager protocol."""

from __future__ import annotations

from devsandbox.common.types import (
    ContextBundle,
    ProjectContext,
    RepoProfile,
    TaskOutcome,
    TaskOutcomeNote,
    TaskSpec,
)


class FakeContextManager:
    """Scriptable fake context manager with optional canned profile/bundles."""

    def __init__(
        self,
        *,
        profiles: list[RepoProfile] | None = None,
        bundles: dict[str, ContextBundle] | list[ContextBundle] | None = None,
        project_contexts: list[ProjectContext] | None = None,
        stale_artifacts: set[str] | None = None,
    ) -> None:
        self.repo_profiles: list[RepoProfile] = []
        self.project_contexts: list[ProjectContext] = []
        self.task_contexts: list[ContextBundle] = []
        self.outcomes: list[tuple[str, str, TaskOutcome | TaskOutcomeNote]] = []
        self.outcome_notes: list[TaskOutcomeNote] = []
        self._profiles = list(profiles or [])
        self._bundles = bundles
        self._project_contexts = list(project_contexts or [])
        self._stale_artifacts = set(stale_artifacts or ())

    def _next_profile(self, revision: str) -> RepoProfile:
        profile = self._profiles.pop(0) if self._profiles else RepoProfile(name="fake-repo")
        profile = profile.model_copy(update={"revision": revision})
        self.repo_profiles.append(profile)
        return profile

    def _bundle_for(self, task: TaskSpec, revision: str) -> ContextBundle:
        if isinstance(self._bundles, dict) and task.id in self._bundles:
            bundle = self._bundles[task.id]
        elif isinstance(self._bundles, list) and self._bundles:
            bundle = self._bundles.pop(0)
        else:
            bundle = ContextBundle(task_id=task.id, task=task, context_files=["src/main.py"])
        bundle = bundle.model_copy(update={"revision": revision})
        self.task_contexts.append(bundle)
        return bundle

    async def analyze_repo(self, repo, revision: str) -> RepoProfile:
        """Analyze a repo (or return the next canned profile)."""
        return self._next_profile(revision)

    async def get_project_context(self, project_id: str, revision: str) -> ProjectContext:
        """Return project context (canned, else a minimal default)."""
        ctx = (
            self._project_contexts.pop(0)
            if self._project_contexts
            else ProjectContext(goals=["g1"])
        )
        self.project_contexts.append(ctx)
        return ctx

    async def get_task_context(
        self, project_id: str, task: TaskSpec, revision: str
    ) -> ContextBundle:
        """Return a canned bundle or a minimal default."""
        return self._bundle_for(task, revision)

    async def is_stale(self, artifact_id: str, current_revision: str) -> bool:
        """Return scripted staleness."""
        return artifact_id in self._stale_artifacts

    async def record_task_outcome(
        self, project_id: str, task_id: str, outcome: TaskOutcome | TaskOutcomeNote
    ) -> None:
        """Record task outcome (enum or structured note)."""
        self.outcomes.append((project_id, task_id, outcome))
        if isinstance(outcome, TaskOutcomeNote):
            self.outcome_notes.append(outcome)
