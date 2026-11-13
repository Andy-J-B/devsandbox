"""Fault-injection wrappers shared by the operational drills."""

from __future__ import annotations

from typing import Any

from devsandbox.common.errors import SandboxError
from devsandbox.sandbox.interfaces import SandboxRequest, SandboxResult


class FlakySandbox:
    """Delegate to a real sandbox but fail the first ``failures`` runs.

    Everything except :meth:`run` is forwarded to the wrapped manager, so the
    rest of the orchestrator sees a normal ``SandboxManager``.
    """

    def __init__(self, inner: Any, *, failures: int, message: str) -> None:
        self._inner = inner
        self._remaining = failures
        self._message = message
        self.failures_served = 0

    async def run(self, req: SandboxRequest) -> SandboxResult:
        """Fail while the outage window is open, then delegate."""
        if self._remaining > 0:
            self._remaining -= 1
            self.failures_served += 1
            raise SandboxError(self._message)
        return await self._inner.run(req)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
