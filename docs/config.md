# DevSandbox configuration

Settings are loaded from `DEVSANDBOX_*` environment variables and/or the
optional `devsandbox.toml` file (see plan 01 deliverables). Nested keys use a
double underscore, for example `DEVSANDBOX_SECURITY__ALLOW_NETWORK_DEFAULT=true`
and `DEVSANDBOX_AGENT__BASE_URL=http://host.docker.internal:11434/v1`.

- `data_root`, `database_url`
- `limits`: `max_global_concurrency` (MVP default 1), `max_project_concurrency`,
  `cpu_headroom_pct`, `mem_headroom_pct`, `disk_min_free_mb`,
  `pressure_cpu_pct`, `pressure_mem_pct`
- `retries`: `default_max_retries`, `backoff_base_seconds`, `backoff_max_seconds`
- `security`: `allow_network_default`, `allow_push`,
  `allow_autonomous_mode` (supervised → autonomous escalation), `allowed_images`,
  `require_pinned_images` (reject tag-based agent/validator images; see
  [images.md](images.md))
- `api`: `host` (default `127.0.0.1`), `port`, `token` (prefer the top-level
  `DEVSANDBOX_API_TOKEN` alias), `max_spec_bytes`, `max_log_bytes`,
  `max_log_lines`, `expensive_rate_limit_per_minute`, `request_id_header`,
  `run_engine`
- `agent`: `default_runtime`, `default_provider`, `default_model`, `image`,
  `base_url`, `secret_ref`, `model_budget`
  - `image` is the agent sandbox image (default `devsandbox/agent:latest`).
    Production deployments should pin it by digest and enable
    `security.require_pinned_images`; see [images.md](images.md).
  - `default_model` is required for a live agent run. It accepts a bare model id
    (`qwen3:8b`) or a `provider/model` reference (`ollama/qwen3:8b`). Use
    `default_provider` when the model id itself contains a slash. There is no
    implicit model fallback: if it is unset, the OpenCode adapter refuses to
    launch and records a clear configuration error instead of sending a
    nonexistent `opencode/opencode` model id to the provider.
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
See [validation.md](validation.md).
