# Storage retention, GC, and disk usage

DevSandbox keeps all mutable state below `data_root`:

| Category | Path | GC |
| --- | --- | --- |
| `repos` | `<data_root>/repos/<project>/repo.git` | never (canonical mirror) |
| `workspaces` | `<data_root>/workspaces/<project>/<attempt>` | expired |
| `logs` | `<data_root>/logs/<attempt>` | expired |
| `validation` | `<data_root>/validation/<run>/workspace` | expired |
| `integration` | `<data_root>/integration/<project>` | never (recovery owns it) |
| `secrets` | `<data_root>/secrets/<attempt>` | never (secret lifecycle) |
| `other` | any other root file/directory | never |

The janitor is implemented by
`devsandbox.storage.StorageJanitor`. It never deletes a workspace, log
directory, or validation scratch tree owned by an active attempt (matched by
attempt id or resolved workspace path). Age is measured from the **newest**
file modification time in the tree, so an agent that is still writing keeps
its workspace alive even if it was created much earlier. Empty project
workspace folders are removed after their attempts expire.

## Retention policy

All values are operator settings under `storage` (see [config.md](config.md));
a project spec can never widen them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `storage.enabled` | `true` | Set to `false` to disable all collection (manual and periodic). |
| `storage.gc_interval_seconds` | `3600` | Periodic engine cadence; `0` disables periodic collection but keeps manual `devsandbox gc`. |
| `storage.workspace_retention_hours` | `72` | Retain attempt workspaces for this long after their newest write. Also feeds `OrchestratorConfig.workspace_retention_hours`. |
| `storage.log_retention_hours` | `168` | Retain agent stdout/stderr logs. |
| `storage.validation_retention_hours` | `24` | Retain validation scratch trees (for example `keep_scratch_on_failure`). |
| `storage.keep_latest_log_directories` | `20` | Always keep the newest N log directories, even if older than the window. |

Collection runs:

1. at startup, as part of crash recovery, before any task resumes;
2. periodically from the engine on `storage.gc_interval_seconds`;
3. on demand through `devsandbox gc` / `POST /v1/system/gc`.

Every pass is idempotent and safe to interrupt: only whole expired trees are
removed, and a failed removal is reported without aborting the other
candidates.

## Disk-usage reporting

`devsandbox storage` (or `GET /v1/system/storage`) reports bytes/files/
directories and oldest/newest mtimes per category, plus the backing
filesystem's total/free/used bytes. The API response is the frozen
`DiskUsageReport` model, so it is safe for dashboards and alerting.

`devsandbox gc --dry-run` (or `POST /v1/system/gc?dry_run=true`) lists what
would be removed and the reclaimable bytes without touching the filesystem.
Manual collections append a `STORAGE_GC_COMPLETED` audit event under project
`system`; automatic engine passes do the same.

## Security notes

* Only the known collectable category roots are scanned. A symlinked project
  or attempt directory is skipped rather than followed.
* Every candidate is resolved and must remain inside its category root before
  removal; a symlink is unlinked, never traversed to its target.
* Repository mirrors, integration scratch, and secret files are report-only.
