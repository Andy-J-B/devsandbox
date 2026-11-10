"""Hostile git workspaces must never execute code on the host.

Agents own their workspace, including ``.git``.  These tests use real git and
real ``GitManagerImpl`` calls to prove that config tricks, hooks, symlink
escapes, and submodule metadata are rejected before they can affect the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devsandbox.common.errors import GitError
from devsandbox.common.types import RepoSource
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.gitmgr.command import run_git
from devsandbox.gitmgr.manager import GitManagerImpl

pytestmark = pytest.mark.security


async def _git(*args: str, cwd: str | Path) -> str:
    return (await run_git(list(args), cwd=cwd)).stdout


async def _commit_file(repo: Path, rel: str, content: str, message: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        await _git("init", "-b", "main", cwd=repo)
        await _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", message, cwd=repo)
    return (await _git("rev-parse", "HEAD", cwd=repo)).strip()


async def _workspace(tmp_path: Path, name: str):
    settings = DevSandboxSettings(data_root=str(tmp_path / "data"))
    manager = GitManagerImpl(settings, tmp_path / "data")
    source_dir = tmp_path / f"upstream-{name}"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await manager.init_project_repo(name, RepoSource(path=str(source_dir)))
    workspace = await manager.create_workspace(repo, "attempt-1", "main")
    return manager, workspace


def _append_config(workspace, snippet: str) -> None:
    config = Path(workspace.path) / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n" + snippet + "\n")


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("[core]\n\tfsmonitor = /tmp/canary-hook.sh", "core.fsmonitor"),
        ("[core]\n\tsshCommand = /tmp/canary-hook.sh", "core.sshcommand"),
        ('[includeIf "gitdir:**"]\n\tpath = /tmp/evil.gitconfig', "includeif"),
        ('[filter "evil"]\n\tsmudge = /tmp/canary-hook.sh', "filter"),
        ("[diff]\n\texternal = /tmp/canary-hook.sh", "diff.external"),
        ("[credential]\n\thelper = /tmp/canary-hook.sh", "credential.helper"),
    ],
)
async def test_hostile_git_config_is_rejected(tmp_path: Path, snippet: str, expected: str) -> None:
    manager, workspace = await _workspace(tmp_path, "cfg")
    canary = tmp_path / "canary-executed"
    script = tmp_path / "canary-hook.sh"
    script.write_text(f"#!/bin/sh\ntouch {canary}\n", encoding="utf-8")
    script.chmod(0o755)
    _append_config(workspace, snippet.replace("/tmp/canary-hook.sh", str(script)))

    with pytest.raises(GitError, match=expected):
        await manager.collect_changes(workspace)

    assert not canary.exists(), "hostile git config executed host code"


async def test_executable_hook_is_rejected_and_never_runs(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "hook")
    canary = tmp_path / "hook-canary"
    hook = Path(workspace.path) / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\n", encoding="utf-8")
    hook.chmod(0o755)

    with pytest.raises(GitError, match="hook"):
        await manager.collect_changes(workspace)

    assert not canary.exists(), "workspace hook executed on the host"


async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "symlink")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host-only\n", encoding="utf-8")
    (Path(workspace.path) / "escape.sym").symlink_to(outside)
    await _git("add", "escape.sym", cwd=workspace.path)
    await _git("commit", "-m", "add symlink", cwd=workspace.path)

    with pytest.raises(GitError, match="symlink escapes"):
        await manager.collect_changes(workspace)


async def test_submodule_metadata_is_rejected(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "submodule")
    (Path(workspace.path) / ".gitmodules").write_text(
        '[submodule "x"]\n\tpath = x\n\turl = file:///tmp/x\n', encoding="utf-8"
    )
    await _git("add", ".gitmodules", cwd=workspace.path)
    await _git("commit", "-m", "add submodule metadata", cwd=workspace.path)

    with pytest.raises(GitError, match="protected path"):
        await manager.collect_changes(workspace)


async def test_normal_workspace_still_collects(tmp_path: Path) -> None:
    """The hardening checks must not reject ordinary repositories."""
    manager, workspace = await _workspace(tmp_path, "normal")
    (Path(workspace.path) / "b.txt").write_text("bbb\n", encoding="utf-8")
    await _git("add", "-A", cwd=workspace.path)
    await _git("commit", "-m", "agent change", cwd=workspace.path)

    changes = await manager.collect_changes(workspace)

    assert "b.txt" in changes.files_changed
    assert changes.head_commit
