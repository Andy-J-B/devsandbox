# Sandbox manager (plan 02)

The sandbox manager runs every agent/validation attempt in a disposable,
locked-down Docker container. It implements the `SandboxManager` protocol
(`src/devsandbox/sandbox/interfaces.py`) via `DockerSandboxManager`
(`src/devsandbox/sandbox/manager.py`).

## Interface

- `run(req) -> SandboxResult` — create, run, time-limit, and destroy a container
  for one attempt; stream stdout/stderr to `<data_root>/logs/<attempt_id>/`
  and enforce resources, network, and isolation policy.
- `exec_in(container_id, cmd, timeout_seconds) -> ExecResult` — run a command
  inside a running container.
- `kill(container_id)` / `destroy(container_id)` — stop / remove a container.
- `inspect(container_id) -> ManagedContainer | None` — observe a container.
- `list_managed() -> list[ManagedContainer]` — containers carrying
  `devsandbox.managed=true` (includes stopped ones, for recovery).
- `reap_orphans(known_attempt_ids, max_age_seconds) -> list[str]` — destroy
  managed containers whose attempt is no longer active or that are too old
  (used by plan 08 recovery).
- `docker_info() -> dict` — daemon CPU/memory totals (plan 07 sanity checks).

## Security (enforced in `_build_container_kwargs`, unit-tested)

Deployment-level Docker access (rootless Docker or a constrained socket proxy)
is covered in [deployment.md](deployment.md).

- Requested network policy can only be **narrowed**, never widened. `none` →
  `network_mode="none"`; `full` → default bridge. `allowlist` is not yet
  supported and raises `PolicyViolation` (it never falls back to `full`).
- Images are checked against `SecurityPolicy.image_allowed` before being
  pulled/run.
- Resources are clamped via `SecurityPolicy.effective_resources`.
- Single writable bind mount: `workspace_path -> /workspace`. No
  `docker.sock`, no other writable host mounts.
- `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`, non-root user
  (uid 1000), read-only rootfs, tmpfs for `/tmp` and `/home/agent`.
- Secrets (`secret_files`, with `secret_env` accepted as a legacy alias) are
  written to a private `0700` host directory and bind-mounted **read-only** at
  `/run/secrets`. A wrapper exports them into the process environment before
  `exec`, so the values never appear in the container command, environment, or
  labels (`docker inspect`). The directory is removed when the attempt ends.
  Set `security.secret_dir` to a host tmpfs (for example
  `/dev/shm/devsandbox-secrets` on Linux) to keep the files in memory. Docker
  cannot populate a container tmpfs after start while the rootfs is read-only;
  the configuration-triggered host tmpfs is the practical equivalent.

## Deterministic naming (00 §8.5)

- Container: `ds-<attempt_id>`
- Logs: `<data_root>/logs/<attempt_id>/{stdout,stderr}.log`
- Branch/workspace naming is owned by the git manager (plan 03).
- Labels: `devsandbox.managed=true`, `devsandbox.attempt`, plus caller-supplied
  `devsandbox.project` / `devsandbox.task` via `req.labels`.

## Idempotency & recovery

`run` replaces a container left behind by a prior crash (same attempt id)
rather than duplicating it. `reap_orphans` destroys containers whose attempt is
not active or that exceed `max_age_seconds` (janitor, plan 08).

## Images

- `docker/agent-base/Dockerfile`: owned by plan 04 (the agent runtime).
- `docker/validator-base/Dockerfile`: validation image (python, node, git,
  build-essential, pre-installed pytest/pytest-asyncio/ruff) so common Python
  checks run without network access. Build it with
  `docker build -t devsandbox/validator:latest docker/validator-base`.
- `ensure_image(image)` pulls the image when it is not present locally.
- `security.require_pinned_images` rejects tag references before any Docker
  call; production deployments should pin the agent/validator images by digest
  and allowlist them. See [images.md](images.md) for the upgrade process.

## Testing

- Unit tests: `tests/unit/test_sandbox.py` (no Docker) — policy enforcement,
  request→docker kwargs mapping (asserts no `docker.sock`, `cap_drop`,
  limits, read-only rootfs), log truncation, and redaction of canary secrets.
- Integration tests: `tests/integration/test_sandbox.py` (Docker, `@pytest.mark.docker`).
- Fake: `tests/fakes/FakeSandboxManager` — scriptable exit codes, timeout,
  OOM, delays; records requests.

## Out of scope

Agent-specific prompts/commands (plan 04), git operations (plan 03), and
scheduling (plan 07).
