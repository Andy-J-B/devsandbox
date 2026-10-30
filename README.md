# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Status

Early scaffold. Plan 01 (foundation) is in progress; see `plans/` for the
authoritative build plan and contracts.

## Development

```bash
make up       # start postgres via docker compose
make migrate  # alembic upgrade head
make test     # pytest
make check    # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
```
