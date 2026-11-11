# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Development

```bash
make up       # start postgres via docker compose
make migrate  # alembic upgrade head
make test     # pytest
make check    # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
```

## How to use

CLI: start and run a project

Start the API + orchestration engine:

export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
devsandbox serve

Then submit and run a spec:

devsandbox submit path/to/project.yaml # prints PROJECT_ID
devsandbox plan PROJECT_ID
devsandbox approve PLAN_ID
devsandbox start PROJECT_ID
devsandbox status PROJECT_ID --watch
devsandbox report PROJECT_ID --out report.md

Validated work lands on `devsandbox/<PROJECT_ID>/staging` in the per-project
bare mirror at `$DEVSANDBOX_DATA_ROOT/repos/<PROJECT_ID>/repo.git`, not in the
source repository. To review and merge it manually:

```bash
MIRROR="$DEVSANDBOX_DATA_ROOT/repos/PROJECT_ID/repo.git"
BRANCH="devsandbox/PROJECT_ID/staging"
git -C /path/to/repo fetch "$MIRROR" "${BRANCH}:refs/heads/${BRANCH}"
git -C /path/to/repo log main.."$BRANCH"
git -C /path/to/repo diff main.."$BRANCH"
git -C /path/to/repo merge --no-ff "$BRANCH"
```

See [docs/quickstart.md](docs/quickstart.md) for the full walkthrough,
including local Ollama configuration.
