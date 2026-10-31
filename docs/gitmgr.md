# Git manager (plan 03)

The git manager manages the canonical project repository, creates isolated
per-attempt workspaces, collects agent changes safely, enforces path
restrictions, and integrates validated changes into the **staging branch**
(never `main`). It implements the `GitManager` protocol
(`src/devsandbox/gitmgr/interfaces.py`) via `GitManagerImpl`
(`src/devsandbox/gitmgr/manager.py`), on top of the hardened `run_git` wrapper
(`src/devsandbox/gitmgr/command.py`).

## Disk layout (00 §4)

```
<data_root>/repos/<project_id>/repo.git        # bare canonical repo
<data_root>/workspaces/<project_id>/<attempt_id>/  # per-attempt clone
<data_root>/integration/<project_id>/          # scratch integration worktree
```

## Interface

- `init_project_repo(project_id, source) -> RepoHandle` — initialize the
  bare canonical repo (idempotent). Local git path: `clone --mirror` (the
  user's repo is never modified). Local non-git path: `init --bare` plus an
  initial import commit on `main`. Remote URL: `clone --mirror` with
  credentials read from host env via a per-subprocess credential helper
  (never embedded in the URL). Creates `devsandbox/<project_id>/staging`
  from the base ref if missing.
- `create_workspace(repo, attempt_id, base_ref) -> Workspace` — per-attempt
  clone based on the current staging head (falling back to `base_ref`).
  Uses `clone --local --no-hardlinks` so the agent cannot write to the
  canonical object store, then removes the remote. Branch:
  `devsandbox/<project_id>/<attempt_id>/a1`. Workspace is made writable by
  container uid 1000. Deterministic path; wiped and recreated if a prior
  attempt left it behind.
- `collect_changes(ws) -> ChangeSet` — treats workspace content as
  untrusted: runs `git fsck --connectivity-only`, verifies HEAD, rejects
  executable non-sample hooks and dangerous content (symlink escapes,
  `.gitmodules`, `.git/*` config), commits leftover uncommitted changes as
  the orchestrator, and builds the change set (commits, changed files, diff
  stat) against the base commit.
- `check_restricted_paths(cs, restricted) -> list[str]` — gitwildmatch
  (`pathspec`) matching of changed files against restricted globs plus the
  always-restricted baseline `.git/**` and `.github/workflows/**`.
- `integrate_to_staging(repo, ws, cs) -> IntegrationResult` — convenience
  wrapper: prepare + CAS-commit a staging integration.
- `prepare_integration(repo, ws, cs) -> IntegrationCandidate` — two-phase
  prepare under a per-project async lock: fetches the attempt branch into the
  bare repo (fsck-validated), merges onto staging in a scratch worktree
  without advancing any ref. Returns a candidate (or a conflict).
- `commit_integration(candidate) -> IntegrationResult` — atomically advances
  staging via `git update-ref <ref> <new> <old>` (compare-and-swap). Rejected
  if staging moved in the meantime.
- `abort_integration(candidate)` — drops the candidate ref and scratch
  worktree.
- `staging_head(repo) -> str | None` — current staging commit.
- `reset_staging_to(repo, commit)` — CAS-protected rollback; refuses to move
  staging forward or to a commit that is not an ancestor of the current head.
- `remove_workspace(ws, keep_for_diagnostics)` — removes the workspace unless
  retention is requested.
- `push_branch(repo, branch) -> PushResult` — pushes only staging/attempt
  branches, never `--force`, never `main`; disabled unless
  `security.allow_push`.
- `list_workspaces(project_id)`, `detect_interrupted_integration(repo)`,
  `recover_integration(repo)` — recovery helpers for plan 08.

## Security properties

- Every git subprocess runs with `core.hooksPath=/dev/null`, so hooks inside
  untrusted workspaces are **never** executed on the host.
- A fixed orchestrator identity (`DevSandbox <devsandbox@localhost>`) is used
  for all commits; `GIT_TERMINAL_PROMPT=0`.
- The canonical bare repo is never exposed writably to agents (workspace is a
  `--no-hardlinks` clone with its remote removed).
- Staging only advances through CAS `update-ref`; `main` is never touched
  (asserted by test).
- Every subprocess has a timeout; stderr is redacted before logging.

## Deterministic naming (00 §8.5)

- Container (plan 02): `ds-<attempt_id>`
- Workspace: `<data_root>/workspaces/<project_id>/<attempt_id>`
- Branch: `devsandbox/<project_id>/<task_id>/a<attempt_number>`
  (plan 03 uses `devsandbox/<project_id>/<attempt_id>/a1` because the
  `create_workspace` interface does not carry task id / attempt number).
- Labels: owned by the sandbox manager.

## Idempotency & recovery

- `init_project_repo` is idempotent (returns the existing handle, ensuring
  staging exists).
- `create_workspace` wipes and recreates a workspace path left behind by a
  crashed attempt.
- `prepare_integration` runs under a per-project async lock; crashed
  integrations are detected via a leftover scratch worktree and aborted by
  default (`recover_integration`).

## Testing

- Unit tests: `tests/unit/test_gitmgr.py` — real git in temp directories, no
  network. Covers init (local git / non-git / `file://` / idempotency),
  workspace isolation, collect (committed, auto-commit, malicious hook not
  executed, symlink escape, `.gitmodules` rejection), restricted paths,
  clean/conflict/sequential integration, CAS mismatch, staging rollback,
  `main` ref protection, push guards, cleanup, and recovery.
- Fake: `tests/fakes/gitmgr.py` — `FakeGitManager` with scriptable change
  sets, integration outcomes (conflict/rejected), CAS failures, and
  two-phase prepare/commit/abort.

## Out of scope

Running validation (plan 09), deciding when to integrate (plan 08), and
PR creation/merging to `main` (future).
