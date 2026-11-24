# DevSandbox configuration

Settings are loaded from `DEVSANDBOX_*` environment variables and/or the
optional `devsandbox.toml` file (see plan 01 deliverables). Nested keys use a
double underscore, for example `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT=true`
and `DEVSANDBOX_AGENT__BASE_URL=http://host.docker.internal:11434/v1`.

- `data_root`, `database_url`
- `migrations`: `auto_upgrade` (default `true`; apply the packaged Alembic chain
  before `serve` binds the API or starts the engine), `lock_timeout_seconds`
  (default `120`; PostgreSQL advisory-lock wait for contending starts). Disable
  only when migrations are a separate deployment step. See
  [deployment.md](deployment.md#55-container-and-systemd-packaging-startup-migrations).
- `limits`: `max_global_concurrency` (MVP default 1), `max_project_concurrency`,
  `cpu_headroom_pct`, `mem_headroom_pct`, `disk_min_free_mb`,
  `pressure_cpu_pct`, `pressure_mem_pct`, `fairness_starvation_rounds`
  (default `10`; a project with ready work deferred this many consecutive
  rounds is promoted ahead of non-starving projects; `0` disables the gate).
  A project spec's `limits.max_concurrency` narrows the per-project quota but
  can never raise it above `max_project_concurrency`.
- `retries`: `default_max_retries`, `backoff_base_seconds`, `backoff_max_seconds`,
  plus the provider-call policy `model_call_max_retries`,
  `model_call_backoff_base_seconds`, `model_call_backoff_max_seconds`
  (retries apply only to timeouts, transport errors, HTTP 408/425/429/5xx;
  `Retry-After` is honoured).
- `git`: `max_blob_bytes` (default 10 MiB) — collect-time cap on any single
  blob an attempt introduces. Content above the cap is rejected before it
  reaches the canonical mirror, integration, validation, or reports, and is
  classified as a retryable `size_limit` failure. This is settings-only policy;
  a project spec cannot raise it.
- `security`: `allow_network_default`, `allow_network_allowlist` (enable
  allowlist egress; off by default), `allowlist_hosts` (operator ceiling; a JSON
  list of `host` or `host:port` entries, optional `*.` subdomain wildcard; an
  empty ceiling denies all allowlist traffic), `allowlist_proxy_image` (egress
  proxy image; default `devsandbox/egress-proxy:latest`), `allow_push`,
  `allow_autonomous_mode` (supervised → autonomous escalation), `allowed_images`,
  `require_pinned_images` (reject tag-based agent/validator/proxy images; see
  [images.md](images.md)), `secret_dir` (optional host directory for per-attempt
  secret files; defaults to `<data_root>/secrets`; point it at a host tmpfs such
  as `/dev/shm/devsandbox-secrets` to keep secrets off disk),
  `seccomp_profile` (default `hardened`: bundled Docker-default-plus-denials
  profile; also `docker-default`, `unconfined`, or an absolute JSON path),
  `apparmor_profile` (loaded host profile name; unset keeps Docker's
  `docker-default`), `userns_mode` (`private` only when the daemon uses
  `userns-remap`; unset keeps the daemon default). See
  [sandbox.md](sandbox.md#security-enforced-in-_build_container_kwargs-unit-tested).
  - Allowlist entries without a port permit only ports 80/443; use
    `host:port` for anything else. Requests are intersected with this ceiling,
    so a project spec can narrow the list but never add a host.
- `api`: `host` (default `127.0.0.1`), `port`, `token` (prefer the top-level
  `DEVSANDBOX_API_TOKEN` alias), `additional_tokens` (JSON list of old tokens
  kept valid during a rotation window; empty and duplicate entries are
  ignored), `max_spec_bytes`, `max_log_bytes`, `max_log_lines`,
  `expensive_rate_limit_per_minute`, `request_id_header`, `run_engine`,
  `metrics_enabled` (Prometheus `/metrics`; default true, aggregate-only and
  unauthenticated like the probes).
  Rotation and TLS/ingress guidance: [deployment.md](deployment.md#6-api-exposure-tls-and-token-rotation).
  There are deliberately no uvicorn certificate/key settings: TLS terminates at
  the shipped reverse-proxy example or your ingress controller.
- `agent`: `default_runtime`, `default_provider`, `default_model`, `image`,
  `base_url`, `secret_ref`, `model_budget`, `rate_limit_per_minute`,
  `rate_limit_burst`
  - `default_runtime` selects the adapter: `opencode` (default) or
    `fake-script` (deterministic, no model; used for drills and local
    end-to-end checks). Unknown values fail at startup instead of silently
    running a real model.
  - `image` is the agent sandbox image (default `devsandbox/agent:latest`).
    Production deployments should pin it by digest and enable
    `security.require_pinned_images`; see [images.md](images.md).
  - `default_model` is required for a live agent run. It accepts a bare model id
    (`qwen3:8b`) or a `provider/model` reference (`ollama/qwen3:8b`). Use
    `default_provider` when the model id itself contains a slash. There is no
    implicit model fallback: if it is unset, the OpenCode adapter refuses to
    launch and records a clear configuration error instead of sending a
    nonexistent `opencode/opencode` model id to the provider.
  - `rate_limit_per_minute` / `rate_limit_burst` throttle agent launches with a
    process-local token bucket before a container starts. `0` (default)
    disables throttling. The provider may still throttle requests inside
    OpenCode; the planner's `OpenAICompatibleClient` has its own token bucket
    and retry policy for direct model calls.
  - `base_url` points an OpenAI-compatible endpoint at a custom server; for a
    local Ollama instance use `http://host.docker.internal:11434/v1` on Docker
    Desktop (or the host bridge address on Linux).
  - `secret_ref` names a `[secrets.<name>]` entry; the secret value is injected
    only into the agent container, under the provider's standard API-key
    variable (for example `OPENROUTER_API_KEY`) or
    `DEVSANDBOX_MODEL_API_KEY` for custom OpenAI-compatible endpoints. It is
    never written to prompts, config, commands, or logs.
  - The sandbox has no egress by default. Reaching a host-local model also
    requires `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT=true`; project specs
    cannot widen that policy.
- `storage`: data-root retention and GC policy (see [storage.md](storage.md))
  - `enabled` (default `true`), `gc_interval_seconds` (default `3600`; `0`
    disables periodic collection but keeps manual `devsandbox gc`)
  - `workspace_retention_hours` (default `72`), `log_retention_hours`
    (default `168`), `validation_retention_hours` (default `24`)
  - `keep_latest_log_directories` (default `20`) keeps the newest log
    directories even when they exceed the age window
  - Only workspaces, agent logs, and validation scratch are collectable;
    canonical repo mirrors, integration scratch, and secrets are report-only.
- `audit`: optional export of the append-only event log (see [audit.md](audit.md))
  - `enabled` (default `false`), `sink` (`file` default, or `webhook`)
  - `path` / `state_path` default to `<data_root>/audit/events.ndjson` and
    `<data_root>/audit/cursor.json`
  - `webhook_url`, `webhook_token`, `webhook_timeout_seconds`
  - `interval_seconds` (default `5`), `batch_size` (default `200`),
    `max_retries` (default `3`), `retry_backoff_seconds`,
    `max_payload_bytes` (default `64000`). Shipping is at-least-once: the
    cursor advances only after a batch is accepted.
- `validation`: independent validation policy (plan 09)
  - `image` (default `devsandbox/validator:latest`), `max_output_bytes`
  - `max_changed_files`, `max_diff_lines`
  - `tamper_mode` (`fail|warn|ignore`), `expected_files_mode`
  - `short_circuit_on_required_failure`, `fail_on_optional_failure`,
    `require_required_check`, `keep_scratch_on_failure`
  - `assert_removal_threshold`, `forbidden_extensions`, `test_config_patterns`
- `secrets`: provider mapping name → env var on host or file path

Security policy is derived only from settings via
`devsandbox.config.policy.SecurityPolicy`. Validation checks can narrow the
configured network/resources, but cannot widen them or receive agent secrets.
See [validation.md](validation.md). For deployment guidance (rootless Docker or
a constrained socket proxy) see [deployment.md](deployment.md).
