"""Context manager / repository analysis (plan 05)."""

from __future__ import annotations

from .analyzer import RepoAnalyzer, analyze_repo
from .interfaces import ContextManager
from .manager import ContextManagerImpl
from .outcomes import TaskOutcomeStore, record_task_outcome
from .store import (
    ArtifactKind,
    ArtifactRepository,
    ArtifactStore,
    ContextArtifact,
    GeneratedArtifact,
    canonical_json,
    content_hash,
    depends_for_kind,
)
from .summary import (
    NullSummarizer,
    Summarizer,
    SummaryService,
    render_profile_summary,
)
from .task_context import TaskContextBuilder

__all__ = [
    "ArtifactKind",
    "ArtifactRepository",
    "ArtifactStore",
    "ContextArtifact",
    "ContextManager",
    "ContextManagerImpl",
    "GeneratedArtifact",
    "NullSummarizer",
    "RepoAnalyzer",
    "Summarizer",
    "SummaryService",
    "TaskContextBuilder",
    "TaskOutcomeStore",
    "analyze_repo",
    "canonical_json",
    "content_hash",
    "depends_for_kind",
    "record_task_outcome",
    "render_profile_summary",
]
