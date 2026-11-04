"""Scratch workspaces for validation (plan 09).

Validation must never mutate the agent's attempt workspace.  Task validation
therefore copies the workspace into a disposable scratch directory; integration
validation checks out the candidate commit into a fresh clone of the canonical
bare repository.  Both paths are deterministic from the validation run id and
are safe to recreate after a crash.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..common.errors import ValidationError
from ..common.types import RepoHandle
from ..gitmgr.command import run_git
from .runner import safe_attempt_id


def make_writable(root: str | Path) -> None:
    """Make ``root`` writable by the container's uid 1000.

    Executable bits are preserved; only write bits are added.  Symlinks are not
    followed (the git manager already rejects escaping symlinks, but the
    validator must not create a new escape via ``chmod``).
    """
    root_path = Path(root)
    if not root_path.exists():
        return
    for current, dirs, files in os.walk(root_path, followlinks=False):
        current_path = Path(current)
        try:
            mode = current_path.stat().st_mode
            os.chmod(current_path, mode | 0o777)
        except OSError:
            pass
        for name in dirs:
            target = current_path / name
            if target.is_symlink():
                continue
            try:
                mode = target.stat().st_mode
                os.chmod(target, mode | 0o777)
            except OSError:
                pass
        for name in files:
            target = current_path / name
            if target.is_symlink():
                continue
            try:
                mode = target.stat().st_mode
                os.chmod(target, mode | 0o666)
            except OSError:
                pass


def scratch_workspace_path(data_root: str | Path, run_id: str) -> Path:
    """Return the deterministic scratch workspace path for ``run_id``."""
    return Path(data_root) / "validation" / safe_attempt_id(run_id) / "workspace"


def reset_scratch_workspace(data_root: str | Path, run_id: str) -> Path:
    """Create (or recreate) an empty scratch workspace directory."""
    path = scratch_workspace_path(data_root, run_id)
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def copy_workspace(source: str | Path, destination: str | Path) -> Path:
    """Copy an attempt workspace into a disposable scratch directory."""
    source_path = Path(source)
    dest_path = Path(destination)
    if not source_path.is_dir():
        raise ValidationError(f"cannot validate missing workspace: {source_path}")
    if dest_path.exists():
        shutil.rmtree(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, source_path, dest_path, symlinks=True)
    await asyncio.to_thread(make_writable, dest_path)
    return dest_path


def cleanup_scratch(data_root: str | Path, run_id: str) -> None:
    """Remove the scratch tree for ``run_id`` (best effort)."""
    path = scratch_workspace_path(data_root, run_id)
    shutil.rmtree(path.parent, ignore_errors=True)


@runtime_checkable
class RevisionCheckout(Protocol):
    """Materialise a revision of a project repo into ``dest``."""

    async def checkout(self, repo: RepoHandle, revision: str, dest: Path) -> None: ...


class GitRevisionCheckout:
    """Default revision checkout backed by the git CLI.

    A mirror clone captures *all* refs (including the git manager's
    ``refs/devsandbox/integration-candidate/*`` candidate refs), then a normal
    clone is made from the mirror so the validation container gets a
    self-contained working tree.  The mirror is removed immediately.
    """

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self._timeout = timeout_seconds

    async def checkout(self, repo: RepoHandle, revision: str, dest: Path) -> None:
        """Check out ``revision`` from ``repo.path`` into ``dest``."""
        repo_path = Path(repo.path)
        if not repo_path.exists():
            raise ValidationError(f"canonical repository not found: {repo_path}")
        if not revision.strip():
            raise ValidationError("cannot check out an empty revision")

        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        mirror = dest.parent / f"mirror-{uuid.uuid4().hex}.git"
        try:
            await run_git(
                ["clone", "--mirror", "--no-hardlinks", str(repo_path), str(mirror)],
                timeout=self._timeout,
            )
            await run_git(
                ["clone", "--local", "--no-hardlinks", str(mirror), str(dest)],
                timeout=self._timeout,
            )
            # Pull every ref from the mirror, including custom candidate refs
            # that a normal clone does not advertise.
            await run_git(
                ["fetch", str(mirror), "+refs/*:refs/remotes/ds-import/*"],
                cwd=dest,
                timeout=self._timeout,
            )
            await run_git(["checkout", "--detach", revision], cwd=dest, timeout=self._timeout)
            await run_git(["remote", "remove", "origin"], cwd=dest, timeout=self._timeout)
        finally:
            shutil.rmtree(mirror, ignore_errors=True)
        await asyncio.to_thread(make_writable, dest)


__all__ = [
    "GitRevisionCheckout",
    "RevisionCheckout",
    "cleanup_scratch",
    "copy_workspace",
    "make_writable",
    "reset_scratch_workspace",
    "scratch_workspace_path",
]
