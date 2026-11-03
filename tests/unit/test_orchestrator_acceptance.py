"""Acceptance guard: task status is only written through TaskRepo.transition."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "devsandbox"
TASK_REPO = SRC / "db" / "repositories" / "task_repo.py"

# Direct ORM/bulk writes to an existing task's status column.
_DIRECT_WRITE_PATTERNS = [
    re.compile(r"\btask\.status\s*="),
    re.compile(r"\bupdate\(\s*TaskModel\s*\)"),
    re.compile(r"\.values\([^)]*\bstatus\s*="),
]


def _source_files() -> list[Path]:
    return [path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts]


def test_no_task_status_writes_outside_task_repo() -> None:
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _DIRECT_WRITE_PATTERNS:
            if pattern.search(text) and path != TASK_REPO:
                offenders.append(f"{path.relative_to(SRC)} matches {pattern.pattern}")
    assert offenders == [], (
        f"task status must only be written via TaskRepo.transition; offenders: {offenders}"
    )


def test_task_repo_transition_is_the_single_write_path() -> None:
    text = TASK_REPO.read_text(encoding="utf-8")
    assert "async def transition(" in text
    assert "update(TaskModel)" in text
    assert text.count("task.status = to_state.value") == 1


def test_orchestrator_uses_transition_for_every_status_change() -> None:
    engines = [
        SRC / "orchestrator" / "engine.py",
        SRC / "orchestrator" / "recovery.py",
        SRC / "orchestrator" / "project_service.py",
    ]
    for path in engines:
        text = path.read_text(encoding="utf-8")
        assert "TaskRepo(" in text, path
        assert ".transition(" in text, path
