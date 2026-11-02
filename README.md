# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Status

Plans 01–07 are implemented: foundation/config/DB/spec, Docker sandbox
manager, git manager, the agent adapter layer (OpenCode + deterministic fake
agent), repository analysis/context, the planner/task-graph lifecycle, and the
deterministic scheduler/resource monitor.
See `plans/` for the authoritative build plan and `docs/` for per-module
documentation.

## Development

```bash
make up       # start postgres via docker compose
make migrate  # alembic upgrade head
make test     # pytest
make check    # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
```
