"""DevSandbox application settings loaded from env (``DEVSANDBOX_*``) and ``devsandbox.toml``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LimitsConfig(BaseModel):
    """Runtime resource limits and scheduling pressure thresholds."""

    max_global_concurrency: int = 1
    max_project_concurrency: int = 1
    cpu_headroom_pct: float = 10.0
    mem_headroom_pct: float = 10.0
    disk_min_free_mb: int = 1024
    pressure_cpu_pct: float = 90.0
    pressure_mem_pct: float = 90.0

    @field_validator("cpu_headroom_pct", "mem_headroom_pct", "pressure_cpu_pct", "pressure_mem_pct")
    @classmethod
    def _pct_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 100.0:
            raise ValueError("percent values must be between 0 and 100")
        return value


class RetriesConfig(BaseModel):
    """Retry / backoff policy."""

    default_max_retries: int = 2
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0

    @field_validator("default_max_retries")
    @classmethod
    def _retries_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("default_max_retries must be >= 0")
        return value

    @field_validator("backoff_base_seconds", "backoff_max_seconds")
    @classmethod
    def _backoff_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("backoff values must be >= 0")
        return value


class SecurityConfig(BaseModel):
    """Security posture (never sourced from the project spec)."""

    allow_network_default: bool = True
    allow_push: bool = False
    allow_main_merge: bool = False
    allowed_images: list[str] = Field(default_factory=list)


class ModelBudget(BaseModel):
    """Model inference budget per project."""

    max_tokens: int | None = None
    max_requests: int | None = None


class AgentConfig(BaseModel):
    """Default agent runtime / model configuration."""

    default_runtime: str = "opencode"
    default_model: str | None = None
    model_budget: ModelBudget = Field(default_factory=ModelBudget)


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
        case_sensitive=False,
        extra="forbid",
    )

    data_root: str = "/var/lib/devsandbox"
    database_url: str = "postgresql+asyncpg://devsandbox:devsandbox@localhost:5432/devsandbox"
    limits: LimitsConfig = LimitsConfig()
    retries: RetriesConfig = RetriesConfig()
    security: SecurityConfig = SecurityConfig()
    agent: AgentConfig = AgentConfig()
    planner: PlannerConfig = PlannerConfig()
    validation: ValidationSettings = ValidationSettings()
    secrets: dict[str, SecretRef] = Field(default_factory=dict)


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
