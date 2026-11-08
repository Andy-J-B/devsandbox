# Security review (plan 11)

**Review date:** 2026-11-07
**Scope:** the DevSandbox MVP: spec intake, planner output, agent containers,
Git integration, validation, API/CLI reads, and reporting.

This document records the threat model, the hardening changes made during plan
11, the test evidence, and the residual risks that remain accepted for the MVP.

## 1. Threat model

### Assets

- The host running the orchestrator and the Docker daemon.
- Host files outside `<data_root>` (SSH keys, cloud credentials, browser data).
- Model/provider API keys and any other configured secrets.
- The canonical bare repository and the `main` branch.
- Audit history: database rows, event log, reports, and Git history.
- Availability of the host (CPU, memory, pids, disk, log volume).

### Trust boundaries

| Boundary | Trusted? | Notes |
|---|---|---|
| Orchestrator process / Docker daemon | trusted | Daemon access is effectively host-root. |
| Agent container | **untrusted** | Runs arbitrary code supplied by the model/agent. |
| Agent workspace (`<data_root>/workspaces/...`) | **untrusted** | Agent can edit files and `.git`. |
| Project spec / planner output | **untrusted input** | May request elevated network/resources. |
| API client | authenticated, not trusted for policy | Can read logs/reports but cannot change policy. |
| PostgreSQL | trusted | Contains audit data; access is host-local. |
| LLM provider | external | Receives prompts/context when network is enabled. |

### Attacker capabilities considered

1. Prompt-injected agent that writes malicious files, Git config, hooks,
   symlinks, submodules, or huge diffs.
2. Agent code that attempts container escape, host file reads, Docker socket
   access, metadata-service access, or host-loopback access.
3. Agent code that tries to exhaust pids, memory, disk, or log volume.
4. Spec/planner content that requests `network: full`, more CPU/memory, or a
   push/main merge.
5. A caller of the log/artifact API supplying traversal paths or symlinks.
6. Secrets accidentally printed by the agent or embedded in a spec.

## 2. Findings and mitigations

| # | Severity | Finding | Status | Mitigation / evidence |
|---|---|---|---|---|
| F1 | High | Workspace-local `.git/config` could execute host commands via `core.fsmonitor`, `core.sshCommand`, `core.hooksPath`, filters, external diff, credential helpers, or `include`/`includeIf`. | **Fixed** | `GitManagerImpl._reject_dangerous_git_config` rejects dangerous sections/keys before `git fsck`/`diff`. Tests: `tests/security/test_git_hostile.py::test_hostile_git_config_is_rejected`. |
| F2 | High | Default `security.allow_network_default` was `True`, contrary to the authoritative default (off). | **Fixed** | Default is now `False`; `effective_network` narrows `full` to `none`. Tests: `test_network_defaults_to_off`, `test_network_can_only_be_narrowed_by_policy`. |
| F3 | High | Orchestrator requires Docker daemon access; a compromised orchestrator is host-root equivalent. | **Accepted** | Documented residual risk. Deploy on a dedicated host/VM, use rootless Docker or a socket proxy, and never expose the daemon to the agent container. |
| F4 | High | Agent container is untrusted and may attempt escapes. | **Mitigated** | `--privileged` never set, `cap_drop: ALL`, `no-new-privileges`, read-only rootfs, non-root uid 1000, pids/memory/CPU limits, `network_mode=none` by default, only the workspace bind-mounted, no Docker socket. Tests: `tests/security/test_container_isolation.py`. |
| F5 | Medium | Secrets injected as container env are visible to anyone who can run `docker inspect`. | **Accepted** | Daemon access is already host-root-equivalent. Secrets are never placed in the command, labels, logs, reports, or workspace; the redactor covers registered secrets and token patterns. Tests: `tests/security/test_secrets.py`. |
| F6 | Medium | `disk_mb` is a policy field but no per-container/per-workspace disk quota is enforced; workspace is a bind mount. | **Accepted / follow-up** | Scheduler enforces `disk_min_free_mb`; validation caps changed files/diff lines; log output is capped. For multi-tenant use, add project quotas or an overlay/XFS project quota. Test evidence: `test_log_flood_is_capped`; disk-fill is documented, not claimed contained. |
| F7 | Medium | Agent hooks in an untrusted workspace could run on the host. | **Mitigated** | Every Git subprocess uses `core.hooksPath=/dev/null`; executable non-sample hooks are rejected. Tests: `test_executable_hook_is_rejected_and_never_runs`. |
| F8 | Medium | Symlink escapes, `.gitmodules`, and `.git/**` edits could smuggle host content or rewrite history. | **Mitigated** | `_reject_dangerous_content` rejects symlinks resolving outside the workspace, `.gitmodules`, and `.git/**`. Tests: `test_symlink_escape_is_rejected`, `test_submodule_metadata_is_rejected`. |
| F9 | Medium | Agent could push to a remote or merge into `main` if policy were derivable from the spec. | **Mitigated** | Policy comes only from settings; push is denied by default and refuses `main` and non-`devsandbox/` branches; the MVP has no main-merge path and logs a warning if enabled. Tests: `test_push_is_denied_by_default`, `test_push_never_allows_main_even_when_enabled`, `test_main_merge_is_disabled_by_default`. |
| F10 | Medium | Log/artifact API path traversal or symlink read. | **Mitigated** | `_log_path` resolves and confines reads to `<data_root>/logs` or `<data_root>/workspaces`; byte/line caps and redaction are applied. Tests: `tests/security/test_path_traversal.py`. |
| F11 | Medium | Fork bomb / memory hog / log flood. | **Mitigated** | pids limit, memory limit + OOM detection, and capped logs with a truncation marker. Tests: `tests/security/test_resource_abuse.py`. |
| F12 | Medium | Secret literals embedded in a project spec. | **Mitigated** | Recursive secret-field guard plus `extra="forbid"` spec models. Test: `test_spec_literal_secret_fields_are_rejected`. |
| F13 | Low | `allowlist` network mode is not implemented; a naive fallback could widen access. | **Mitigated** | The sandbox manager raises `PolicyViolation` instead of falling back to full network. Test: `test_sandbox_rejects_allowlist_instead_of_widening`. |
| F14 | Low | Unlisted container images could be used. | **Mitigated** | `allowed_images` is checked before any Docker call. Test: `test_sandbox_rejects_unlisted_image_before_docker`. |
| F15 | Low | LLM provider egress can exfiltrate context or secrets if enabled. | **Accepted** | Network is off by default; when enabling a provider, use a scoped key and egress allowlisting/firewall. |
| F16 | Low | Log capture could silently lose output from fast containers (observability, not confidentiality). | **Fixed** | The sandbox attaches to the log socket before starting the container. Unit/integration coverage: `tests/unit/test_sandbox.py`, `tests/integration/test_sandbox_manager.py::test_run_echo_captures_logs`. |

