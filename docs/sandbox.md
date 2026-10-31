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

- Requested network policy can only be **narrowed**, never widened. `none` →
  `network_mode="none"`; `full` → default bridge. `allowlist` is not yet
  supported and raises `PolicyViolation` (it never falls back to `full`).
- Images are checked against `SecurityPolicy.image_allowed` before being
  pulled/run.
- Resources are clamped via `SecurityPolicy.effective_resources`.
- Single writable bind mount: `workspace_path -> /workspace` only. No
  `docker.sock`, no other host mounts.
- `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`, non-root user
  (uid 1000), read-only rootfs, tmpfs for `/tmp` and `/home/agent`.
- Secrets (`secret_env`) are injected as container environment variables at
  create time (never the command line or labels) and are redacted from logs
  and exceptions.

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
- `docker/validator-base/Dockerfile`: provided here (python, node, git,
  build-essential) for running validation commands; plan 09 may extend it.
- `ensure_image(image)` pulls the image when it is not present locally.

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
