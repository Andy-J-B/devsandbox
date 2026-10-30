# DevSandbox configuration

Settings are loaded from `DEVSANDBOX_*` environment variables and/or the
optional `devsandbox.toml` file (see plan 01 deliverables).

- `data_root`, `database_url`
- `limits`: `max_global_concurrency`, `max_project_concurrency`, `cpu_headroom_pct`,
  `mem_headroom_pct`, `disk_min_free_mb`, `pressure_cpu_pct`, `pressure_mem_pct`
- `retries`: `default_max_retries`, `backoff_base_seconds`, `backoff_max_seconds`
- `security`: `allow_network_default`, `allow_push`, `allow_main_merge`,
  `allowed_images`
- `agent`: `default_runtime`, `default_model`, `model_budget`
- `secrets`: provider mapping name → env var on host or file path

Security policy is derived only from settings via
`devsandbox.config.policy.SecurityPolicy`.
