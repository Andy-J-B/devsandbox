"""Task context bundling with a deterministic hard size budget.

Priority order (highest first): task-specific content, completed dependency
outcomes, recent failure notes, conventions/commands, project summary, tree.
Secret-bearing files are never read and all rendered text passes through the
process redactor.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.redaction import redact
from ..common.time import utcnow
from ..common.types import (
    ContextBundle,
    DetectedCommand,
    RepoHandle,
    RepoProfile,
    TaskOutcomeNote,
    TaskSpec,
)
from .outcomes import TaskOutcomeStore
from .source import GitSnapshot, is_binary_path, is_ignored_path, is_secret_path
from .summary import render_profile_summary

DEFAULT_MAX_CHARS = 24_000
DEFAULT_MAX_FILE_CHARS = 6_000
DEFAULT_MAX_FILES = 12
DEFAULT_MAX_DIR_ENTRIES = 30


@dataclass(frozen=True)
class _Section:
    name: str
    text: str


class TaskContextBuilder:
    """Build a size-capped, secret-safe :class:`ContextBundle`."""

    def __init__(
        self,
        repo: RepoHandle,
        profile: RepoProfile,
        *,
        summary: str = "",
        outcome_store: TaskOutcomeStore | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
        max_files: int = DEFAULT_MAX_FILES,
        max_dir_entries: int = DEFAULT_MAX_DIR_ENTRIES,
    ) -> None:
        self._repo = repo
        self._profile = profile
        self._summary = summary
        self._outcome_store = outcome_store
        self._max_chars = max(0, max_chars)
        self._max_file_chars = max(0, max_file_chars)
        self._max_files = max(0, max_files)
        self._max_dir_entries = max(0, max_dir_entries)

    async def build(
        self,
        project_id: str,
        task: TaskSpec,
        revision: str,
        *,
        dependency_task_ids: list[str] | tuple[str, ...] | None = None,
        dependency_outcomes: list[TaskOutcomeNote] | None = None,
        failure_notes: list[TaskOutcomeNote] | None = None,
    ) -> ContextBundle:
        """Assemble and budget a context bundle for ``task``."""
        snapshot = await GitSnapshot.open(self._repo.path, revision)
        truncation: list[str] = []
        file_contents: dict[str, str] = {}
        task_text = await self._task_section(snapshot, task, file_contents, truncation)

        dependencies = dependency_outcomes
        if dependencies is None and self._outcome_store is not None:
            dependencies = await self._outcome_store.latest_for_tasks(
                project_id, list(dependency_task_ids or ())
            )
        failures = failure_notes
        if failures is None and self._outcome_store is not None:
            failures = await self._outcome_store.recent_failures(project_id, task.id)

        summary = self._summary or render_profile_summary(
            self._profile, self._profile.readme_excerpt
        )
        sections = [
            _Section("task", task_text),
            _Section("dependency outcomes", _notes_section(dependencies or [])),
            _Section("recent failures", _notes_section(failures or [])),
            _Section("conventions and commands", _conventions_section(self._profile)),
            _Section("project summary", summary),
            _Section("repository tree", "\n".join(self._profile.tree_summary)),
        ]

        rendered: list[str] = []
        remaining = self._max_chars
        for section in sections:
            if not section.text.strip():
                continue
            header = f"## {section.name}\n"
            separator = 2 if rendered else 0
            available = remaining - len(header) - separator
            if available <= 0:
                truncation.append(f"{section.name}: omitted (budget exhausted)")
                continue
            body = section.text
            if len(body) > available:
                omitted = len(body) - available
                body = _truncate_text(body, available)
                truncation.append(f"{section.name}: truncated ({omitted} chars omitted)")
            rendered.append(header + body)
            remaining -= len(header) + len(body) + separator

        instructions = str(redact("\n\n".join(rendered).strip()))
        redacted_contents = redact(file_contents)
        safe_contents = {
            str(key): str(value)
            for key, value in (
                redacted_contents.items()
                if isinstance(redacted_contents, dict)
                else file_contents.items()
            )
        }
        return ContextBundle(
            task_id=task.id,
            task=task,
            context_files=sorted(file_contents),
            instructions=instructions,
            revision=revision,
            built_at=utcnow(),
            summary=str(redact(summary)),
            conventions=list(self._profile.conventions),
            commands=_commands(self._profile),
            file_contents=safe_contents,
            truncation_report=truncation,
        )

    async def _task_section(
        self,
        snapshot: GitSnapshot,
        task: TaskSpec,
        file_contents: dict[str, str],
        truncation: list[str],
    ) -> str:
        lines: list[str] = [
            f"Task: {task.title}",
            f"Task id: {task.id}",
        ]
        if task.description:
            lines.append(f"Description:\n{task.description}")
        if task.instructions:
            lines.append(f"Instructions:\n{task.instructions}")
        if task.success_criteria:
            lines.append("Success criteria:")
            lines.extend(f"- {criterion}" for criterion in task.success_criteria)
        if task.restricted_files:
            lines.append("Restricted files:")
            lines.extend(f"- {path}" for path in task.restricted_files)

        expanded: list[str] = []
        for expected in task.expected_files:
            if any(char in expected for char in "*?["):
                expanded.extend(snapshot.glob(expected))
            else:
                expanded.append(expected)
        expected_paths = sorted(set(expanded))

        if expected_paths:
            lines.append("Expected files:")
            lines.extend(f"- {path}" for path in expected_paths)
            file_budget = max(0, self._max_chars // 2)
            used = 0
            included = 0
            for path in expected_paths:
                if included >= self._max_files:
                    truncation.append(f"expected files: omitted after {self._max_files} files")
                    break
                if is_secret_path(path):
                    truncation.append(f"expected files: skipped secret-bearing {path}")
                    continue
                if is_binary_path(path):
                    truncation.append(f"expected files: skipped binary {path}")
                    continue
                if not snapshot.exists(path):
                    lines.append(f"\n### {path}\n(not present at revision)")
                    continue
                text = await snapshot.read_text(path, max_chars=self._max_file_chars + 1)
                if text is None:
                    truncation.append(f"expected files: skipped unreadable {path}")
                    continue
                if len(text) > self._max_file_chars:
                    omitted = len(text) - self._max_file_chars
                    text = _truncate_text(text, self._max_file_chars)
                    truncation.append(f"expected file {path}: truncated ({omitted} chars omitted)")
                if used + len(text) > file_budget:
                    truncation.append(
                        f"expected file {path}: omitted (file-content budget exhausted)"
                    )
                    continue
                used += len(text)
                included += 1
                file_contents[path] = text
                lines.append(f"\n### {path}\n```\n{text}\n```")

        directories: list[str] = []
        for path in expected_paths:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent not in directories:
                directories.append(parent)
        if not directories:
            directories = [""]
        listing_lines: list[str] = []
        for directory in directories[:5]:
            entries = _directory_entries(snapshot, directory, self._max_dir_entries)
            if entries:
                listing_lines.append(f"Directory: {directory or '.'}")
                listing_lines.extend(f"- {entry}" for entry in entries)
        if listing_lines:
            lines.append("Relevant directory listings:")
            lines.extend(listing_lines)
        return "\n".join(lines)


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    marker = "\n[... truncated]"
    if max_chars <= len(marker):
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker


def _directory_entries(snapshot: GitSnapshot, directory: str, limit: int) -> list[str]:
    prefix = directory.rstrip("/") + "/" if directory else ""
    children: set[str] = set()
    for path in snapshot.files:
        if not path.startswith(prefix):
            continue
        if is_ignored_path(path):
            continue
        rest = path[len(prefix) :]
        if not rest:
            continue
        head, separator, _tail = rest.partition("/")
        if separator:
            children.add(head + "/")
        elif not is_secret_path(path) and not is_binary_path(path):
            children.add(head)
    return sorted(children)[:limit]


def _notes_section(notes: list[TaskOutcomeNote]) -> str:
    if not notes:
        return ""
    lines: list[str] = []
    for note in notes:
        lines.append(f"### Task {note.task_id} ({note.outcome.value})")
        if note.revision:
            lines.append(f"Revision: {note.revision}")
        if note.files_changed:
            lines.append("Files changed:")
            lines.extend(f"- {path}" for path in note.files_changed)
        if note.commit_ids:
            lines.append(f"Commits: {', '.join(note.commit_ids)}")
        if note.validation_summary:
            lines.append(f"Validation: {note.validation_summary}")
        if note.decisions:
            lines.append("Decisions (untrusted agent report):")
            lines.extend(f"- {decision}" for decision in note.decisions)
        if note.failure_learnings:
            lines.append("Failure learnings:")
            lines.extend(f"- {learning}" for learning in note.failure_learnings)
        if note.agent_summary:
            lines.append(f"Agent summary (untrusted): {note.agent_summary}")
    return "\n".join(lines)


def _commands(profile: RepoProfile) -> list[str]:
    specs: list[DetectedCommand] = [
        *profile.build_command_specs,
        *profile.test_command_specs,
        *profile.lint_command_specs,
    ]
    seen: list[str] = []
    for spec in specs:
        if spec.command not in seen:
            seen.append(spec.command)
    return sorted(seen)


def _conventions_section(profile: RepoProfile) -> str:
    lines: list[str] = []
    if profile.conventions:
        lines.append("Conventions:")
        lines.extend(f"- {item}" for item in profile.conventions)
    commands = _commands(profile)
    if commands:
        lines.append("Commands:")
        lines.extend(f"- {command}" for command in commands)
    if profile.test_layout:
        lines.append(f"Test layout: {', '.join(profile.test_layout)}")
    if profile.environment_hints:
        hints = ", ".join(f"{key}={value}" for key, value in profile.environment_hints.items())
        lines.append(f"Environment hints: {hints}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_DIR_ENTRIES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_CHARS",
    "TaskContextBuilder",
]
