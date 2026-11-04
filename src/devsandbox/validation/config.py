"""Validation configuration (plan 09).

All values here are orchestrator policy.  A project spec can select which
checks to run and may request *less* network access, but it can never raise the
output caps, disable required checks, or change the validation image outside
the configured policy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..common.types import NetworkPolicy, ResourceProfile
from ..config.settings import DevSandboxSettings, get_settings


class ValidationConfig(BaseModel):
    """Runtime configuration for the independent validator."""

    model_config = ConfigDict(frozen=True)

    image: str = "devsandbox/validator:latest"
    resources: ResourceProfile = Field(
        default_factory=lambda: ResourceProfile(
            cpu_cores=1.0, memory_mb=2048, disk_mb=4096, pids_limit=512
        )
    )
    network: NetworkPolicy = Field(default_factory=lambda: NetworkPolicy(mode="none"))
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

    @classmethod
    def from_settings(
        cls,
        settings: DevSandboxSettings | None = None,
        **overrides: object,
    ) -> ValidationConfig:
        """Build a config from plan-01 settings plus explicit overrides."""
        resolved = settings or get_settings()
        section = getattr(resolved, "validation", None)
        values: dict[str, object] = {}
        if section is not None:
            for field_name in cls.model_fields:
                if hasattr(section, field_name):
                    values[field_name] = getattr(section, field_name)
        values.update(overrides)
        return cls.model_validate(values)


__all__ = ["ValidationConfig"]
