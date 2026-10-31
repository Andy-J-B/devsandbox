"""Git manager implementation (plan 03).

Implements :class:`devsandbox.gitmgr.interfaces.GitManager` on top of the
hardened :func:`devsandbox.gitmgr.command.run_git` wrapper.

Layout on disk (00 §4):

    <data_root>/repos/<project_id>/repo.git        # bare canonical repo
    <data_root>/workspaces/<project_id>/<attempt_id>/  # per-attempt clone
    <data_root>/integration/<project_id>/          # scratch integration worktree

Security properties:

* every git subprocess runs with ``core.hooksPath=/dev/null`` so hooks inside
  untrusted workspaces are *never* executed on the host,
* a fixed orchestrator identity is used for all commits,
* the canonical bare repo is never exposed writably to agents (workspace is a
  ``--local --no-hardlinks`` clone with its remote removed),
* staging only advances through a compare-and-swap ``update-ref``; ``main`` is
  never touched,
* every subprocess has a timeout and redacted error output.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec

from ..common.errors import GitError
from ..common.ids import AttemptId
from ..common.types import RepoHandle, RepoSource
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings
from .command import GitCommandError, is_git_repo, run_git
from .interfaces import ChangeSet, CommitInfo, IntegrationResult, PushResult, Workspace

_ALWAYS_RESTRICTED: list[str] = [".git/**", ".github/workflows/**"]
_CONTAINER_UID = 1000
_BRANCH_PREFIX = "devsandbox"
_ATTEMPT_REF_PREFIX = "refs/devsandbox/attempts"
_INTEGRATION_REF_PREFIX = "refs/devsandbox/integration-candidate"


@dataclass(frozen=True)
class IntegrationCandidate:
    """A prepared (but not yet committed) staging integration.

    Produced by :meth:`prepare_integration` and consumed by
    :meth:`commit_integration` / :meth:`abort_integration`.
    """

    attempt_id: AttemptId
    repo: RepoHandle
    staging_before: str | None
    staging_after: str
    conflicting_files: list[str]
    conflicted: bool
    integration_ref: str  # ref in the bare repo holding the candidate commit


class GitManagerImpl:
    """Filesystem + git-CLI implementation of :class:`GitManager`."""

    def __init__(
        self,
        settings: DevSandboxSettings,
        data_root: str | Path,
    ) -> None:
        self._settings = settings
        self._policy = SecurityPolicy(settings)
        self._data_root = Path(data_root)
        self._repos_root = self._data_root / "repos"
        self._workspaces_root = self._data_root / "workspaces"
        self._integration_root = self._data_root / "integration"
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._cred_helper_paths: list[str] = []

    # -- protocol: init -----------------------------------------------------

    async def init_project_repo(self, project_id: str, source: RepoSource) -> RepoHandle:
        """Initialize (idempotently) the canonical bare repo for a project.

        Local path: clone --mirror if it is a git repo; otherwise init a bare
        repo and import the directory as an initial commit on ``main``.
        Remote URL: clone --mirror using credentials from the host environment
        (never embedded in the URL).
        """
        project_id = str(project_id)
        bare_path = self._bare_path(project_id)
        bare_path.parent.mkdir(parents=True, exist_ok=True)

        if bare_path.exists() and is_git_repo(bare_path):
            # Idempotent: ensure staging branch exists, then return handle.
            await self._ensure_staging_branch(bare_path, project_id, source.ref)
            return self._handle(project_id, bare_path, source)

        if source.path:
            await self._init_from_local(bare_path, Path(source.path), source.ref)
        elif source.url:
            await self._init_from_url(bare_path, source.url, source.ref)
        else:  # pragma: no cover - RepoSource validates exactly one source
            raise GitError(f"no repo source for project {project_id}")

        await self._ensure_staging_branch(bare_path, project_id, source.ref)
        return self._handle(project_id, bare_path, source)

    # -- protocol: workspace ----------------------------------------------

    async def create_workspace(
        self, repo: RepoHandle, attempt_id: AttemptId, base_ref: str
    ) -> Workspace:
        """Create an isolated per-attempt workspace clone.

        The workspace is based on the current staging head (so dependent tasks
        see earlier completed work), falling back to ``base_ref`` when staging
        does not exist yet. It is a ``--local --no-hardlinks`` clone of the
        bare repo with its remote removed, so the agent cannot write to the
        canonical object store.
        """
        attempt_id = AttemptId(attempt_id)
        project_id = repo.project_id
        ws_path = self._workspace_path(project_id, attempt_id)

        # Deterministic path: wipe and recreate if a prior (crashed) attempt
        # left it behind (00 §8.4 idempotency).
        if ws_path.exists():
            shutil.rmtree(ws_path)
        ws_path.mkdir(parents=True, exist_ok=True)

        base_commit = await self._resolve_base_commit(repo, base_ref)
        if base_commit is None:
            raise GitError(f"cannot resolve base commit for project {project_id} (ref {base_ref})")

        # Clone from the bare repo (no hardlinks -> agent cannot corrupt the
        # canonical object store), then drop the remote so it cannot push/fetch.
        await run_git(["clone", "--local", "--no-hardlinks", repo.path, str(ws_path)])
        await run_git(["remote", "remove", "origin"], cwd=ws_path)

        # Create the per-attempt branch off the resolved base commit.
        branch = f"{_BRANCH_PREFIX}/{project_id}/{attempt_id}/a1"
        await run_git(["checkout", "-b", branch, base_commit], cwd=ws_path)

        self._make_container_writable(ws_path)

        return Workspace(
            attempt_id=attempt_id,
            path=str(ws_path),
            base_commit=base_commit,
            branch=branch,
        )

    # -- protocol: collect changes -----------------------------------------

    async def collect_changes(self, ws: Workspace) -> ChangeSet:
        """Collect and sanity-check changes from an untrusted workspace.

        The workspace content is treated as untrusted: ``.git`` integrity is
        verified, hooks are never executed (via ``core.hooksPath=/dev/null``),
        uncommitted changes are committed by the orchestrator, and dangerous
        changes (symlink escapes, ``.gitmodules`` edits) are rejected.
        """
        # 1. .git sanity (hooks are never executed because run_git sets
        #    core.hooksPath=/dev/null on every invocation).
        await run_git(["fsck", "--connectivity-only"], cwd=ws.path)
        head_res = await run_git(["rev-parse", "--verify", "HEAD"], cwd=ws.path)
        head_commit = head_res.stdout.strip()

        self._reject_dangerous_hooks(ws.path)

        # 2. Commit any uncommitted changes (orchestrator, not the agent).
        uncommitted_committed_by_orchestrator = False
        status = await run_git(["status", "--porcelain"], cwd=ws.path)
        if status.stdout.strip():
            await run_git(["add", "-A"], cwd=ws.path)
            await run_git(
                [
                    "commit",
                    "-m",
                    f"task {ws.attempt_id}: agent changes (committed by orchestrator)",
                ],
                cwd=ws.path,
            )
            uncommitted_committed_by_orchestrator = True
            head_res = await run_git(["rev-parse", "--verify", "HEAD"], cwd=ws.path)
            head_commit = head_res.stdout.strip()

        # 3. Build the change set against the base commit.
        base_ancestor = True
        if ws.base_commit:
            anc = await run_git(
                ["merge-base", "--is-ancestor", ws.base_commit, head_commit],
                cwd=ws.path,
            )
            base_ancestor = anc.returncode == 0

        commits: list[dict[str, str]] = []
        files_changed: list[str] = []
        diff_stat = ""

        if base_ancestor and ws.base_commit:
            log_res = await run_git(
                ["log", "--format=%H %s", f"{ws.base_commit}..HEAD"],
                cwd=ws.path,
            )
            for line in log_res.stdout.splitlines():
                line = line.strip()
                if line:
                    sha, _, message = line.partition(" ")
                    commits.append({"sha": sha, "message": message})
            diff_res = await run_git(
                ["diff", "--name-status", ws.base_commit, head_commit],
                cwd=ws.path,
            )
            files_changed = [
                line.split("\t", 1)[-1] for line in diff_res.stdout.splitlines() if line.strip()
            ]
            stat_res = await run_git(
                ["diff", "--stat", ws.base_commit, head_commit],
                cwd=ws.path,
            )
            diff_stat = stat_res.stdout.strip()
        else:
            # History rewritten or base missing: fall back to listing all files.
            ls_res = await run_git(["ls-files"], cwd=ws.path)
            files_changed = [line for line in ls_res.stdout.splitlines() if line.strip()]
            diff_stat = f"(history rewrite detected; base {ws.base_commit} not an ancestor of HEAD)"

        # 4. Dangerous-change detection (reject symlink escapes / .gitmodules).
        self._reject_dangerous_content(ws.path, files_changed)

        return ChangeSet(
            base_commit=ws.base_commit,
            head_commit=head_commit,
            commits=[CommitInfo(sha=c["sha"], message=c["message"]) for c in commits],
            files_changed=files_changed,
            diff_stat=diff_stat,
            uncommitted_committed_by_orchestrator=uncommitted_committed_by_orchestrator,
        )

    # -- protocol: restricted paths ----------------------------------------

    async def check_restricted_paths(self, cs: ChangeSet, restricted: list[str]) -> list[str]:
        """Return violation descriptions for changed files matching restricted globs.

        Always-restricted (regardless of spec): ``.git/**`` and
        ``.github/workflows/**``. Matching uses gitwildmatch semantics via
        :mod:`pathspec`.
        """
        patterns = list(restricted) + _ALWAYS_RESTRICTED
        spec = PathSpec.from_lines("gitignore", patterns)
        violations: list[str] = []
        for file_path in cs.files_changed:
            if spec.match_file(file_path):
                violations.append(f"restricted path touched: {file_path}")
        return violations

    # -- protocol: integration ---------------------------------------------

    async def integrate_to_staging(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationResult:
        """Convenience: prepare + commit a staging integration (CAS).

        For fine-grained control (e.g. plan 09 integration checks between
        prepare and commit) use :meth:`prepare_integration`,
        :meth:`commit_integration`, and :meth:`abort_integration` directly.
        """
        candidate = await self.prepare_integration(repo, ws, cs)
        if candidate.conflicted:
            return IntegrationResult(
                status="conflict",
                conflicting_files=list(candidate.conflicting_files),
                detail="merge conflict; staging unchanged",
            )
        return await self.commit_integration(candidate)

    async def prepare_integration(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationCandidate:
        """Prepare a staging merge in a scratch worktree without advancing refs.

        Runs under a per-project async lock. Fetches the attempt branch into
        the bare repo (validated with fsck), then merges it onto staging in a
        scratch worktree. Does not advance the staging ref.
        """
        project_id = repo.project_id
        lock = await self._lock_for(project_id)
        async with lock:
            return await self._prepare_integration_locked(repo, ws, cs)

    async def commit_integration(self, candidate: IntegrationCandidate) -> IntegrationResult:
        """Atomically advance staging to ``candidate.staging_after`` (CAS).

        Only succeeds if staging is still at ``candidate.staging_before``.
        Cleans up the candidate ref and scratch worktree.
        """
        repo = candidate.repo
        update_args: list[str] = [
            "update-ref",
            f"refs/heads/{repo.staging_branch}",
            candidate.staging_after,
        ]
        if candidate.staging_before is not None:
            update_args.append(candidate.staging_before)
        try:
            await run_git(update_args, cwd=repo.path)
        except GitCommandError as exc:
            return IntegrationResult(
                status="rejected",
                detail=f"staging moved during integration: {exc}",
            )
        finally:
            await self._drop_candidate_ref(repo, candidate.integration_ref)
            await self._remove_integration_dir(repo.project_id)
        return IntegrationResult(
            status="integrated",
            staging_commit=candidate.staging_after,
            detail="staging advanced via CAS update-ref",
        )

    async def abort_integration(self, candidate: IntegrationCandidate) -> None:
        """Abort a prepared integration: drop the candidate ref and worktree."""
        repo = candidate.repo
        if candidate.integration_ref:
            with contextlib.suppress(GitCommandError):
                await run_git(["update-ref", "-d", candidate.integration_ref], cwd=repo.path)
        await self._remove_integration_dir(repo.project_id)

    # -- protocol: staging head / reset ------------------------------------

    async def staging_head(self, repo: RepoHandle) -> str | None:
        """Return the current staging commit sha, or None if unset."""
        staging_branch = repo.staging_branch or f"{_BRANCH_PREFIX}/{repo.project_id}/staging"
        ref = f"refs/heads/{staging_branch}"
        try:
            res = await run_git(["rev-parse", "--verify", ref], cwd=repo.path)
        except GitCommandError:
            return None
        return res.stdout.strip() or None

    async def reset_staging_to(self, repo: RepoHandle, commit: str) -> None:
        """CAS-protected rollback of staging to a known earlier commit.

        Refuses to move staging forward or to a commit that is not an ancestor
        of the current staging head.
        """
        current = await self.staging_head(repo)
        # Verify the target commit exists in the bare repo.
        await run_git(["rev-parse", "--verify", commit], cwd=repo.path)
        if current is not None:
            # Only allow moving backward (target must be an ancestor of current).
            anc = await run_git(["merge-base", "--is-ancestor", commit, current], cwd=repo.path)
            if anc.returncode != 0:
                raise GitError(
                    f"refusing to reset staging {current} -> {commit}: target is not an ancestor"
                )
        staging_branch = repo.staging_branch or f"{_BRANCH_PREFIX}/{repo.project_id}/staging"
        ref = f"refs/heads/{staging_branch}"
        reset_args: list[str] = ["update-ref", ref, commit]
        if current is not None:
            reset_args.append(current)
        try:
            await run_git(reset_args, cwd=repo.path)
        except GitCommandError as exc:
            raise GitError(f"staging CAS reset failed (staging may have moved): {exc}") from exc

    # -- protocol: cleanup --------------------------------------------------

    async def remove_workspace(self, ws: Workspace, keep_for_diagnostics: bool) -> None:
        """Remove a workspace unless diagnostics retention is requested."""
        if keep_for_diagnostics:
            return
        ws_path = Path(ws.path)
        if ws_path.exists():
            shutil.rmtree(ws_path, ignore_errors=True)

    # -- protocol: push -----------------------------------------------------

    async def push_branch(self, repo: RepoHandle, branch: str) -> PushResult:
        """Push a staging/attempt branch to the remote, if policy allows.

        Never pushes ``main`` and never uses ``--force``. Disabled by default
        (security.allow_push = False).
        """
        if not self._policy.push_allowed():
            return PushResult(
                pushed=False,
                detail="push disabled by policy (security.allow_push is false)",
            )
        if branch == "main" or branch.startswith("refs/heads/main"):
            return PushResult(pushed=False, detail="refusing to push main")
        if not branch.startswith(f"{_BRANCH_PREFIX}/"):
            return PushResult(pushed=False, detail="refusing to push non-devsandbox branch")
        if not repo.url:
            return PushResult(pushed=False, detail="no remote URL configured")
        try:
            await run_git(["push", repo.url, branch], cwd=repo.path)
        except GitCommandError as exc:
            return PushResult(pushed=False, detail=str(exc))
        return PushResult(pushed=True, remote_ref=branch, remote_commit="")

    # -- recovery helpers ---------------------------------------------------

    async def list_workspaces(self, project_id: str) -> list[Path]:
        """List on-disk workspace directories for a project (orphan cleanup)."""
        project_dir = self._workspaces_root / project_id
        if not project_dir.is_dir():
            return []
        return sorted(p for p in project_dir.iterdir() if p.is_dir())

    async def detect_interrupted_integration(self, repo: RepoHandle) -> IntegrationCandidate | None:
        """Detect an integration interrupted by a crash (scratch worktree present)."""
        project_id = repo.project_id
        integration_dir = self._integration_dir(project_id)
        if integration_dir.is_dir():
            return IntegrationCandidate(
                attempt_id=AttemptId("unknown"),
                repo=repo,
                staging_before=None,
                staging_after="",
                conflicting_files=[],
                conflicted=False,
                integration_ref="",
            )
        return None

    async def recover_integration(self, repo: RepoHandle) -> None:
        """Complete or abort an interrupted integration (safe default: abort)."""
        candidate = await self.detect_interrupted_integration(repo)
        if candidate is None:
            return
        # Without a persisted validation flag, abort (safe default).
        await self.abort_integration(candidate)

    # -- internal -----------------------------------------------------------

    def _bare_path(self, project_id: str) -> Path:
        return self._repos_root / project_id / "repo.git"

    def _workspace_path(self, project_id: str, attempt_id: AttemptId) -> Path:
        return self._workspaces_root / project_id / str(attempt_id)

    def _integration_dir(self, project_id: str) -> Path:
        return self._integration_root / project_id

    def _handle(self, project_id: str, bare_path: Path, source: RepoSource) -> RepoHandle:
        staging_branch = f"{_BRANCH_PREFIX}/{project_id}/staging"
        return RepoHandle(
            project_id=project_id,
            path=str(bare_path),
            url=source.url,
            ref=source.ref,
            main_branch="main",
            staging_branch=staging_branch,
        )

    async def _lock_for(self, project_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[project_id] = lock
            return lock

    async def _ensure_staging_branch(self, bare_path: Path, project_id: str, base_ref: str) -> None:
        """Create ``devsandbox/<project>/staging`` from base_ref if missing."""
        base_res = await run_git(["rev-parse", "--verify", base_ref], cwd=bare_path)
        base_commit = base_res.stdout.strip()
        staging_branch = f"{_BRANCH_PREFIX}/{project_id}/staging"
        staging_ref = f"refs/heads/{staging_branch}"
        # Only create if it does not already exist.
        try:
            await run_git(["rev-parse", "--verify", staging_ref], cwd=bare_path)
            return  # already exists
        except GitCommandError:
            pass
        await run_git(["update-ref", staging_ref, base_commit], cwd=bare_path)

    async def _resolve_base_commit(self, repo: RepoHandle, base_ref: str) -> str | None:
        staging_head = await self.staging_head(repo)
        if staging_head:
            return staging_head
        return await self._resolve_ref(repo, base_ref)

    async def _resolve_ref(self, repo: RepoHandle, ref: str) -> str | None:
        try:
            res = await run_git(["rev-parse", "--verify", ref], cwd=repo.path)
        except GitCommandError:
            return None
        return res.stdout.strip() or None

    def _make_container_writable(self, path: Path) -> None:
        """Best-effort: make ``path`` writable by container uid 1000."""
        try:
            if os.geteuid() == 0:
                os.chown(path, _CONTAINER_UID, _CONTAINER_UID)
                for root, _dirs, files in os.walk(path):
                    for d in _dirs:
                        os.chown(os.path.join(root, d), _CONTAINER_UID, _CONTAINER_UID)
                    for f in files:
                        os.chown(os.path.join(root, f), _CONTAINER_UID, _CONTAINER_UID)
        except (PermissionError, OSError, AttributeError):
            # Fall back to world-writable so uid 1000 can write.
            with contextlib.suppress(OSError):
                os.chmod(path, 0o777)
                for root, _dirs, _files in os.walk(path):
                    for d in _dirs:
                        with contextlib.suppress(OSError):
                            os.chmod(os.path.join(root, d), 0o777)

    def _reject_dangerous_hooks(self, repo_path: str | Path) -> None:
        """Reject executable non-sample hooks in an untrusted workspace."""
        hooks_dir = Path(repo_path) / ".git" / "hooks"
        if not hooks_dir.is_dir():
            return
        for entry in hooks_dir.iterdir():
            if entry.is_file() and not entry.name.endswith(".sample") and os.access(entry, os.X_OK):
                raise GitError(f"refusing to execute workspace hook: {entry.name}")

    def _reject_dangerous_content(self, repo_path: str | Path, files_changed: list[str]) -> None:
        """Reject symlink escapes and .gitmodules/.git* config changes."""
        repo_root = Path(repo_path).resolve()
        for rel in files_changed:
            if rel == ".gitmodules" or rel.startswith(".git/"):
                raise GitError(f"refusing to integrate protected path: {rel}")
            full = repo_root / rel
            if full.is_symlink():
                target = Path(os.path.realpath(full)).resolve()
                if not target.is_relative_to(repo_root):
                    raise GitError(f"symlink escapes repository: {rel} -> {target}")

    async def _init_from_local(self, bare_path: Path, source_path: Path, ref: str) -> None:
        if is_git_repo(source_path):
            await run_git(["clone", "--mirror", str(source_path), str(bare_path)])
        else:
            # Import a non-git directory as an initial commit on main.
            with tempfile.TemporaryDirectory(prefix="devsandbox-import-") as tmp:
                tmp_path = Path(tmp)
                await run_git(["init"], cwd=tmp_path)
                for item in source_path.iterdir():
                    dest = tmp_path / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest, symlinks=True)
                    else:
                        shutil.copy2(item, dest)
                await run_git(["add", "-A"], cwd=tmp_path)
                await run_git(["commit", "-m", f"initial import from {source_path}"], cwd=tmp_path)
                await run_git(["clone", "--bare", str(tmp_path), str(bare_path)])

    async def _init_from_url(self, bare_path: Path, url: str, ref: str) -> None:
        env: dict[str, str] = {}
        helper_args: list[str] = []
        cred_user = os.environ.get("DEVSANDBOX_GIT_USERNAME")
        cred_pass = os.environ.get("DEVSANDBOX_GIT_PASSWORD")
        if cred_user or cred_pass:
            helper_path = self._write_credential_helper()
            helper_args = ["-c", f"credential.helper={helper_path}"]
            env["DEVSANDBOX_GIT_USERNAME"] = cred_user or ""
            env["DEVSANDBOX_GIT_PASSWORD"] = cred_pass or ""
        try:
            await run_git(
                ["clone", "--mirror", *helper_args, url, str(bare_path)],
                env=env or None,
            )
        finally:
            for p in list(self._cred_helper_paths):
                with contextlib.suppress(OSError):
                    os.unlink(p)

    def _write_credential_helper(self) -> str:
        helper_dir = Path(tempfile.gettempdir()) / "devsandbox-cred-helpers"
        helper_dir.mkdir(parents=True, exist_ok=True)
        helper_path = helper_dir / f"ds-cred-{os.getpid()}-{id(self)}"
        script = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  get)\n"
            "    printf 'username=%s\\n"
            'password=%s\\n\' "$DEVSANDBOX_GIT_USERNAME" "$DEVSANDBOX_GIT_PASSWORD"\n'
            "    ;;\n"
            "esac\n"
        )
        helper_path.write_text(script, encoding="utf-8")
        helper_path.chmod(0o700)
        self._cred_helper_paths.append(str(helper_path))
        return str(helper_path)

    async def _prepare_integration_locked(
        self, repo: RepoHandle, ws: Workspace, cs: ChangeSet
    ) -> IntegrationCandidate:
        project_id = repo.project_id
        attempt_id = ws.attempt_id
        staging_branch = repo.staging_branch or f"{_BRANCH_PREFIX}/{project_id}/staging"

        staging_before = await self.staging_head(repo)

        # Fetch the attempt branch into the bare repo and validate it.
        attempt_ref = f"{_ATTEMPT_REF_PREFIX}/{attempt_id}"
        await run_git(["fetch", ws.path, f"{ws.branch}:{attempt_ref}"], cwd=repo.path)
        await run_git(["fsck", "--connectivity-only"], cwd=repo.path)

        # Scratch integration worktree: fresh clone of the bare repo.
        integration_dir = self._integration_dir(project_id)
        if integration_dir.is_dir():
            shutil.rmtree(integration_dir)
        integration_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = integration_dir / "worktree"
        await run_git(["clone", repo.path, str(worktree_path)])

        # Bring the attempt commit into the worktree.
        await run_git(["fetch", repo.path, f"{attempt_ref}:{attempt_ref}"], cwd=worktree_path)

        # Checkout staging (or main if no staging) and merge the attempt.
        # ``git clone`` creates remote-tracking branches (origin/<branch>), not
        # local branches, so we check out from origin/.
        if staging_before:
            await run_git(
                ["checkout", "-B", "ds-integration", f"origin/{staging_branch}"],
                cwd=worktree_path,
            )
        else:
            await run_git(
                ["checkout", "-B", "ds-integration", f"origin/{repo.main_branch}"],
                cwd=worktree_path,
            )

        merge = await run_git(["merge", "--no-ff", attempt_ref], cwd=worktree_path, check=False)
        if merge.returncode != 0:
            conflict_files = await self._conflicting_files(worktree_path)
            await run_git(["merge", "--abort"], cwd=worktree_path)
            return IntegrationCandidate(
                attempt_id=attempt_id,
                repo=repo,
                staging_before=staging_before,
                staging_after="",
                conflicting_files=conflict_files,
                conflicted=True,
                integration_ref="",
            )

        staging_after_res = await run_git(["rev-parse", "HEAD"], cwd=worktree_path)
        staging_after = staging_after_res.stdout.strip()

        # Record the candidate commit in the bare repo so the object exists
        # there before the CAS update-ref in commit_integration.
        integration_ref = f"{_INTEGRATION_REF_PREFIX}/{attempt_id}"
        await run_git(
            ["fetch", str(worktree_path), f"{staging_after}:{integration_ref}"],
            cwd=repo.path,
        )

        return IntegrationCandidate(
            attempt_id=attempt_id,
            repo=repo,
            staging_before=staging_before,
            staging_after=staging_after,
            conflicting_files=[],
            conflicted=False,
            integration_ref=integration_ref,
        )

    async def _drop_candidate_ref(self, repo: RepoHandle, integration_ref: str) -> None:
        if not integration_ref:
            return
        with contextlib.suppress(GitCommandError):
            await run_git(["update-ref", "-d", integration_ref], cwd=repo.path)

    async def _remove_integration_dir(self, project_id: str) -> None:
        integration_dir = self._integration_dir(project_id)
        if integration_dir.is_dir():
            shutil.rmtree(integration_dir, ignore_errors=True)

    async def _conflicting_files(self, worktree_path: str | Path) -> list[str]:
        """Return files in conflict state after a failed merge."""
        try:
            res = await run_git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path)
            return [line for line in res.stdout.splitlines() if line.strip()]
        except Exception:  # pragma: no cover - best effort
            return []
