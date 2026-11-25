# DevSandbox

DevSandbox takes a YAML project spec + a Git repo (local path or URL), analyzes
the repo, generates a task graph, (optionally) asks for approval, then runs
coding agents (OpenCode first) in disposable Docker containers, validates
results independently, and integrates validated work into a staging branch.

## Prerequisites

- Python 3.12 and a Docker daemon the control plane can reach.
- PostgreSQL 16 or newer for the control-plane database.
- PostgreSQL **client** tools (`pg_dump`, `pg_restore`, `psql`) on the host or
  in the orchestrator image for `devsandbox db backup/verify/restore`. The
  client major version must be equal to or newer than the server major; the
  production image pins `POSTGRES_CLIENT_MAJOR=17` by default and accepts a
  build arg when your server is newer. See [docs/db.md](docs/db.md).

## Development

```bash
make up          # start postgres via docker compose
make migrate     # alembic upgrade head
make test        # pytest
make integration # PostgreSQL-backed integration tests (no Docker required)
make check       # scripts/check.sh (ruff + ruff format + mypy strict + pytest)
make check-all   # full gate: check + integration + security + E2E + soak
make drills      # opt-in Docker/PostgreSQL/disk/idle operational drills
make audit       # pip-audit + gitleaks (+ CodeQL if installed)
make supply-chain # build images, generate SBOMs, scan with Grype
```

Deployment guidance (rootless Docker and constrained socket proxies) is in
[docs/deployment.md](docs/deployment.md). Supply-chain controls and image
signing are in [docs/supply-chain.md](docs/supply-chain.md).

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
