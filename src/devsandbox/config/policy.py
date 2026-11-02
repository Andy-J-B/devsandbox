"""Security policy derived only from settings (never from spec / agent input)."""

from __future__ import annotations

from ..common.types import NetworkPolicy, ResourceProfile
from .settings import DevSandboxSettings


class SecurityPolicy:
    """Effective security posture for sandboxed execution."""

    MAX_CPU_CORES: float = 4.0
    MAX_MEMORY_MB: int = 8192
    MAX_DISK_MB: int = 16384
    MAX_PIDS_LIMIT: int = 1024

    def __init__(self, settings: DevSandboxSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> DevSandboxSettings:
        """Return the backing settings."""
        return self._settings

    def effective_network(self, task_requested: NetworkPolicy) -> NetworkPolicy:
        """Narrow the requested network policy to the effective policy."""
        mode = task_requested.mode
        if mode == "full" and not self._settings.security.allow_network_default:
            mode = "none"
        allowed_hosts = task_requested.allowed_hosts if mode == "allowlist" else []
        return NetworkPolicy(mode=mode, allowed_hosts=list(allowed_hosts))

    def effective_resources(self, requested: ResourceProfile) -> ResourceProfile:
        """Clamp the requested resources to policy maxima."""
        return ResourceProfile(
            cpu_cores=min(requested.cpu_cores, self.MAX_CPU_CORES),
            memory_mb=min(requested.memory_mb, self.MAX_MEMORY_MB),
            disk_mb=min(requested.disk_mb, self.MAX_DISK_MB),
            pids_limit=min(requested.pids_limit, self.MAX_PIDS_LIMIT),
        )

    def image_allowed(self, image: str) -> bool:
        """Return whether ``image`` is permitted by policy."""
        allowed = self._settings.security.allowed_images
        if not allowed:
            return True
        return image in allowed

    def push_allowed(self) -> bool:
        """Return whether pushing to remote is permitted."""
        return self._settings.security.allow_push

    def main_merge_allowed(self) -> bool:
        """Return whether integrating into the main branch is permitted."""
        return self._settings.security.allow_main_merge

    def max_retries(self, requested: int) -> int:
        """Return the effective retry budget (never above the policy default)."""
        return min(requested, self._settings.retries.default_max_retries)

    def effective_timeout(self, requested: int) -> int:
        """Return the effective task timeout (never above the planner maximum)."""
        maximum = self._settings.planner.max_task_timeout_seconds
        return min(requested, maximum)

    def max_resources(self) -> ResourceProfile:
        """Return the policy resource ceiling as a profile."""
        return ResourceProfile(
            cpu_cores=self.MAX_CPU_CORES,
            memory_mb=self.MAX_MEMORY_MB,
            disk_mb=self.MAX_DISK_MB,
            pids_limit=self.MAX_PIDS_LIMIT,
        )
