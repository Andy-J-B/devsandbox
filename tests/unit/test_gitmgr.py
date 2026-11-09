"""Unit tests for :class:`GitManagerImpl` using real git in temp directories.

No network access is required; remote URLs use local ``file://`` or bare
repositories on disk.  These tests exercise the hardened ``run_git`` wrapper
and the full git manager lifecycle (init, workspace, collect, restricted
paths, two-phase integration, CAS, recovery, push guards).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from devsandbox.common.errors import GitError
from devsandbox.common.types import RepoHandle, RepoSource
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.gitmgr.command import GitCommandError, run_git
from devsandbox.gitmgr.interfaces import ChangeSet
from devsandbox.gitmgr.manager import GitManagerImpl


def _settings(data_root: Path) -> DevSandboxSettings:
    return DevSandboxSettings(data_root=str(data_root))


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def manager(data_root: Path) -> GitManagerImpl:
    return GitManagerImpl(_settings(data_root), data_root)


async def _git(*args: str, cwd: str | Path | None = None) -> str:
    """Run a git command (async helper for tests running in an event loop)."""
    return (await run_git(list(args), cwd=cwd)).stdout


async def _commit_file(repo: Path, rel: str, content: str, message: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        await _git("init", cwd=repo)
        await _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    await _git("add", "-A", cwd=repo)
    await _git("commit", "-m", message, cwd=repo)
    return (await _git("rev-parse", "HEAD", cwd=repo)).strip()


async def _init_project(manager: GitManagerImpl, project_id: str, source: RepoSource):
    return await manager.init_project_repo(project_id, source)


# ---------------------------------------------------------------------------
# run_git wrapper (command.py)
# ---------------------------------------------------------------------------


def test_run_git_raises_on_bad_ref() -> None:
    repo = Path(tempfile.mkdtemp())
    try:
        asyncio.run(_git("init", cwd=repo))
        with pytest.raises(GitCommandError):
            asyncio.run(
                run_git(
                    ["log", "--no-walk", "--format=%H", "nonexistent-ref"],
                    cwd=repo,
                    timeout=1,
                )
            )
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# init_project_repo
# ---------------------------------------------------------------------------


async def test_init_from_local_non_git_dir(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    source_dir.mkdir()
    (source_dir / "README.md").write_text("hello\n", encoding="utf-8")

    repo = await _init_project(manager, "proj-1", RepoSource(path=str(source_dir)))

    assert repo.main_branch == "main"
    assert repo.staging_branch == "devsandbox/proj-1/staging"
    assert Path(repo.path).exists()
    head = (await _git("rev-parse", "main", cwd=repo.path)).strip()
    assert head
    ls = await _git("ls-tree", "--name-only", "main", cwd=repo.path)
    assert "README.md" in ls
    # The original directory is not modified into a git repo.
    assert not (source_dir / ".git").exists()


async def test_init_from_local_git_dir_is_mirror(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    original_head = (await _git("rev-parse", "HEAD", cwd=source_dir)).strip()

    repo = await _init_project(manager, "proj-2", RepoSource(path=str(source_dir)))

    bare_head = (await _git("rev-parse", "main", cwd=repo.path)).strip()
    assert bare_head == original_head
    # Modifying the source must not affect the mirror.
    await _commit_file(source_dir, "b.txt", "bbb\n", "add b")
    assert (await _git("rev-parse", "main", cwd=repo.path)).strip() == original_head


async def test_init_from_file_url(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-3", RepoSource(url=f"file://{source_dir}"))
    assert (await _git("rev-parse", "main", cwd=repo.path)).strip()


async def test_init_idempotent(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo1 = await _init_project(manager, "proj-4", RepoSource(path=str(source_dir)))
    head1 = (await _git("rev-parse", "main", cwd=repo1.path)).strip()
    repo2 = await _init_project(manager, "proj-4", RepoSource(path=str(source_dir)))
    head2 = (await _git("rev-parse", "main", cwd=repo2.path)).strip()
    assert head1 == head2
    assert repo1.path == repo2.path


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


async def test_create_workspace_isolated_from_bare(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    base = (await _commit_file(source_dir, "a.txt", "aaa\n", "add a")).strip()
    repo = await _init_project(manager, "proj-5", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-1", "main")
    assert ws.path
    assert ws.base_commit == base
    assert ws.branch == "devsandbox/proj-5/attempt-1/a1"

    # A commit made inside the workspace must not appear in the bare repo.
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent change")
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", ws.branch],
        cwd=repo.path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0  # branch not in bare until integration


async def test_create_workspace_based_on_staging_head(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-6", RepoSource(path=str(source_dir)))

    # Integrate a first change to advance staging.
    ws1 = await manager.create_workspace(repo, "attempt-1", "main")
    await _commit_file(Path(ws1.path), "x.txt", "xxx\n", "first")
    cs1 = await manager.collect_changes(ws1)
    res1 = await manager.integrate_to_staging(repo, ws1, cs1)
    assert res1.status == "integrated"

    # A second workspace should be based on the new staging head.
    ws2 = await manager.create_workspace(repo, "attempt-2", "main")
    head = await manager.staging_head(repo)
    assert ws2.base_commit == head
    # Workspace sees the first committed file.
    assert (Path(ws2.path) / "x.txt").read_text(encoding="utf-8") == "xxx\n"


async def test_create_workspace_wipe_and_recreate(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-7", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-8", "main")
    (Path(ws.path) / "leftover.txt").write_text("junk\n", encoding="utf-8")
    ws2 = await manager.create_workspace(repo, "attempt-8", "main")
    assert not (Path(ws2.path) / "leftover.txt").exists()


# ---------------------------------------------------------------------------
# collect_changes
# ---------------------------------------------------------------------------


async def test_collect_committed_changes(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-9", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-10", "main")
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent added b")
    cs = await manager.collect_changes(ws)
    assert cs.head_commit is not None
    assert cs.head_commit != ws.base_commit
    assert "b.txt" in cs.files_changed
    assert cs.uncommitted_committed_by_orchestrator is False


async def test_collect_auto_commits_uncommitted_changes(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-10", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-11", "main")
    (Path(ws.path) / "c.txt").write_text("ccc\n", encoding="utf-8")  # uncommitted
    cs = await manager.collect_changes(ws)
    assert cs.uncommitted_committed_by_orchestrator is True
    assert "c.txt" in cs.files_changed
    assert cs.head_commit != ws.base_commit


async def test_collect_rejects_malicious_hook_not_executed(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    """A hook in an untrusted workspace is rejected and never executed on the host.

    Per 00 §8.2 and plan 03 §3, unexpected executable hooks in an untrusted
    workspace cause collect_changes to reject it; because run_git also forces
    core.hooksPath=/dev/null, the hook can never run on the host.
    """
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-11", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-12", "main")
    canary = Path(ws.path) / ".git" / "hooks" / "post-commit"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_text("#!/bin/sh\necho CANARY > /tmp/devsandbox-hook-canary\n", encoding="utf-8")
    canary.chmod(0o755)

    canary_file = Path("/tmp/devsandbox-hook-canary")
    if canary_file.exists():
        canary_file.unlink()

    # Make a committed change so collect_changes would run git commit internally.
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent change")

    with pytest.raises(GitError):
        await manager.collect_changes(ws)
    assert not canary_file.exists(), "workspace hook was executed on the host!"


async def test_collect_rejects_symlink_escape(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-13", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-14", "main")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (Path(ws.path) / "escape.sym").symlink_to(outside)
    await _git("add", "escape.sym", cwd=ws.path)
    await _git("commit", "-m", "add symlink", cwd=ws.path)

    with pytest.raises(GitError):
        await manager.collect_changes(ws)


async def test_collect_rejects_gitmodules(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-15", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-16", "main")
    (Path(ws.path) / ".gitmodules").write_text("[submodule]\n", encoding="utf-8")
    await _git("add", ".gitmodules", cwd=ws.path)
    await _git("commit", "-m", "add gitmodules", cwd=ws.path)

    with pytest.raises(GitError):
        await manager.collect_changes(ws)


# ---------------------------------------------------------------------------
# check_restricted_paths
# ---------------------------------------------------------------------------


def test_check_restricted_paths_detects_violations(manager: GitManagerImpl) -> None:
    cs = ChangeSet(
        base_commit="abc",
        head_commit="def",
        files_changed=[
            "src/main.py",
            ".git/config",
            ".github/workflows/ci.yml",
            "docs/readme.md",
        ],
    )
    violations = asyncio.run(manager.check_restricted_paths(cs, ["docs/**", "secrets/**"]))
    joined = "\n".join(violations)
    assert ".git/config" in joined
    assert ".github/workflows/ci.yml" in joined
    assert "docs/readme.md" in joined
    assert "src/main.py" not in joined


# ---------------------------------------------------------------------------
# integration (two-phase)
# ---------------------------------------------------------------------------


async def test_integration_clean(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-17", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-1", "main")
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent added b")
    cs = await manager.collect_changes(ws)
    res = await manager.integrate_to_staging(repo, ws, cs)
    assert res.status == "integrated"
    assert res.staging_commit is not None
    assert await manager.staging_head(repo) == res.staging_commit


async def test_integration_conflict(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "line1\nline2\nline3\n", "add a")
    repo = await _init_project(manager, "proj-18", RepoSource(path=str(source_dir)))

    # Both workspaces are based on the same original (staging head before any
    # integration), then each edits line2 differently.
    ws1 = await manager.create_workspace(repo, "attempt-1", "main")
    ws2 = await manager.create_workspace(repo, "attempt-2", "main")
    assert ws1.base_commit == ws2.base_commit

    (Path(ws1.path) / "a.txt").write_text("line1\nEDIT1\nline3\n", encoding="utf-8")
    await _git("add", "-A", cwd=ws1.path)
    await _git("commit", "-m", "task1", cwd=ws1.path)
    cs1 = await manager.collect_changes(ws1)
    r1 = await manager.integrate_to_staging(repo, ws1, cs1)
    assert r1.status == "integrated"

    (Path(ws2.path) / "a.txt").write_text("line1\nEDIT2\nline3\n", encoding="utf-8")
    await _git("add", "-A", cwd=ws2.path)
    await _git("commit", "-m", "task2", cwd=ws2.path)
    cs2 = await manager.collect_changes(ws2)
    r2 = await manager.integrate_to_staging(repo, ws2, cs2)
    assert r2.status == "conflict"
    assert r2.conflicting_files
    assert "a.txt" in r2.conflicting_files
    # Staging unchanged by the failed integration.
    assert await manager.staging_head(repo) == r1.staging_commit


async def test_sequential_dependent_tasks(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-19", RepoSource(path=str(source_dir)))

    for i in range(3):
        ws = await manager.create_workspace(repo, f"attempt-{i}", "main")
        await _commit_file(Path(ws.path), f"f{i}.txt", f"content-{i}\n", f"task {i}")
        cs = await manager.collect_changes(ws)
        res = await manager.integrate_to_staging(repo, ws, cs)
        assert res.status == "integrated"

    head = await manager.staging_head(repo)
    tree = await _git("ls-tree", "--name-only", "-r", head, cwd=repo.path)
    for i in range(3):
        assert f"f{i}.txt" in tree


async def test_cas_staging_moved_between_prepare_and_commit(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-20", RepoSource(path=str(source_dir)))

    # Prepare candidate 1 (staging does not advance yet).
    ws1 = await manager.create_workspace(repo, "attempt-1", "main")
    await _commit_file(Path(ws1.path), "b.txt", "bbb\n", "agent added b")
    cs1 = await manager.collect_changes(ws1)
    candidate = await manager.prepare_integration(repo, ws1, cs1)
    assert not candidate.conflicted
    staging_before = candidate.staging_before

    # Another actor integrates a different task, advancing staging.
    ws2 = await manager.create_workspace(repo, "attempt-2", "main")
    await _commit_file(Path(ws2.path), "c.txt", "ccc\n", "other task")
    cs2 = await manager.collect_changes(ws2)
    res2 = await manager.integrate_to_staging(repo, ws2, cs2)
    assert res2.status == "integrated"

    # Committing the stale candidate should be rejected (CAS mismatch).
    result = await manager.commit_integration(candidate)
    assert result.status == "rejected"
    assert await manager.staging_head(repo) == res2.staging_commit
    assert staging_before != res2.staging_commit


async def test_reset_staging_to_rollback(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    base_head = (await _commit_file(source_dir, "a.txt", "aaa\n", "add a")).strip()
    repo = await _init_project(manager, "proj-21", RepoSource(path=str(source_dir)))

    ws = await manager.create_workspace(repo, "attempt-1", "main")
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent added b")
    cs = await manager.collect_changes(ws)
    res = await manager.integrate_to_staging(repo, ws, cs)
    assert res.status == "integrated"
    assert await manager.staging_head(repo) != base_head

    # Roll back to the earlier base commit (an ancestor of current staging).
    await manager.reset_staging_to(repo, base_head)
    assert await manager.staging_head(repo) == base_head


# ---------------------------------------------------------------------------
# main ref protection
# ---------------------------------------------------------------------------


async def test_main_ref_never_changes(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    main_head = (await _commit_file(source_dir, "a.txt", "aaa\n", "add a")).strip()
    repo = await _init_project(manager, "proj-22", RepoSource(path=str(source_dir)))

    for i in range(4):
        ws = await manager.create_workspace(repo, f"attempt-{i}", "main")
        await _commit_file(Path(ws.path), f"f{i}.txt", f"c{i}\n", f"t{i}")
        cs = await manager.collect_changes(ws)
        await manager.integrate_to_staging(repo, ws, cs)

    assert (await _git("rev-parse", "main", cwd=repo.path)).strip() == main_head


# ---------------------------------------------------------------------------
# push guards
# ---------------------------------------------------------------------------


async def test_push_disabled_by_default(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-23", RepoSource(path=str(source_dir)))
    res = await manager.push_branch(repo, "devsandbox/proj-23/staging")
    assert res.pushed is False
    assert "disabled by policy" in res.detail


async def test_push_refuses_main() -> None:
    settings = DevSandboxSettings(
        data_root=str(Path(tempfile.mkdtemp())), security={"allow_push": True}
    )
    try:
        mgr = GitManagerImpl(settings, Path(settings.data_root))
        source_dir = Path(tempfile.mkdtemp())
        await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
        repo = await _init_project(mgr, "proj-24", RepoSource(path=str(source_dir)))
        res = await mgr.push_branch(repo, "main")
        assert res.pushed is False
    finally:
        shutil.rmtree(settings.data_root, ignore_errors=True)


async def test_push_allowed_to_local_remote(manager: GitManagerImpl, tmp_path: Path) -> None:
    settings = DevSandboxSettings(data_root=str(tmp_path / "data"), security={"allow_push": True})
    mgr = GitManagerImpl(settings, tmp_path / "data")
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(mgr, "proj-25", RepoSource(path=str(source_dir)))

    remote = tmp_path / "remote.git"
    await _git("init", "--bare", str(remote))
    repo = RepoHandle(
        project_id=repo.project_id,
        path=repo.path,
        url=str(remote),
        ref=repo.ref,
        main_branch=repo.main_branch,
        staging_branch=repo.staging_branch,
    )
    ws = await mgr.create_workspace(repo, "attempt-1", "main")
    await _commit_file(Path(ws.path), "b.txt", "bbb\n", "agent")
    cs = await mgr.collect_changes(ws)
    await mgr.integrate_to_staging(repo, ws, cs)
    res = await mgr.push_branch(repo, repo.staging_branch)
    assert res.pushed is True


# ---------------------------------------------------------------------------
# recovery / cleanup
# ---------------------------------------------------------------------------


async def test_remove_workspace(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-26", RepoSource(path=str(source_dir)))
    ws = await manager.create_workspace(repo, "attempt-1", "main")
    assert Path(ws.path).exists()
    await manager.remove_workspace(ws, keep_for_diagnostics=False)
    assert not Path(ws.path).exists()


async def test_remove_workspace_keeps_when_requested(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-27", RepoSource(path=str(source_dir)))
    ws = await manager.create_workspace(repo, "attempt-1", "main")
    await manager.remove_workspace(ws, keep_for_diagnostics=True)
    assert Path(ws.path).exists()


async def test_list_workspaces(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-28", RepoSource(path=str(source_dir)))
    await manager.create_workspace(repo, "attempt-1", "main")
    await manager.create_workspace(repo, "attempt-2", "main")
    workspaces = await manager.list_workspaces("proj-28")
    assert len(workspaces) == 2


async def test_recover_integration_aborts_interrupted(
    manager: GitManagerImpl, tmp_path: Path
) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-29", RepoSource(path=str(source_dir)))
    integration_dir = manager._integration_dir("proj-29")
    integration_dir.mkdir(parents=True, exist_ok=True)
    candidate = await manager.detect_interrupted_integration(repo)
    assert candidate is not None
    await manager.recover_integration(repo)
    assert not integration_dir.exists()


# ---------------------------------------------------------------------------
# staging head
# ---------------------------------------------------------------------------


async def test_staging_head_set_after_init(manager: GitManagerImpl, tmp_path: Path) -> None:
    source_dir = tmp_path / "upstream"
    await _commit_file(source_dir, "a.txt", "aaa\n", "add a")
    repo = await _init_project(manager, "proj-30", RepoSource(path=str(source_dir)))
    head = await manager.staging_head(repo)
    assert head is not None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(pytest.main([__file__, "-v"])))
