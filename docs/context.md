# Context manager & repository analysis (plan 05)

The context layer turns a bare git repository at a specific revision into
deterministic, revision-stamped artifacts used by the planner, agents, and the
final report. It performs **no semantic search / embeddings** and makes **zero
LLM calls by default**.

Modules:

- `src/devsandbox/context/analyzer.py` — `RepoAnalyzer`, `analyze_repo`
- `src/devsandbox/context/detectors/` — per-ecosystem `Detector` plugins
- `src/devsandbox/context/source.py` — read-only `GitSnapshot` / diff helpers
- `src/devsandbox/context/store.py` — `ArtifactStore`, staleness, hashing
- `src/devsandbox/context/summary.py` — deterministic summaries + optional LLM
- `src/devsandbox/context/task_context.py` — budgeted `ContextBundle` builder
- `src/devsandbox/context/outcomes.py` — `TaskOutcomeStore`
- `src/devsandbox/context/manager.py` — `ContextManagerImpl` wiring everything

## Analysis is read-only

Analysis only runs git plumbing against the canonical bare repository
(`<data_root>/repos/<project>/repo.git`): `git ls-tree`, `git show`,
`git grep`, and `git diff`. It never checks out a worktree, imports modules,
runs `setup.py`/`conftest.py`, installs packages, or executes repository code.
The tests include canary files that write a marker when imported and assert the
marker never appears.

`RepoProfile` carries the repository revision, file/LOC estimates, a
depth-limited tree summary (ignored build/vendor directories and binaries are
excluded), language shares, manifests, package managers, frameworks,
build/test/lint commands with `source` + `confidence`, test layout, CI files,
conventions, docs, environment hints, a suggested validation image, and
warnings.

### Detector plugins

A detector is any object implementing the `Detector` protocol:

```python
class Detector(Protocol):
    name: str
    def detect(self, files: RepoFiles) -> Detection: ...
```

Built-ins: Python, Node/TypeScript, Go, Rust, Java (Maven/Gradle), a generic
Ruby/PHP manifest detector, and a generic Makefile detector. Add a detector to
`DEFAULT_DETECTORS` (or pass a custom tuple to `RepoAnalyzer`) without touching
the analyzer core.

Spec-provided `validation.commands` always win: detected commands remain in
`test_command_specs`, the spec commands appear in `spec_commands`, and
`test_commands` / `effective_test_commands` contain the override.

## Artifacts and revisions

Kinds: `repo_profile`, `project_summary`, `repo_structure`, `conventions`,
`build_test_commands`, `task_context`, `outcome_notes`.

Every row stores `revision`, JSON content, a SHA-256 hash of canonical JSON,
`generated_by` (`analyzer` or `llm`), dependency paths, timestamps, and a
`stale` flag. Regeneration inserts a new row and marks the previous row
`stale=True`; history is never deleted.

### Staleness rules

`is_stale(artifact_id, current_revision)` first checks the revision and then the
changed paths between the artifact revision and the current revision:

| Artifact | Invalidated by |
| --- | --- |
| `build_test_commands` | changes to any manifest/test-config path in `depends_on_paths` |
| `repo_profile`, `repo_structure` | any **added or deleted** path (modification-only changes stay fresh) |
| `project_summary`, `conventions` | changes to their docs/manifest dependency paths |
| `task_context` | never reused; rebuilt per task |
| `outcome_notes` | append-only history; not cache-reused |

A README-only change therefore leaves command artifacts fresh, while a
`package.json`/`pyproject.toml` change invalidates them. A new source file
invalidates structural artifacts. If the diff cannot be computed (missing
revision), the artifact is treated as stale.

## Project summaries

`SummaryService` defaults to `NullSummarizer`, which returns the deterministic
summary (`render_profile_summary`) from the profile + size-capped README
excerpt. If a custom `Summarizer` is supplied, results are cached by revision
and constrained by an optional token budget; an exhausted budget falls back to
the deterministic summary. This keeps the MVP fully usable with no model.

## Task context bundles

`get_task_context(project_id, task, revision)` returns a `ContextBundle`
stamped with `revision` and `built_at`. Sections are prioritized:

1. task-specific content (instructions, expected file contents, directory listings)
2. completed dependency outcomes
3. recent failure notes for this task
4. conventions + build/test/lint commands
5. project summary
6. repository tree

The default budget is **24,000 characters** (`max_chars` is configurable).
Sections and files are truncated deterministically; every omission is listed in
`bundle.truncation_report`. Expected-file content is capped per file and the
total file-content budget is half of the bundle budget.

Secret-bearing paths are never read (`.env*`, `*.pem`, `*.key`, `id_rsa*`,
`.npmrc`, `.netrc`, `credentials*`, ...), and all rendered text/content passes
through `common.redaction.redact()` to catch token patterns and registered
secrets.

## Outcomes

`record_task_outcome` stores a `TaskOutcomeNote` as an `outcome_notes`
artifact: outcome, revision, files changed, commit ids, validation summary,
failure learnings, and decisions/agent summary. Agent-provided content is
always marked `untrusted=True`; completion decisions must rely on independent
validation. Later tasks can include notes for their dependency task ids; recent
failure notes are surfaced for retries.

## Example

```python
from devsandbox.context import ContextManagerImpl
from devsandbox.context.store import ArtifactStore
from devsandbox.db.repositories import ArtifactRepo

manager = ContextManagerImpl(
    repo_lookup=lambda project_id: repo_handles[project_id],
    artifacts=ArtifactStore(ArtifactRepo(session), repo_lookup=...),
)
profile = await manager.analyze_repo(repo, revision)
bundle = await manager.get_task_context(project_id, task, revision,
                                        dependency_task_ids=["task-a"])
await manager.record_task_outcome(project_id, task.id, note)
```

## Testing

```bash
pytest tests/unit/test_context_*.py -q
```

Fixtures under `tests/fixtures/repos/` (python-pytest, node-jest, go, mixed,
empty, no-tests, monorepo) are copied into temp dirs and built into real git +
bare repositories. Tests cover detector findings, no-code-execution canaries,
the staleness matrix, bundle budget/priorities/secrets, outcome history, and
byte-identical profile JSON for the same revision.

## Out of scope

Semantic search, embeddings, incremental indexing, and planning logic (plan 06).
