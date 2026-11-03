# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Status

Plans 01–08 are implemented: foundation/config/DB/spec, Docker sandbox
manager, git manager, the agent adapter layer (OpenCode + deterministic fake
agent), repository analysis/context, the planner/task-graph lifecycle, the
deterministic scheduler/resource monitor, and the orchestrator core (state
machine, execution loop, retries/circuit breakers, crash recovery).
See `plans/` for the authoritative build plan and `docs/` for per-module
documentation; [`docs/orchestrator.md`](docs/orchestrator.md) and
[`docs/recovery.md`](docs/recovery.md) cover execution and startup
reconciliation.

## Development

```bash
make up       # start postgres via docker compose
make migrate  # alembic upgrade head
make test     # pytest
make check    # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
```
