"""Unit tests for disposable validation workspaces (plan 09).

These tests use the real git CLI but never Docker or the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from devsandbox.common.errors import ValidationError
from devsandbox.common.types import RepoHandle
from devsandbox.gitmgr.command import run_git
from devsandbox.validation.workspace import (
    GitRevisionCheckout,
    cleanup_scratch,
    copy_workspace,
    make_writable,
    reset_scratch_workspace,
    scratch_workspace_path,
)


async def _git(*args: str, cwd: str | Path | None = None) -> str:
    return (await run_git(list(args), cwd=cwd)).stdout


async def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        await _git("init", cwd=repo)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", message, cwd=repo)
    return (await _git("rev-parse", "HEAD", cwd=repo)).strip()


def _repo(path: Path) -> RepoHandle:
    return RepoHandle(
        project_id="proj-1",
        path=str(path),
        main_branch="main",
        staging_branch="devsandbox/proj-1/staging",
    )


@pytest.mark.asyncio
async def test_checkout_materialises_a_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base = await _commit(source, "app.py", "base\n", "base")
    bare = tmp_path / "repo.git"
    await _git("clone", "--bare", str(source), str(bare))
    candidate = await _commit(source, "app.py", "candidate\n", "candidate")
    await _git(
        "push",
        str(bare),
        f"{candidate}:refs/devsandbox/integration-candidate/cand",
        cwd=source,
    )
    assert candidate != base

    dest = tmp_path / "checkout" / "workspace"
    await GitRevisionCheckout().checkout(_repo(bare), candidate, dest)

    assert (dest / "app.py").read_text(encoding="utf-8") == "candidate\n"
    assert (await _git("rev-parse", "HEAD", cwd=dest)).strip() == candidate
    assert (await _git("status", "--porcelain", cwd=dest)).strip() == ""
    assert (await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)).strip() == "HEAD"
    assert (await _git("remote", cwd=dest)).strip() == ""
    # The validation container must be able to write test artefacts.
    assert (dest / "app.py").stat().st_mode & 0o200


@pytest.mark.asyncio
async def test_checkout_replaces_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    head = await _commit(source, "app.py", "new\n", "new")
    bare = tmp_path / "repo.git"
    await _git("clone", "--bare", str(source), str(bare))

    dest = tmp_path / "workspace"
    dest.mkdir()
    (dest / "stale.txt").write_text("stale\n", encoding="utf-8")

    await GitRevisionCheckout().checkout(_repo(bare), head, dest)

    assert not (dest / "stale.txt").exists()
    assert (dest / "app.py").read_text(encoding="utf-8") == "new\n"


@pytest.mark.asyncio
async def test_checkout_rejects_missing_repo(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        await GitRevisionCheckout().checkout(_repo(tmp_path / "missing.git"), "abc", tmp_path / "w")


@pytest.mark.asyncio
async def test_checkout_rejects_empty_revision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    await _commit(source, "app.py", "x\n", "x")
    bare = tmp_path / "repo.git"
    await _git("clone", "--bare", str(source), str(bare))

    with pytest.raises(ValidationError):
        await GitRevisionCheckout().checkout(_repo(bare), "   ", tmp_path / "w")


@pytest.mark.asyncio
async def test_copy_workspace_is_isolated_and_writable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (source / "pkg" / "app.py").chmod(0o400)
    (source / "link").symlink_to("pkg/app.py")

    destination = tmp_path / "scratch" / "workspace"
    destination.mkdir(parents=True)
    (destination / "stale.txt").write_text("stale\n", encoding="utf-8")

    result = await copy_workspace(source, destination)

    assert result == destination
    assert (destination / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (destination / "pkg" / "app.py").stat().st_mode & 0o200
    assert (destination / "link").is_symlink()
    assert not (destination / "stale.txt").exists()
    # The source workspace is untouched.
    assert not (source / "pkg" / "app.py").stat().st_mode & 0o200


@pytest.mark.asyncio
async def test_copy_workspace_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        await copy_workspace(tmp_path / "missing", tmp_path / "dest")


def test_reset_and_cleanup_scratch_workspace(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    first = reset_scratch_workspace(data_root, "task/attempt 1")
    (first / "old.txt").write_text("old\n", encoding="utf-8")

    second = reset_scratch_workspace(data_root, "task/attempt 1")

    assert first == second
    assert second.is_dir()
    assert not (second / "old.txt").exists()
    assert "task-attempt-1" in str(second)

    cleanup_scratch(data_root, "task/attempt 1")
    assert not scratch_workspace_path(data_root, "task/attempt 1").exists()


def test_make_writable_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside\n", encoding="utf-8")
    target.chmod(0o400)
    (root / "link").symlink_to(target)

    make_writable(root)

    assert target.stat().st_mode & 0o200 == 0
    assert root.stat().st_mode & 0o200


def test_make_writable_ignores_missing_root(tmp_path: Path) -> None:
    make_writable(tmp_path / "missing")  # must not raise


def test_scratch_path_is_deterministic(tmp_path: Path) -> None:
    assert scratch_workspace_path(tmp_path, "a/b") == scratch_workspace_path(tmp_path, "a-b")
    assert scratch_workspace_path(tmp_path, "...").name == "workspace"
    assert os.path.basename(scratch_workspace_path(tmp_path, "...").parent) == "validation"
