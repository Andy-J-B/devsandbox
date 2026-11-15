"""Security policy derived only from settings (never from spec / agent input)."""

from __future__ import annotations

import re

from ..common.types import (
    NetworkPolicy,
    ResourceProfile,
    host_matches_allowlist,
    split_allowed_host,
)
from .settings import DevSandboxSettings

# A digest reference is a registry manifest reference (``repo@sha256:<64 hex>``)
# or a local image ID (``sha256:<64 hex>``).  Tags, including ``:latest``, are
# mutable and therefore not pinned.
_DIGEST_IMAGE_RE = re.compile(
    r"^(?:[^\s@]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$",
    re.IGNORECASE,
)


def is_digest_pinned(image: str) -> bool:
    """Return whether ``image`` is content-addressed rather than tag-based."""
    return bool(_DIGEST_IMAGE_RE.match(image.strip()))


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
        """Narrow the requested network policy to the effective policy.

        ``full`` is allowed only when ``allow_network_default`` is true.
        ``allowlist`` is allowed only when ``allow_network_allowlist`` is true
        *and* the requested hosts are covered by ``allowlist_hosts``.  An
        empty request or empty policy ceiling therefore narrows to ``none``.
        """
        mode = task_requested.mode
        if mode == "full" and not self._settings.security.allow_network_default:
            mode = "none"
        allowed_hosts: list[str] = []
        if mode == "allowlist":
            if not self._settings.security.allow_network_allowlist:
                mode = "none"
            else:
                ceiling = self._settings.security.allowlist_hosts
                allowed_hosts = self._narrow_hosts(task_requested.allowed_hosts, ceiling)
                if not allowed_hosts:
                    mode = "none"
        return NetworkPolicy(mode=mode, allowed_hosts=allowed_hosts)

    @staticmethod
    def _narrow_hosts(requested: list[str], ceiling: list[str]) -> list[str]:
        """Return requested entries that are covered by the policy ceiling."""
        if not ceiling:
            return []
        narrowed: list[str] = []
        for entry in requested:
            if entry.startswith("*."):
                # A wildcard request is only narrowed to an identical wildcard
                # in policy; inferring coverage from broader patterns is easy
                # to get wrong, so fail closed.
                if entry in ceiling:
                    narrowed.append(entry)
                continue
            host, port = split_allowed_host(entry)
            if port is not None:
                covered = host_matches_allowlist(host, port, ceiling)
            else:
                # An entry without a port permits both 80 and 443.  Require
                # both to be covered so the narrowed entry cannot widen the
                # policy ceiling's ports.
                covered = host_matches_allowlist(host, 80, ceiling) and host_matches_allowlist(
                    host, 443, ceiling
                )
            if covered:
                narrowed.append(entry)
        return narrowed

    def effective_resources(self, requested: ResourceProfile) -> ResourceProfile:
        """Clamp the requested resources to policy maxima."""
        return ResourceProfile(
            cpu_cores=min(requested.cpu_cores, self.MAX_CPU_CORES),
            memory_mb=min(requested.memory_mb, self.MAX_MEMORY_MB),
            disk_mb=min(requested.disk_mb, self.MAX_DISK_MB),
            pids_limit=min(requested.pids_limit, self.MAX_PIDS_LIMIT),
        )

    def image_allowed(self, image: str) -> bool:
        """Return whether ``image`` is permitted by the allowlist."""
        allowed = self._settings.security.allowed_images
        if not allowed:
            return True
        return image in allowed

    def image_violation(self, image: str) -> str | None:
        """Return a policy violation for ``image``, or ``None`` when allowed.

        Enforces both the optional allowlist and the optional digest-pinning
        requirement.  Callers fail closed on any non-``None`` result *before*
        touching Docker.
        """
        if not self.image_allowed(image):
            return f"image not allowed by policy: {image}"
        if self._settings.security.require_pinned_images and not is_digest_pinned(image):
            return (
                f"image is not pinned by digest: {image} "
                "(set security.require_pinned_images=false only for local development)"
            )
        return None

    def proxy_image_violation(self, image: str) -> str | None:
        """Return a policy violation for the egress proxy image, or ``None``.

        The proxy image is operator-configured (never spec-controlled), so the
        agent/validator image allowlist does not apply.  The digest-pinning
        requirement does apply, because an unpinned proxy is part of the
        network trust boundary.
        """
        if self._settings.security.require_pinned_images and not is_digest_pinned(image):
            return (
                f"egress proxy image is not pinned by digest: {image} "
                "(set security.require_pinned_images=false only for local development)"
            )
        return None

    def push_allowed(self) -> bool:
        """Return whether pushing to remote is permitted."""
        return self._settings.security.allow_push

    def autonomous_mode_allowed(self) -> bool:
        """Return whether projects may be escalated to autonomous mode."""
        return self._settings.security.allow_autonomous_mode

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
