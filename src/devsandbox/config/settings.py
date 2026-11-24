"""DevSandbox application settings loaded from env (``DEVSANDBOX_*``) and ``devsandbox.toml``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..common.types import normalize_allowed_host


class LimitsConfig(BaseModel):
    """Runtime resource limits and scheduling pressure thresholds."""

    max_global_concurrency: int = 1
    max_project_concurrency: int = 1
    cpu_headroom_pct: float = 10.0
    mem_headroom_pct: float = 10.0
    disk_min_free_mb: int = 1024
    pressure_cpu_pct: float = 90.0
    pressure_mem_pct: float = 90.0
    # Promote a project whose ready work has been deferred this many
    # consecutive rounds ahead of non-starving projects.  ``0`` disables the
    # fairness gate.  This is operator policy and cannot be widened by a spec.
    fairness_starvation_rounds: int = 10

    @field_validator("fairness_starvation_rounds")
    @classmethod
    def _fairness_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("fairness_starvation_rounds must be >= 0")
        return value

    @field_validator("cpu_headroom_pct", "mem_headroom_pct", "pressure_cpu_pct", "pressure_mem_pct")
    @classmethod
    def _pct_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 100.0:
            raise ValueError("percent values must be between 0 and 100")
        return value


class MigrationsConfig(BaseModel):
    """Startup database migration behavior for container/systemd deployments.

    ``auto_upgrade`` applies the packaged Alembic chain before the API or
    orchestration engine starts.  PostgreSQL deployments serialize the upgrade
    with an advisory lock, so overlapping service restarts cannot race the
    schema.  Set it to false only when migrations are run as a separate deploy
    step.
    """

    auto_upgrade: bool = True
    lock_timeout_seconds: float = 120.0

    @field_validator("lock_timeout_seconds")
    @classmethod
    def _positive_lock_timeout(cls, value: float) -> float:
        if value <= 0.0:
            raise ValueError("migrations.lock_timeout_seconds must be > 0")
        return value


class RetriesConfig(BaseModel):
    """Retry / backoff policy."""

    default_max_retries: int = 2
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    # Provider model calls use a separate bounded retry policy.  Retries apply
    # only to transient failures (timeouts, transport errors, HTTP 429/5xx).
    model_call_max_retries: int = 3
    model_call_backoff_base_seconds: float = 1.0
    model_call_backoff_max_seconds: float = 30.0

    @field_validator("default_max_retries", "model_call_max_retries")
    @classmethod
    def _retries_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("retry counts must be >= 0")
        return value

    @field_validator(
        "backoff_base_seconds",
        "backoff_max_seconds",
        "model_call_backoff_base_seconds",
        "model_call_backoff_max_seconds",
    )
    @classmethod
    def _backoff_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("backoff values must be >= 0")
        return value


class GitSettings(BaseModel):
    """Git collection limits (plan 03).

    These limits are orchestrator policy: a project spec can never raise them.
    """

    # Largest single blob (uncompressed) an attempt may introduce.  Enforced
    # while collecting changes, before the content reaches the canonical
    # mirror, integration, validation, or reports.
    max_blob_bytes: int = 10 * 1024 * 1024

    @field_validator("max_blob_bytes")
    @classmethod
    def _positive_blob_cap(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("git.max_blob_bytes must be > 0")
        return value


class SecurityConfig(BaseModel):
    """Security posture (never sourced from the project spec)."""

    # Fail closed on unknown nested keys.  Removing a setting must not turn a
    # stale operator override (for example ``ALLOW_MAIN_MERGE``) into a silent
    # no-op that looks like it was applied.
    model_config = ConfigDict(extra="forbid")

    allow_network_default: bool = False
    # Allowlist egress is disabled by default.  When enabled, a request can
    # only use hosts that also appear in ``allowlist_hosts``; an empty ceiling
    # denies all allowlist traffic.
    allow_network_allowlist: bool = False
    allowlist_hosts: list[str] = Field(default_factory=list)
    # Image for the per-attempt egress proxy.  The orchestrator starts one
    # proxy container per allowlisted attempt on an internal Docker network.
    allowlist_proxy_image: str = "devsandbox/egress-proxy:latest"
    allow_push: bool = False
    allow_autonomous_mode: bool = True
    allowed_images: list[str] = Field(default_factory=list)
    # When true, every agent/validator image must be content-addressed
    # (``repo@sha256:<digest>`` or ``sha256:<image-id>``).  Off for local
    # development where images are built and tagged locally; enable it in
    # deployments that pull from a registry.  See docs/images.md.
    require_pinned_images: bool = False
    # Optional host directory for per-attempt secret files.  Defaults to
    # <data_root>/secrets.  Point this at a host tmpfs (for example
    # /dev/shm/devsandbox-secrets on Linux) to keep secret files off disk.
    secret_dir: str | None = None
    # Seccomp profile applied to every agent/validator container.  The default
    # "hardened" profile is bundled with DevSandbox (Docker's default profile
    # plus explicit denials for namespace, mount, tracing, keyring, module, and
    # kernel-attack syscalls).  "docker-default" omits the option so Docker
    # applies its built-in profile, "unconfined" disables seccomp (not
    # recommended), and an absolute path loads a custom JSON profile.
    seccomp_profile: str = "hardened"
    # Optional AppArmor profile name already loaded on the host.  When unset,
    # Docker applies its built-in "docker-default" profile.  See
    # docker/apparmor/devsandbox-agent for a stricter profile.
    apparmor_profile: str | None = None
    # Optional user-namespace remapping.  Docker only accepts "private" when
    # the daemon itself is configured with userns-remap (see
    # docs/deployment.md); unset leaves the daemon default unchanged.
    userns_mode: Literal["private"] | None = None

    @field_validator("seccomp_profile")
    @classmethod
    def _seccomp_profile_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("seccomp_profile must not be empty")
        return value

    @field_validator("apparmor_profile")
    @classmethod
    def _apparmor_profile_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("apparmor_profile must not be empty")
        if any(character.isspace() or character in ",=" for character in value):
            raise ValueError("apparmor_profile must be a single profile name")
        return value

    @field_validator("allowlist_hosts")
    @classmethod
    def _normalize_allowlist_hosts(cls, value: list[str]) -> list[str]:
        return [normalize_allowed_host(entry) for entry in value]


class ModelBudget(BaseModel):
    """Model inference budget per project."""

    max_tokens: int | None = None
    max_requests: int | None = None


class AgentConfig(BaseModel):
    """Default agent runtime / model configuration.

    ``default_model`` accepts either a bare model id (``qwen3:8b``) or a
    ``provider/model`` reference (``ollama/qwen3:8b``).  ``default_provider``
    can be set explicitly when the model id itself contains a slash.
    ``base_url`` and ``secret_ref`` configure OpenAI-compatible endpoints and
    the host secret that holds the provider key; the value itself never leaves
    the host.
    """

    default_runtime: str = "opencode"
    default_provider: str | None = None
    default_model: str | None = None
    # Agent sandbox image.  Local development uses the built tag; production
    # deployments should set a digest and enable security.require_pinned_images.
    image: str = "devsandbox/agent:latest"
    base_url: str | None = None
    secret_ref: str | None = None
    model_budget: ModelBudget = Field(default_factory=ModelBudget)
    # Process-wide provider launch throttle.  ``0`` disables it.  This gates
    # agent attempts before the container starts; the provider may still
    # throttle individual requests inside OpenCode.
    rate_limit_per_minute: float = 0.0
    rate_limit_burst: int = 1

    @field_validator("rate_limit_per_minute")
    @classmethod
    def _rate_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("rate_limit_per_minute must be >= 0")
        return value

    @field_validator("rate_limit_burst")
    @classmethod
    def _burst_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("rate_limit_burst must be >= 1")
        return value


class PlannerConfig(BaseModel):
    """Planning limits and defaults (owned by plan 06)."""

    max_tasks: int = 100
    max_depth: int = 12
    max_task_timeout_seconds: int = 7200
    default_task_timeout_seconds: int = 1800
    max_replans: int = 2
    min_priority: int = 0
    max_priority: int = 100
    max_expected_files: int = 200
    planner_version: str = "1.0"

    @field_validator("max_tasks", "max_depth", "max_task_timeout_seconds", "max_replans")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("planner limits must be >= 1")
        return value

    @field_validator("default_task_timeout_seconds")
    @classmethod
    def _default_timeout_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("default_task_timeout_seconds must be > 0")
        return value

    @field_validator("max_priority")
    @classmethod
    def _priority_range(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_priority must be >= 0")
        return value


class SecretRef(BaseModel):
    """Reference to a secret: host env var name or file path."""

    env_var: str | None = None
    path: str | None = None


class StorageSettings(BaseModel):
    """On-disk retention and garbage-collection policy.

    These values are operator policy, never project spec input.  The janitor
    only deletes workspaces, agent logs, and validation scratch trees; the
    canonical Git mirrors, integration scratch, and secrets are report-only.
    """

    enabled: bool = True
    # How often the running engine performs a collection pass.  A value of 0
    # disables periodic collection but still allows manual ``devsandbox gc``.
    gc_interval_seconds: int = 3600
    # Age is measured from the newest file modification time in the tree, so
    # recent agent activity keeps a workspace/log alive even if it was created
    # long ago.
    workspace_retention_hours: float = 72.0
    log_retention_hours: float = 168.0
    validation_retention_hours: float = 24.0
    # Keep the most recent N attempt log directories even when they are older
    # than the retention window.  Useful when an operator wants a short audit
    # tail without keeping every historical log.
    keep_latest_log_directories: int = 20

    @field_validator("gc_interval_seconds", "keep_latest_log_directories")
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("storage limits must be >= 0")
        return value

    @field_validator(
        "workspace_retention_hours",
        "log_retention_hours",
        "validation_retention_hours",
    )
    @classmethod
    def _retention_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("retention hours must be >= 0")
        return value


class ApiSettings(BaseModel):
    """HTTP API configuration (plan 10).

    ``token`` is required before the API serves requests.  It is intentionally
    a ``SecretStr`` so it can never be logged accidentally.  The CLI can also
    read the top-level ``DEVSANDBOX_API_TOKEN`` alias.

    ``additional_tokens`` accepts old tokens during a deliberate rotation
    window.  Every configured token authenticates; remove an entry once all
    clients use the new primary token.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    token: SecretStr | None = None
    # Rotation overlap: old tokens remain valid until the operator removes
    # them.  Empty entries are ignored, so a blank env var is harmless.
    additional_tokens: list[SecretStr] = Field(default_factory=list)
    max_spec_bytes: int = 1_000_000
    max_log_bytes: int = 1_000_000
    max_log_lines: int = 5_000
    expensive_rate_limit_per_minute: int = 60
    request_id_header: str = "X-Request-ID"
    run_engine: bool = True
    # Prometheus exposition at /metrics.  It is unauthenticated like the health
    # probes and contains only aggregate counters/gauges (never project ids or
    # secrets), so disabling it is optional hardening for strict deployments.
    metrics_enabled: bool = True

    @field_validator("port")
    @classmethod
    def _port_in_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("api port must be between 1 and 65535")
        return value

    @field_validator("max_spec_bytes", "max_log_bytes", "max_log_lines")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("api limits must be > 0")
        return value

    @field_validator("expensive_rate_limit_per_minute")
    @classmethod
    def _non_negative_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("expensive_rate_limit_per_minute must be >= 0")
        return value


