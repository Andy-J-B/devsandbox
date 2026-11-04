# DevSandbox configuration

Settings are loaded from `DEVSANDBOX_*` environment variables and/or the
optional `devsandbox.toml` file (see plan 01 deliverables).

- `data_root`, `database_url`
- `limits`: `max_global_concurrency` (MVP default 1), `max_project_concurrency`,
  `cpu_headroom_pct`, `mem_headroom_pct`, `disk_min_free_mb`,
  `pressure_cpu_pct`, `pressure_mem_pct`
- `retries`: `default_max_retries`, `backoff_base_seconds`, `backoff_max_seconds`
- `security`: `allow_network_default`, `allow_push`, `allow_main_merge`,
  `allowed_images`
- `agent`: `default_runtime`, `default_model`, `model_budget`
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
