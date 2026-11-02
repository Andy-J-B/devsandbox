"""Versioned, revision-stamped context artifact storage.

Artifacts are persisted in the ``context_artifacts`` table.  Old revisions are
never deleted: regeneration marks the previous row ``stale=True`` and inserts a
new row, so history and audit remain intact.

Staleness rules (also documented in ``docs/context.md``):

* command artifacts (``build_test_commands``) depend on exact manifest/test
  config paths; a change to any of those paths invalidates them,
* structural artifacts (``repo_profile``/``repo_structure``) invalidate on any
  added/deleted path (a modification alone leaves them fresh),
* summary/convention artifacts depend on their docs/config paths,
* task context is rebuilt for every task and is not cached.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..common.errors import ContextError
from ..common.time import utcnow
from ..common.types import RepoHandle, RepoProfile
from ..db.models import ContextArtifactModel
from .source import diff_revisions


class ArtifactKind(StrEnum):
    """Kinds of context artifacts tracked for a project."""

    REPO_PROFILE = "repo_profile"
    PROJECT_SUMMARY = "project_summary"
    REPO_STRUCTURE = "repo_structure"
    CONVENTIONS = "conventions"
    BUILD_TEST_COMMANDS = "build_test_commands"
    TASK_CONTEXT = "task_context"
    OUTCOME_NOTES = "outcome_notes"


_STRUCTURAL_KINDS: frozenset[ArtifactKind] = frozenset(
    {ArtifactKind.REPO_PROFILE, ArtifactKind.REPO_STRUCTURE}
)


def canonical_json(content: Any) -> str:
    """Return canonical JSON used for deterministic content hashing."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(content: Any) -> str:
    """Return the SHA-256 hash of canonical JSON ``content``."""
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


class ContextArtifact(BaseModel):
    """A decoded artifact row."""

    model_config = ConfigDict(frozen=True)

    id: str
    project_id: str
    kind: ArtifactKind
    revision: str
    content: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = ""
    generated_by: str = "analyzer"
    depends_on_paths: list[str] = Field(default_factory=list)
    stale: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, row: ContextArtifactModel) -> ContextArtifact:
        """Build an artifact from a SQLAlchemy row."""
        return cls(
            id=row.id,
            project_id=row.project_id,
            kind=ArtifactKind(row.kind),
            revision=row.revision,
            content=dict(row.content_json or {}),
            content_hash=row.content_hash or "",
            generated_by=row.generated_by or "analyzer",
            depends_on_paths=list(row.depends_on_paths_json or []),
            stale=bool(row.stale),
            created_at=row.created_at,
        )


@runtime_checkable
class ArtifactRepository(Protocol):
    """Persistence operations required by :class:`ArtifactStore`."""

    async def create(self, artifact: ContextArtifactModel) -> ContextArtifactModel: ...

    async def get(self, artifact_id: str) -> ContextArtifactModel | None: ...

    async def list_for_project(
        self, project_id: str, kind: str | None = None
    ) -> list[ContextArtifactModel]: ...

    async def latest_for_project(
        self, project_id: str, kind: str
    ) -> ContextArtifactModel | None: ...

    async def mark_stale(self, artifact_id: str, stale: bool = True) -> None: ...


@dataclass(frozen=True)
class GeneratedArtifact:
    """Content/provenance returned by an artifact generator."""

    content: dict[str, Any]
    depends_on_paths: list[str] = field(default_factory=list)
    generated_by: str = "analyzer"


RepoLookup = Callable[[str], RepoHandle | None]
ArtifactGenerator = Callable[[str, str], Awaitable[GeneratedArtifact]]


def depends_for_kind(kind: ArtifactKind, profile: RepoProfile) -> list[str]:
    """Return the default dependency paths for an artifact kind."""
    if kind is ArtifactKind.BUILD_TEST_COMMANDS:
        return sorted(set(profile.manifests) | set(profile.ci_files))
    if kind is ArtifactKind.PROJECT_SUMMARY:
        return sorted(set(profile.docs_found) | set(profile.manifests))
    if kind is ArtifactKind.CONVENTIONS:
        return sorted(set(profile.docs_found) | set(profile.manifests))
    return ["*"]


