"""Context manager implementation wiring analysis, artifacts, summaries and bundles."""

from __future__ import annotations

from collections.abc import Sequence

from ..common.errors import ContextError
from ..common.types import (
    CheckSpec,
    ContextBundle,
    ProjectContext,
    RepoHandle,
    RepoProfile,
    TaskOutcome,
    TaskOutcomeNote,
    TaskSpec,
)
from .analyzer import RepoAnalyzer
from .outcomes import TaskOutcomeStore
from .store import (
    ArtifactKind,
    ArtifactStore,
    GeneratedArtifact,
    RepoLookup,
    depends_for_kind,
)
from .summary import SummaryService
from .task_context import DEFAULT_MAX_CHARS, TaskContextBuilder


class ContextManagerImpl:
    """Default context manager (plan 05).

    ``repo_lookup`` resolves a project id to its canonical bare repository
    handle.  Artifact persistence and summary generation are injected so the
    manager is usable with the real database, in-memory repositories, or tests.
    """

    def __init__(
        self,
        repo_lookup: RepoLookup,
        artifacts: ArtifactStore,
        *,
        analyzer: RepoAnalyzer | None = None,
        summary_service: SummaryService | None = None,
        outcome_store: TaskOutcomeStore | None = None,
        project_context: ProjectContext | None = None,
        max_context_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._repo_lookup = repo_lookup
        self._artifacts = artifacts
        self._analyzer = analyzer or RepoAnalyzer()
        self._summaries = summary_service or SummaryService()
        self._outcomes = outcome_store or TaskOutcomeStore(artifacts)
        self._project_context = project_context or ProjectContext()
        self._max_context_chars = max_context_chars
        self._register_generators()

    # -- protocol: analysis ------------------------------------------------

    async def analyze_repo(
        self,
        repo: RepoHandle,
        revision: str,
        *,
        validation_commands: Sequence[CheckSpec | str] | None = None,
    ) -> RepoProfile:
        """Analyze a revision (no code execution, no LLM calls)."""
        return await self._analyzer.analyze_repo(
            repo, revision, validation_commands=validation_commands
        )

    async def get_project_context(self, project_id: str, revision: str) -> ProjectContext:
        """Return project-level goal/requirement/constraint context."""
        return self._project_context

    # -- protocol: task context -------------------------------------------

    async def get_task_context(
        self,
        project_id: str,
        task: TaskSpec,
        revision: str,
        *,
        dependency_task_ids: list[str] | tuple[str, ...] | None = None,
    ) -> ContextBundle:
        """Build a revision-stamped, budgeted task context bundle."""
        repo = self._require_repo(project_id)
        profile = await self._profile(project_id, revision)
        summary_artifact = await self._artifacts.get_or_generate(
            ArtifactKind.PROJECT_SUMMARY, repo, revision
        )
        summary = str(summary_artifact.content.get("text", ""))
        builder = TaskContextBuilder(
            repo,
            profile,
            summary=summary,
            outcome_store=self._outcomes,
            max_chars=self._max_context_chars,
        )
        return await builder.build(
            project_id,
            task,
            revision,
            dependency_task_ids=dependency_task_ids,
        )

    # -- protocol: staleness / outcomes ------------------------------------

    async def is_stale(self, artifact_id: str, current_revision: str) -> bool:
        """Evaluate staleness using the project's canonical repo handle."""
        artifact = await self._artifacts.get(artifact_id)
        repo = self._repo_lookup(artifact.project_id) if artifact is not None else None
        return await self._artifacts.is_stale(artifact_id, current_revision, repo=repo)

    async def record_task_outcome(
        self,
        project_id: str,
        task_id: str,
        outcome: TaskOutcome | TaskOutcomeNote,
    ) -> None:
        """Persist a structured outcome note for later tasks/reporting."""
        await self._outcomes.record(project_id, task_id, outcome)

    # -- helpers -----------------------------------------------------------

    def _require_repo(self, project_id: str) -> RepoHandle:
        repo = self._repo_lookup(project_id)
        if repo is None:
            raise ContextError(f"no repository registered for project {project_id}")
        return repo

    async def _profile(self, project_id: str, revision: str) -> RepoProfile:
        repo = self._require_repo(project_id)
        artifact = await self._artifacts.get_or_generate(ArtifactKind.REPO_PROFILE, repo, revision)
        return RepoProfile.model_validate(artifact.content)

    def _register_generators(self) -> None:
        self._artifacts.register_generator(ArtifactKind.REPO_PROFILE, self._generate_profile)
        self._artifacts.register_generator(ArtifactKind.REPO_STRUCTURE, self._generate_structure)
        self._artifacts.register_generator(ArtifactKind.CONVENTIONS, self._generate_conventions)
        self._artifacts.register_generator(
            ArtifactKind.BUILD_TEST_COMMANDS, self._generate_commands
        )
        self._artifacts.register_generator(ArtifactKind.PROJECT_SUMMARY, self._generate_summary)

    async def _generate_profile(self, project_id: str, revision: str) -> GeneratedArtifact:
        repo = self._require_repo(project_id)
        profile = await self._analyzer.analyze_repo(repo, revision)
        return GeneratedArtifact(
            content=profile.model_dump(mode="json"),
            depends_on_paths=["*"],
            generated_by="analyzer",
        )

    async def _generate_structure(self, project_id: str, revision: str) -> GeneratedArtifact:
        profile = await self._profile(project_id, revision)
        content = {
            "revision": revision,
            "file_count": profile.file_count,
            "total_loc_estimate": profile.total_loc_estimate,
            "languages": profile.languages,
            "language_share": profile.language_share,
            "tree_summary": profile.tree_summary,
        }
        return GeneratedArtifact(
            content=content,
            depends_on_paths=["*"],
            generated_by="analyzer",
        )

    async def _generate_conventions(self, project_id: str, revision: str) -> GeneratedArtifact:
        profile = await self._profile(project_id, revision)
        content = {
            "revision": revision,
            "conventions": profile.conventions,
            "test_layout": profile.test_layout,
            "environment_hints": profile.environment_hints,
            "docs_found": profile.docs_found,
        }
        return GeneratedArtifact(
            content=content,
            depends_on_paths=depends_for_kind(ArtifactKind.CONVENTIONS, profile),
            generated_by="analyzer",
        )

    async def _generate_commands(self, project_id: str, revision: str) -> GeneratedArtifact:
        profile = await self._profile(project_id, revision)
        content = {
            "revision": revision,
            "build_commands": [
                item.model_dump(mode="json") for item in profile.build_command_specs
            ],
            "test_commands": [item.model_dump(mode="json") for item in profile.test_command_specs],
            "lint_commands": [item.model_dump(mode="json") for item in profile.lint_command_specs],
            "spec_commands": [item.model_dump(mode="json") for item in profile.spec_commands],
            "effective_test_commands": profile.effective_test_commands,
        }
        return GeneratedArtifact(
            content=content,
            depends_on_paths=depends_for_kind(ArtifactKind.BUILD_TEST_COMMANDS, profile),
            generated_by="analyzer",
        )

    async def _generate_summary(self, project_id: str, revision: str) -> GeneratedArtifact:
        profile = await self._profile(project_id, revision)
        text = await self._summaries.summarize(profile, profile.readme_excerpt)
        return GeneratedArtifact(
            content={
                "revision": revision,
                "text": text,
                "generated_by": self._summaries.last_generated_by,
            },
            depends_on_paths=depends_for_kind(ArtifactKind.PROJECT_SUMMARY, profile),
            generated_by=self._summaries.last_generated_by,
        )


__all__ = ["ContextManagerImpl"]
