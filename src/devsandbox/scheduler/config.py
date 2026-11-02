"""Scheduler configuration derived from application settings.

The plan-01 settings expose the policy limits and pressure thresholds.  Plan 07
adds a few scheduler-local tuning knobs with safe defaults; they intentionally
live here (not in the project spec) so neither agents nor planners can change
them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from ..config.settings import DevSandboxSettings, LimitsConfig, get_settings
from .models import SchedulingLimits


class SchedulerConfig(BaseModel):
    """Tuning for the scheduler, resource monitor, and pressure guard."""

    model_config = ConfigDict(frozen=True)

    max_global_concurrency: int = 1
    max_project_concurrency: int = 1
    cpu_headroom_pct: float = 10.0
    mem_headroom_pct: float = 10.0
    disk_min_free_mb: int = 1024
    pressure_cpu_pct: float = 90.0
    pressure_mem_pct: float = 90.0
    pressure_elevated_ratio: float = 0.85
    pressure_hysteresis_samples: int = 2
    pressure_blocks_elevated: bool = False
    aging_per_round: int = 1
    aging_cap: int = 5
    cancel_lowest_priority_on_critical: bool = False
    cpu_sample_interval_seconds: float = 0.1
    sampler_interval_seconds: float = 5.0
    history_seconds: int = 300
    history_max_samples: int = 1000

    @model_validator(mode="after")
    def _validate(self) -> SchedulerConfig:
        if self.max_global_concurrency < 1 or self.max_project_concurrency < 1:
            raise ValueError("concurrency limits must be >= 1")
        if not 0.0 <= self.cpu_headroom_pct <= 100.0:
            raise ValueError("cpu_headroom_pct must be between 0 and 100")
        if not 0.0 <= self.mem_headroom_pct <= 100.0:
            raise ValueError("mem_headroom_pct must be between 0 and 100")
        if not 0.0 < self.pressure_elevated_ratio <= 1.0:
            raise ValueError("pressure_elevated_ratio must be in (0, 1]")
        if self.pressure_hysteresis_samples < 1:
            raise ValueError("pressure_hysteresis_samples must be >= 1")
        if self.aging_per_round < 0 or self.aging_cap < 0:
            raise ValueError("aging values must be >= 0")
        if self.cpu_sample_interval_seconds < 0:
            raise ValueError("cpu_sample_interval_seconds must be >= 0")
        if self.sampler_interval_seconds <= 0:
            raise ValueError("sampler_interval_seconds must be > 0")
        if self.history_seconds < 1 or self.history_max_samples < 1:
            raise ValueError("history settings must be >= 1")
        return self

    @classmethod
    def from_settings(
        cls,
        settings: DevSandboxSettings | None = None,
        *,
        limits: LimitsConfig | None = None,
        **overrides: object,
    ) -> SchedulerConfig:
        """Build a config from application settings with optional overrides."""
        resolved = settings or get_settings()
        source = limits or resolved.limits
        values: dict[str, object] = {
            "max_global_concurrency": source.max_global_concurrency,
            "max_project_concurrency": source.max_project_concurrency,
            "cpu_headroom_pct": source.cpu_headroom_pct,
            "mem_headroom_pct": source.mem_headroom_pct,
            "disk_min_free_mb": source.disk_min_free_mb,
            "pressure_cpu_pct": source.pressure_cpu_pct,
            "pressure_mem_pct": source.pressure_mem_pct,
        }
        values.update(overrides)
        return cls.model_validate(values)

    def scheduling_limits(
        self,
        project_max_concurrency: dict[str, int] | None = None,
    ) -> SchedulingLimits:
        """Return the immutable limits carried inside a scheduling snapshot."""
        return SchedulingLimits(
            max_global_concurrency=self.max_global_concurrency,
            max_project_concurrency=self.max_project_concurrency,
            cpu_headroom_pct=self.cpu_headroom_pct,
            mem_headroom_pct=self.mem_headroom_pct,
            disk_min_free_mb=self.disk_min_free_mb,
            pressure_blocks_elevated=self.pressure_blocks_elevated,
            aging_per_round=self.aging_per_round,
            aging_cap=self.aging_cap,
            project_max_concurrency=dict(project_max_concurrency or {}),
        )
