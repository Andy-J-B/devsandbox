"""Deterministic project summaries with an optional, budgeted LLM hook.

The default :class:`NullSummarizer` never calls a model, so the whole context
pipeline works with zero LLM calls.  A configured summarizer is cached per
revision and constrained by a simple token/character budget.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol, runtime_checkable

from ..common.redaction import redact
from ..common.types import RepoProfile

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    marker = "\n[... truncated]"
    if max_chars <= len(marker):
        return text[:max_chars], True
    return text[: max_chars - len(marker)] + marker, True


def _clean_readme(excerpt: str, max_chars: int) -> str:
    text = excerpt.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    capped, _ = _cap(text, max_chars)
    return capped


def render_profile_summary(
    profile: RepoProfile,
    readme_excerpt: str = "",
    *,
    max_chars: int = 2_000,
) -> str:
    """Return a deterministic, size-capped project summary."""
    lines: list[str] = [f"Repository: {profile.name or 'unknown'}"]
    if profile.revision:
        lines.append(f"Revision: {profile.revision}")
    if profile.primary_language:
        share = profile.language_share.get(profile.primary_language)
        share_text = f" ({share:.0%} of source files)" if share is not None else ""
        lines.append(f"Primary language: {profile.primary_language}{share_text}")
    if profile.languages:
        lines.append(f"Languages: {', '.join(profile.languages)}")
    lines.append(f"Files: {profile.file_count} (~{profile.total_loc_estimate} LOC estimate)")
    if profile.frameworks:
        lines.append(f"Frameworks: {', '.join(profile.frameworks)}")
    if profile.package_managers:
        lines.append(f"Package managers: {', '.join(profile.package_managers)}")
    if profile.build_command_specs:
        commands = ", ".join(
            f"{item.command} ({item.confidence.value}, {item.source})"
            for item in profile.build_command_specs
        )
        lines.append(f"Build commands: {commands}")
    if profile.effective_test_commands or profile.test_command_specs:
        specs = profile.spec_commands if profile.spec_commands else profile.test_command_specs
        commands = ", ".join(
            f"{item.command} ({item.confidence.value}, {item.source})" for item in specs
        )
        lines.append(f"Test commands: {commands}")
    if profile.lint_command_specs:
        commands = ", ".join(
            f"{item.command} ({item.confidence.value}, {item.source})"
            for item in profile.lint_command_specs
        )
        lines.append(f"Lint commands: {commands}")
    if profile.test_layout:
        lines.append(f"Test layout: {', '.join(profile.test_layout)}")
    if profile.ci_files:
        lines.append(f"CI files: {', '.join(profile.ci_files)}")
    if profile.conventions:
        lines.append(f"Conventions: {', '.join(profile.conventions)}")
    if profile.environment_hints:
        hints = ", ".join(f"{key}={value}" for key, value in profile.environment_hints.items())
        lines.append(f"Environment hints: {hints}")
    if profile.warnings:
        lines.append(f"Warnings: {'; '.join(profile.warnings)}")
    readme = _clean_readme(readme_excerpt, max(0, max_chars // 2))
    if readme:
        lines.append("README excerpt:")
        lines.append(readme)
    text, _ = _cap(redact("\n".join(lines)), max_chars)
    return text


@runtime_checkable
class Summarizer(Protocol):
    """Optional LLM / external enrichment hook."""

    async def summarize(self, profile: RepoProfile, readme_excerpt: str) -> str | None: ...


class NullSummarizer:
    """Default summarizer: no model calls, deterministic fallback."""

    async def summarize(self, profile: RepoProfile, readme_excerpt: str) -> str | None:
        """Return ``None`` so the deterministic summary is used."""
        return None


class SummaryService:
    """Generate/cached project summaries with an optional token budget."""

    def __init__(
        self,
        summarizer: Summarizer | None = None,
        *,
        max_chars: int = 2_000,
        max_input_chars: int = 4_000,
        token_budget: int | None = None,
    ) -> None:
        self._summarizer = summarizer or NullSummarizer()
        self._max_chars = max_chars
        self._max_input_chars = max_input_chars
        self._token_budget = token_budget
        self._tokens_used = 0
        self._cache: dict[str, tuple[str, str]] = {}
        self.last_generated_by = "analyzer"

    @property
    def tokens_used(self) -> int:
        """Return the estimated tokens consumed by LLM summaries."""
        return self._tokens_used

    def _key(self, profile: RepoProfile, excerpt: str) -> str:
        payload = profile.model_dump_json() + "\0" + excerpt
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def summarize(self, profile: RepoProfile, readme_excerpt: str = "") -> str:
        """Return a summary, using the LLM hook only when configured and in budget."""
        excerpt = _clean_readme(readme_excerpt, self._max_input_chars)
        key = self._key(profile, excerpt)
        cached = self._cache.get(key)
        if cached is not None:
            self.last_generated_by = cached[1]
            return cached[0]

        if not isinstance(self._summarizer, NullSummarizer):
            estimated_input = max(1, len(excerpt) // 4)
            within_budget = (
                self._token_budget is None
                or self._tokens_used + estimated_input <= self._token_budget
            )
            if within_budget:
                result = await self._summarizer.summarize(profile, excerpt)
                if result:
                    text, _ = _cap(redact(result.strip()), self._max_chars)
                    self._tokens_used += estimated_input + max(1, len(text) // 4)
                    self._cache[key] = (text, "llm")
                    self.last_generated_by = "llm"
                    return text

        text = render_profile_summary(profile, excerpt, max_chars=self._max_chars)
        self._cache[key] = (text, "analyzer")
        self.last_generated_by = "analyzer"
        return text


__all__ = [
    "NullSummarizer",
    "Summarizer",
    "SummaryService",
    "render_profile_summary",
]
