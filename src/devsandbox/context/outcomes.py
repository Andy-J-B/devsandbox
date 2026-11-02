"""Task outcome notes used by later tasks and the final report.

Outcome content is stored as ``outcome_notes`` context artifacts, preserving
history per task.  Agent-provided decisions/summaries are explicitly marked
untrusted; only independent validation is authoritative.
"""

from __future__ import annotations

from ..common.time import utcnow
from ..common.types import TaskOutcome, TaskOutcomeNote
from .store import ArtifactKind, ArtifactStore, ContextArtifact

_KIND = ArtifactKind.OUTCOME_NOTES


class TaskOutcomeStore:
    """Record and retrieve structured task outcome notes."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self._artifacts = artifacts

    async def record(
        self, project_id: str, task_id: str, outcome: TaskOutcome | TaskOutcomeNote
    ) -> TaskOutcomeNote:
        """Persist an outcome note (forcing the untrusted marker)."""
        if isinstance(outcome, TaskOutcomeNote):
            note = outcome
        else:
            note = TaskOutcomeNote(task_id=task_id, outcome=outcome)
        note = note.model_copy(
            update={
                "task_id": task_id,
                "untrusted": True,
                "recorded_at": note.recorded_at or utcnow(),
            }
        )
        await self._artifacts.put(
            _KIND,
            project_id,
            note.revision,
            note.model_dump(mode="json"),
            depends_on_paths=["*"],
            generated_by="analyzer",
        )
        return note

    async def latest_for_task(self, project_id: str, task_id: str) -> TaskOutcomeNote | None:
        """Return the newest note for one task."""
        artifacts = await self._artifacts.history(project_id, _KIND)
        for artifact in artifacts:
            note = _decode(artifact)
            if note is not None and note.task_id == task_id:
                return note
        return None

    async def latest_for_tasks(
        self, project_id: str, task_ids: list[str] | tuple[str, ...]
    ) -> list[TaskOutcomeNote]:
        """Return the newest note for each requested task (deterministic order)."""
        notes: list[TaskOutcomeNote] = []
        for task_id in sorted(set(task_ids)):
            note = await self.latest_for_task(project_id, task_id)
            if note is not None:
                notes.append(note)
        return notes

    async def recent_failures(
        self, project_id: str, task_id: str, limit: int = 3
    ) -> list[TaskOutcomeNote]:
        """Return recent failure/blocked notes for a task (oldest first)."""
        artifacts = await self._artifacts.history(project_id, _KIND)
        failures: list[TaskOutcomeNote] = []
        for artifact in artifacts:
            note = _decode(artifact)
            if note is None or note.task_id != task_id:
                continue
            if note.outcome in (TaskOutcome.FAILURE, TaskOutcome.BLOCKED):
                failures.append(note)
            if len(failures) >= limit:
                break
        return list(reversed(failures))


def _decode(artifact: ContextArtifact) -> TaskOutcomeNote | None:
    try:
        return TaskOutcomeNote.model_validate(artifact.content)
    except ValueError:
        return None


async def record_task_outcome(
    store: TaskOutcomeStore,
    project_id: str,
    task_id: str,
    outcome: TaskOutcome | TaskOutcomeNote,
) -> TaskOutcomeNote:
    """Convenience wrapper for :meth:`TaskOutcomeStore.record`."""
    return await store.record(project_id, task_id, outcome)


__all__ = ["TaskOutcomeStore", "record_task_outcome"]