class ValidationSettings(BaseModel):
    """Independent validation policy (plan 09).

    These values are orchestrator configuration, never derived from a project
    spec or agent output.  Per-project checks may *narrow* them (for example a
    check may request no network) but cannot widen the security posture.
    """

    image: str = "devsandbox/validator:latest"
    max_output_bytes: int = 16_384
    max_changed_files: int = 200
    max_diff_lines: int = 5_000
    tamper_mode: Literal["fail", "warn", "ignore"] = "fail"
    expected_files_mode: Literal["ignore", "warn", "fail"] = "warn"
    short_circuit_on_required_failure: bool = False
    fail_on_optional_failure: bool = False
    require_required_check: bool = True
    keep_scratch_on_failure: bool = False
    assert_removal_threshold: int = 2
    forbidden_extensions: list[str] = Field(
        default_factory=lambda: [
            ".exe",
            ".dll",
            ".so",
            ".dylib",
            ".bin",
            ".o",
            ".a",
            ".class",
            ".jar",
            ".pyc",
            ".wasm",
        ]
    )
    test_config_patterns: list[str] = Field(
        default_factory=lambda: [
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "conftest.py",
            "jest.config.*",
            "vitest.config.*",
            "karma.conf.*",
            ".mocharc.*",
            "phpunit.xml",
            ".github/workflows/**",
        ]
    )

    @field_validator("max_output_bytes", "max_changed_files", "max_diff_lines")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("validation limits must be > 0")
        return value

    @field_validator("assert_removal_threshold")
    @classmethod
    def _threshold_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("assert_removal_threshold must be >= 0")
        return value


