# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Status

Plans 01–10 are implemented: foundation/config/DB/spec, Docker sandbox
manager, git manager, the agent adapter layer (OpenCode + deterministic fake
agent), repository analysis/context, the planner/task-graph lifecycle, the
deterministic scheduler/resource monitor, the orchestrator core (state
machine, execution loop, retries/circuit breakers, crash recovery), the
independent validator, and the FastAPI API, Typer CLI, and project reports.

Start with [`docs/quickstart.md`](docs/quickstart.md). The HTTP API and CLI are
documented in [`docs/api.md`](docs/api.md) and [`docs/cli.md`](docs/cli.md);
report contents are in [`docs/reporting.md`](docs/reporting.md). The generated
schema is committed at [`docs/openapi.json`](docs/openapi.json). See `plans/`
for the authoritative build plan and `docs/` for per-module documentation;
[`docs/orchestrator.md`](docs/orchestrator.md) and
[`docs/recovery.md`](docs/recovery.md) cover execution and startup
reconciliation.

## Development

```bash
make up       # start postgres via docker compose
make migrate  # alembic upgrade head
make test     # pytest
make check    # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
```
