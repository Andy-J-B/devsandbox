"""Shared helpers for context manager / analyzer tests (plan 05)."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devsandbox.common.types import RepoHandle, TaskSpec
from devsandbox.db.models import ContextArtifactModel

FIXTURE_REPOS = Path(__file__).parent / "fixtures" / "repos"


@dataclass(frozen=True)
class FixtureRepo:
    """A fixture copied into a temp dir and cloned as a bare canonical repo."""

    name: str
    source: Path
    bare: Path
    handle: RepoHandle
    revision: str


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def build_fixture_repo(tmp_path: Path, name: str) -> FixtureRepo:
    """Copy ``tests/fixtures/repos/<name>`` and build source + bare repos."""
    fixture = FIXTURE_REPOS / name
    source = tmp_path / "source" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        fixture,
        source,
        ignore=shutil.ignore_patterns(".gitkeep", "__pycache__", "*.pyc", "*CANARY_EXECUTED"),
    )
    has_files = any(source.iterdir())
    _git(source, "init", "-b", "main")
    _git(source, "add", "-A")
    if not has_files:
        _git(source, "commit", "--allow-empty", "-m", "initial")
    else:
        _git(source, "commit", "-m", "initial")

    bare = tmp_path / "data" / "repos" / name / "repo.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    revision = _git(bare, "rev-parse", "main")
    handle = RepoHandle(
        project_id=name,
        path=str(bare),
        ref="main",
        main_branch="main",
        staging_branch=f"devsandbox/{name}/staging",
        base_commit=revision,
    )
    return FixtureRepo(
        name=name,
        source=source,
        bare=bare,
        handle=handle,
        revision=revision,
    )


def commit_file(fixture: FixtureRepo, path: str, content: str, message: str = "change") -> str:
    """Commit a changed/added file in the source repo and push it to the bare repo."""
    target = fixture.source / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(fixture.source, "add", "-A")
    _git(fixture.source, "commit", "-m", message)
    _git(fixture.source, "push", str(fixture.bare), "main")
    return _git(fixture.bare, "rev-parse", "main")


def delete_file(fixture: FixtureRepo, path: str, message: str = "delete") -> str:
    """Delete a file in the source repo and push it to the bare repo."""
    _git(fixture.source, "rm", path)
    _git(fixture.source, "commit", "-m", message)
    _git(fixture.source, "push", str(fixture.bare), "main")
    return _git(fixture.bare, "rev-parse", "main")


def make_task(
    task_id: str = "task-1",
    *,
    expected_files: list[str] | None = None,
    instructions: str = "Make the change.",
) -> TaskSpec:
    """Return a task spec suitable for context-bundle tests."""
    return TaskSpec(
        id=task_id,
        title="Implement the feature",
        description="Add the requested behaviour.",
        instructions=instructions,
        success_criteria=["tests pass"],
        expected_files=expected_files or [],
    )


class InMemoryArtifactRepository:
    """Minimal in-memory implementation of the artifact repository protocol."""

    def __init__(self) -> None:
        self.rows: list[ContextArtifactModel] = []

    async def create(self, artifact: ContextArtifactModel) -> ContextArtifactModel:
        self.rows.append(artifact)
        return artifact

    async def get(self, artifact_id: str) -> ContextArtifactModel | None:
        return next((row for row in self.rows if row.id == artifact_id), None)

    async def list_for_project(
        self, project_id: str, kind: str | None = None
    ) -> list[ContextArtifactModel]:
        rows = [
            row
            for row in self.rows
            if row.project_id == project_id and (kind is None or row.kind == kind)
        ]
        return list(reversed(rows))

    async def latest_for_project(self, project_id: str, kind: str) -> ContextArtifactModel | None:
        rows = await self.list_for_project(project_id, kind)
        return rows[0] if rows else None

    async def mark_stale(self, artifact_id: str, stale: bool = True) -> None:
        row = await self.get(artifact_id)
        if row is not None:
            row.stale = stale


__all__ = [
    "FIXTURE_REPOS",
    "FixtureRepo",
    "InMemoryArtifactRepository",
    "build_fixture_repo",
    "commit_file",
    "delete_file",
    "make_task",
]
