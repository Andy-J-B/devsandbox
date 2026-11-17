"""Hostile git workspaces must never execute code on the host.

Agents own their workspace, including ``.git``.  These tests use real git and
real ``GitManagerImpl`` calls to prove that config tricks, hooks, symlink
escapes, and submodule metadata are rejected before they can affect the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devsandbox.common.errors import GitChangeLimitError, GitError
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


async def _workspace(tmp_path: Path, name: str, *, max_blob_bytes: int | None = None):
    overrides = {}
    if max_blob_bytes is not None:
        overrides["git"] = {"max_blob_bytes": max_blob_bytes}
    settings = DevSandboxSettings(data_root=str(tmp_path / "data"), **overrides)
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
        # A boolean (no "=") is still a key: git reads it as true.
        ("[core]\n\tfsmonitor", "core.fsmonitor"),
        ("[core]\n\tsshCommand = /tmp/canary-hook.sh", "core.sshcommand"),
        ("[core]\n\tpager = /tmp/canary-hook.sh", "core.pager"),
        ("[core]\n\tworktree = /tmp/evil-worktree", "core.worktree"),
        ("[core]\n\tgitProxy = /tmp/canary-hook.sh", "core.gitproxy"),
        ("[core]\n\talternateRefsCommand = /tmp/canary-hook.sh", "core.alternaterefscommand"),
        ("[core]\n\tattributesFile = /tmp/evil-attributes", "core.attributesfile"),
        ("[Core]\n\tPager = /tmp/canary-hook.sh", "core.pager"),
        ('[includeIf "gitdir:**"]\n\tpath = /tmp/evil.gitconfig', "includeif"),
        # Deprecated [section.subsection] syntax must not hide the section.
        ("[include.path]\n\tvalue = /tmp/evil.gitconfig", "include"),
        ('[filter "evil"]\n\tsmudge = /tmp/canary-hook.sh', "filter"),
        ("[diff]\n\texternal = /tmp/canary-hook.sh", "diff.external"),
        ('[diff "evil"]\n\tcommand = /tmp/canary-hook.sh', "diff"),
        ("[credential]\n\thelper = /tmp/canary-hook.sh", "credential.helper"),
        ('[credential "https://evil.example"]\n\thelper = /tmp/canary-hook.sh', "credential"),
        ('[url "https://evil.example"]\n\tinsteadOf = https://', "url"),
        ('[url "https://evil.example"]\n\tpushInsteadOf = https://', "url"),
        ("[protocol]\n\tallow = always", "protocol"),
        ('[protocol "ext"]\n\tallow = always', "protocol"),
        ("[http]\n\tproxy = /tmp/canary-hook.sh", "http"),
        ('[http "https://evil.example"]\n\tslCert = /tmp/evil.pem', "http"),
        ('[remote "origin"]\n\tuploadpack = /tmp/canary-hook.sh', "remote"),
        ('[submodule "x"]\n\tupdate = !/tmp/canary-hook.sh', "submodule"),
        ("[pager]\n\tdiff = /tmp/canary-hook.sh", "pager"),
        ("[gpg]\n\tprogram = /tmp/canary-hook.sh", "gpg"),
        ("[alias]\n\tpwn = !/tmp/canary-hook.sh", "alias"),
        (
            "[extensions]\n\tworktreeConfig = true\n[core]\n\tpager = /tmp/canary-hook.sh",
            "extensions",
        ),
        ('[browser "x"]\n\tcmd = /tmp/canary-hook.sh', "browser"),
        ("[interactive]\n\tdiffFilter = /tmp/canary-hook.sh", "interactive"),
        # A command-like key in an otherwise benign section is rejected too.
        ("[user]\n\tprogram = /tmp/canary-hook.sh", "user.program"),
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


async def test_benign_git_config_is_accepted(tmp_path: Path) -> None:
    """The allowlist must not reject ordinary identity/branch configuration."""
    manager, workspace = await _workspace(tmp_path, "cfg-ok")
    _append_config(
        workspace,
        "[user]\n\tname = Agent\n\temail = agent@localhost\n"
        '[branch "main"]\n\tdescription = mainline\n',
    )
    (Path(workspace.path) / "b.txt").write_text("bbb\n", encoding="utf-8")
    await _git("add", "-A", cwd=workspace.path)
    await _git("commit", "-m", "agent change", cwd=workspace.path)

    changes = await manager.collect_changes(workspace)

    assert "b.txt" in changes.files_changed


async def test_symlinked_git_config_is_rejected(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "cfg-link")
    config = Path(workspace.path) / ".git" / "config"
    outside = tmp_path / "outside-config"
    config.replace(outside)
    config.symlink_to(outside)

    with pytest.raises(GitError, match="symlink"):
        await manager.collect_changes(workspace)


async def test_huge_git_config_is_rejected(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "cfg-huge")
    config = Path(workspace.path) / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n# " + "x" * (1 << 20) + "\n")

    with pytest.raises(GitError, match="larger than"):
        await manager.collect_changes(workspace)


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


# ---------------------------------------------------------------------------
# Oversized blobs are rejected at collect time, not just by the diff-line cap
# ---------------------------------------------------------------------------


async def _commit_bytes(repo: Path, rel: str, content: bytes, message: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(content)
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", message, cwd=repo)


async def test_oversized_blob_is_rejected(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "big", max_blob_bytes=1024)
    repo = Path(workspace.path)
    await _commit_bytes(repo, "huge.bin", b"x" * 2048, "add huge blob")

    with pytest.raises(GitChangeLimitError, match=r"huge\.bin"):
        await manager.collect_changes(workspace)


async def test_oversized_blob_in_intermediate_commit_is_rejected(tmp_path: Path) -> None:
    """A blob added then deleted still bloats the mirror and must be rejected."""
    manager, workspace = await _workspace(tmp_path, "big-deleted", max_blob_bytes=1024)
    repo = Path(workspace.path)
    await _commit_bytes(repo, "transient.bin", b"x" * 2048, "add huge blob")
    await _git("rm", "-q", "transient.bin", cwd=repo)
    await _git("commit", "-m", "delete huge blob", cwd=repo)

    with pytest.raises(GitChangeLimitError, match=r"transient\.bin"):
        await manager.collect_changes(workspace)


async def test_blob_at_the_limit_is_allowed(tmp_path: Path) -> None:
    manager, workspace = await _workspace(tmp_path, "big-ok", max_blob_bytes=1024)
    repo = Path(workspace.path)
    await _commit_bytes(repo, "exact.bin", b"x" * 1024, "add boundary blob")

    changes = await manager.collect_changes(workspace)

    assert "exact.bin" in changes.files_changed


async def test_preexisting_large_file_is_not_rejected(tmp_path: Path) -> None:
    """Only blobs the attempt introduces count against the cap."""
    settings = DevSandboxSettings(data_root=str(tmp_path / "data"), git={"max_blob_bytes": 64})
    manager = GitManagerImpl(settings, tmp_path / "data")
    source_dir = tmp_path / "upstream-legacy"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    await _commit_bytes(source_dir, "legacy.bin", b"x" * 2048, "add legacy blob")
    repo = await manager.init_project_repo("legacy", RepoSource(path=str(source_dir)))
    workspace = await manager.create_workspace(repo, "attempt-1", "main")
    ws_path = Path(workspace.path)
    (ws_path / "a.txt").write_text("changed\n", encoding="utf-8")
    await _git("add", "-A", cwd=ws_path)
    await _git("commit", "-m", "touch a.txt", cwd=ws_path)

    changes = await manager.collect_changes(workspace)

    assert "a.txt" in changes.files_changed