class ArtifactStore:
    """Persist and regenerate revision-stamped context artifacts."""

    def __init__(
        self,
        repository: ArtifactRepository,
        repo_lookup: RepoLookup | None = None,
        generators: dict[ArtifactKind, ArtifactGenerator] | None = None,
    ) -> None:
        self._repository = repository
        self._repo_lookup = repo_lookup
        self._generators: dict[ArtifactKind, ArtifactGenerator] = dict(generators or {})

    def register_generator(self, kind: ArtifactKind, generator: ArtifactGenerator) -> None:
        """Register the generator used by ``get_or_generate``."""
        self._generators[kind] = generator

    async def put(
        self,
        kind: ArtifactKind,
        project_id: str,
        revision: str,
        content: dict[str, Any],
        *,
        depends_on_paths: list[str] | None = None,
        generated_by: str = "analyzer",
    ) -> ContextArtifact:
        """Insert a new artifact version (never overwrites history)."""
        now = utcnow()
        row = ContextArtifactModel(
            id=uuid.uuid4().hex,
            project_id=project_id,
            kind=kind.value,
            revision=revision,
            content_json=content,
            content_hash=content_hash(content),
            generated_by=generated_by,
            depends_on_paths_json=sorted(set(depends_on_paths or [])),
            stale=False,
            created_at=now,
            updated_at=now,
        )
        created = await self._repository.create(row)
        return ContextArtifact.from_model(created)

    async def get(self, artifact_id: str) -> ContextArtifact | None:
        """Return an artifact by id."""
        row = await self._repository.get(artifact_id)
        return ContextArtifact.from_model(row) if row is not None else None

    async def latest(self, project_id: str, kind: ArtifactKind) -> ContextArtifact | None:
        """Return the newest artifact of ``kind`` for a project."""
        row = await self._repository.latest_for_project(project_id, kind.value)
        return ContextArtifact.from_model(row) if row is not None else None

    async def history(
        self, project_id: str, kind: ArtifactKind | None = None
    ) -> list[ContextArtifact]:
        """Return artifact history (newest first)."""
        rows = await self._repository.list_for_project(
            project_id, kind.value if kind is not None else None
        )
        return [ContextArtifact.from_model(row) for row in rows]

    async def is_stale(
        self,
        artifact_id: str,
        current_revision: str,
        repo: RepoHandle | None = None,
    ) -> bool:
        """Return True when ``artifact_id`` must be regenerated."""
        artifact = await self.get(artifact_id)
        if artifact is None or artifact.stale:
            return True
        if artifact.revision == current_revision:
            return False
        repo_handle = repo or self._lookup(artifact.project_id)
        if repo_handle is None:
            return True
        diff = await diff_revisions(repo_handle.path, artifact.revision, current_revision)
        depends = artifact.depends_on_paths
        if not depends:
            return True
        if "*" in depends or artifact.kind in _STRUCTURAL_KINDS:
            return bool(diff.added_or_deleted)
        changed = set(diff.changed)
        return any(path in changed for path in depends)

    async def get_or_generate(
        self,
        kind: ArtifactKind,
        project: RepoHandle | str,
        revision: str,
    ) -> ContextArtifact:
        """Return a fresh artifact, regenerating and marking old versions stale."""
        project_id = project.project_id if isinstance(project, RepoHandle) else str(project)
        repo = project if isinstance(project, RepoHandle) else self._lookup(project_id)
        latest = await self.latest(project_id, kind)
        if latest is not None and not await self.is_stale(latest.id, revision, repo=repo):
            return latest
        if latest is not None:
            await self._repository.mark_stale(latest.id, True)

        generator = self._generators.get(kind)
        if generator is None:
            raise ContextError(f"no generator registered for artifact kind {kind.value}")
        generated = await generator(project_id, revision)
        return await self.put(
            kind,
            project_id,
            revision,
            generated.content,
            depends_on_paths=generated.depends_on_paths,
            generated_by=generated.generated_by,
        )

    def _lookup(self, project_id: str) -> RepoHandle | None:
        if self._repo_lookup is None:
            return None
        return self._repo_lookup(project_id)


__all__ = [
    "ArtifactGenerator",
    "ArtifactKind",
    "ArtifactRepository",
    "ArtifactStore",
    "ContextArtifact",
    "GeneratedArtifact",
    "canonical_json",
    "content_hash",
    "depends_for_kind",
]
