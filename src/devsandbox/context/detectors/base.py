"""Detector plugin protocol and shared detection result types."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ...common.types import DetectedCommand


@dataclass(frozen=True)
class RepoFiles:
    """Deterministic, read-only view of the files needed by detectors."""

    paths: tuple[str, ...]
    contents: dict[str, str]

    def exists(self, path: str) -> bool:
        """Return True when ``path`` exists in the revision."""
        return path in self.contents or path in self.paths

    def glob(self, pattern: str) -> tuple[str, ...]:
        """Return matching paths (fnmatch semantics, sorted).

        A leading ``**/`` also matches root-level files, which makes patterns
        like ``**/requirements*.txt`` work for both flat and monorepo layouts.
        """
        patterns = [pattern]
        if pattern.startswith("**/"):
            patterns.append(pattern[3:])
        return tuple(
            path
            for path in self.paths
            if any(fnmatch.fnmatch(path, candidate) for candidate in patterns)
        )

    def read(self, path: str, max_chars: int = 200_000) -> str | None:
        """Return file text, or ``None`` when it was not preloaded."""
        text = self.contents.get(path)
        if text is None:
            return None
        return text[:max_chars]

    def find(self, *names: str) -> tuple[str, ...]:
        """Return root-level files whose basename is one of ``names``."""
        wanted = set(names)
        return tuple(path for path in self.paths if "/" not in path and path in wanted)

    def by_name(self, *names: str) -> tuple[str, ...]:
        """Return all files (at any depth) whose basename is one of ``names``."""
        wanted = set(names)
        return tuple(path for path in self.paths if path.rsplit("/", 1)[-1] in wanted)


@dataclass
class Detection:
    """One ecosystem detector's findings."""

    ecosystem: str
    package_managers: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    build_commands: list[DetectedCommand] = field(default_factory=list)
    test_commands: list[DetectedCommand] = field(default_factory=list)
    lint_commands: list[DetectedCommand] = field(default_factory=list)
    test_layout: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    environment_hints: dict[str, str] = field(default_factory=dict)
    key_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        """Return True when this ecosystem was actually detected."""
        return bool(self.manifests)


@runtime_checkable
class Detector(Protocol):
    """Small, independently testable ecosystem detector."""

    name: str

    def detect(self, files: RepoFiles) -> Detection: ...


__all__ = ["Detection", "Detector", "RepoFiles"]