class DevSandboxSettings(BaseSettings):
    """Top-level DevSandbox settings."""

    model_config = SettingsConfigDict(
        env_prefix="DEVSANDBOX_",
        env_file=(".env", "devsandbox.toml"),
        # Only load prefixed keys from dotenv files.  A developer's .env often
        # contains unrelated provider credentials (for example
        # OPENCODE_API_KEY); those must never become settings inputs.
        dotenv_filtering="match_prefix",
        # Nested settings use ``DEVSANDBOX_<SECTION>__<FIELD>`` (for example
        # ``DEVSANDBOX_API__PORT`` and ``DEVSANDBOX_AGENT__DEFAULT_MODEL``).
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    data_root: str = "/var/lib/devsandbox"
    database_url: str = "postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
    migrations: MigrationsConfig = MigrationsConfig()
    limits: LimitsConfig = LimitsConfig()
    retries: RetriesConfig = RetriesConfig()
    git: GitSettings = GitSettings()
    security: SecurityConfig = SecurityConfig()
    agent: AgentConfig = AgentConfig()
    planner: PlannerConfig = PlannerConfig()
    validation: ValidationSettings = ValidationSettings()
    storage: StorageSettings = StorageSettings()
    api: ApiSettings = ApiSettings()
    # Top-level alias so ``DEVSANDBOX_API_TOKEN`` works as documented.  The
    # nested ``DEVSANDBOX_API__TOKEN`` form is also supported.
    api_token: SecretStr | None = None
    secrets: dict[str, SecretRef] = Field(default_factory=dict)

    @property
    def effective_api_tokens(self) -> tuple[SecretStr, ...]:
        """Return every accepted API token in preference order.

        The top-level ``DEVSANDBOX_API_TOKEN`` alias wins over the nested
        ``api.token`` setting.  Rotation tokens follow the primary token;
        empty and duplicate values are ignored so an operator can leave a
        blank environment variable without disabling authentication.
        """
        primary = self.api_token or self.api.token
        candidates = ([] if primary is None else [primary]) + list(self.api.additional_tokens)
        tokens: list[SecretStr] = []
        seen: set[str] = set()
        for candidate in candidates:
            value = candidate.get_secret_value()
            if not value or value in seen:
                continue
            seen.add(value)
            tokens.append(candidate)
        return tuple(tokens)

    @property
    def effective_api_token(self) -> SecretStr | None:
        """Return the primary API token (top-level alias wins)."""
        tokens = self.effective_api_tokens
        return tokens[0] if tokens else None


_cached_settings: DevSandboxSettings | None = None


def get_settings() -> DevSandboxSettings:
    """Return the process-wide settings (cached)."""
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = DevSandboxSettings()
    return _cached_settings


def reset_settings_cache() -> None:
    """Reset the cached settings (mainly for tests)."""
    global _cached_settings
    _cached_settings = None