No unresolved **high-severity** findings remain. F3/F5/F6/F15 are accepted MVP
residual risks and are called out below.

## 3. Container hardening checklist

Verified by `tests/security/test_container_isolation.py` against a real Docker
daemon:

- not privileged; `CapDrop=["ALL"]`; `no-new-privileges`;
- read-only root filesystem (`ReadonlyRootfs=true`);
- non-root user (`uid=1000`);
- pids limit present;
- `NetworkMode=none` for the default policy;
- only the attempt workspace is mounted; no `/var/run`, no `docker.sock`;
- host file reads, Docker socket access, metadata/loopback connections, and
  writes outside `/workspace` fail from inside the container.

## 4. Secrets handling

- Agent secrets live as `SecretStr` and are injected only into container env
  (never the command line or labels).
- All container output passes through `common.redaction.redact()`; registered
  provider secrets and common token formats are replaced with `[REDACTED]`.
- API log reads redact again at the boundary.
- Validation/report/git/DB persist only redacted or agent-independent data.
- `tests/security/test_secrets.py` covers known-pattern canaries and arbitrary
  registered secrets, and confirms the canary is absent from logs and the
  workspace.

## 5. Residual risks (accepted for MVP)

| Risk | Why accepted | Recommended next step |
|---|---|---|
| Docker daemon access is host-root equivalent. | The orchestrator must create/attach/kill containers. | Dedicated host/VM, rootless Docker, or a constrained socket proxy. |
| Secrets visible in `docker inspect` env. | Daemon access already grants root; removing env visibility does not change the trust boundary. | Inject via tmpfs file and pass only a path if compliance requires it. |
| No per-workspace disk quota. | Bind mounts inherit host disk; quotas are host/filesystem-specific. | XFS project quotas, overlay upperdir quota, or a per-project volume. |
| LLM provider egress. | Real agents need provider access when enabled. | Egress allowlist and scoped provider keys; keep `allow_network_default=false` otherwise. |
| `allowlist` network mode unimplemented. | Fails closed instead of widening access. | Implement with a DNS/HTTP proxy or per-container firewall. |
| Main-branch merge path absent. | MVP only advances `devsandbox/<project>/staging`. | Add an explicit, policy-gated merge path if required. |

## 6. How to run the security suite

```bash
# static policy / git / traversal checks (no Docker required)
python3 -m pytest -q tests/security

# real container isolation, secrets, pids/OOM, log cap
DEVSANDBOX_E2E=1 python3 -m pytest -q tests/security \
  tests/security/test_container_isolation.py \
  tests/security/test_secrets.py \
  tests/security/test_resource_abuse.py
```

The Docker-gated tests skip cleanly when `DEVSANDBOX_E2E` is unset or the
`devsandbox/validator:latest` image is unavailable.
