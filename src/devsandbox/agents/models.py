"""Model provider descriptors, budget enforcement, and usage accounting.

``ModelConfig`` (provider, model, base_url, secret_ref, limits) lives in
``devsandbox.common.types``; this module adds provider descriptors and a
``UsageLimiter`` that enforces the per-project model budget before each run
and records usage after each run.

Persistence is injected: production wiring (plan 08) supplies callbacks that
read/write ``usage_counters`` and ``cost_entries``; unit tests use the
in-memory defaults.  Inference cost is kept separate from infra cost by the
caller (``cost_entries.kind == "inference"``); free models record amount 0
while still tracking tokens.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..common.errors import BudgetExceeded
from ..common.time import utcnow
from ..common.types import ModelConfig, TokenUsage
from ..config.settings import DevSandboxSettings


@dataclass(frozen=True)
class ModelProvider:
    """Descriptor for a single model provider.

    ``base_url`` is the endpoint used by OpenAI-compatible clients (or None
    for the provider's default cloud endpoint).  ``secret_ref`` names the
    secret (host env var, file path, or ``settings.secrets`` entry) that holds
    the provider API key.
    """

    name: str
    base_url: str | None = None
    secret_ref: str | None = None


@dataclass
class UsageSnapshot:
    """Token / request usage for a project/provider window."""

    tokens: int = 0
    requests: int = 0

    def copy(self) -> UsageSnapshot:
        """Return an independent copy."""
        return UsageSnapshot(tokens=self.tokens, requests=self.requests)


# A usage store is responsible for reading and recording usage.  It is an
# injected dependency so the limiter can be unit-tested without a database.
UsageRead = Callable[[str, str, str], UsageSnapshot]
UsageRecord = Callable[[str, str, str, int, int], None]
CostRecorder = Callable[[str, ModelConfig, TokenUsage | None], None]


class UsageLimiter:
    """Enforce the per-project model budget and record usage.

    ``settings.agent.model_budget`` holds max_tokens/max_requests per
    project window.  ``read`` returns current usage for
    ``(project_id, provider, window)``; ``record`` persists new usage.
    """

    def __init__(
        self,
        settings: DevSandboxSettings,
        *,
        read: UsageRead | None = None,
        record: UsageRecord | None = None,
        cost_recorder: CostRecorder | None = None,
        window: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._read = read or self._default_read
        self._record = record or self._default_record
        self._cost_recorder = cost_recorder
        self._window_fn = window or self._default_window
        self._memory: dict[tuple[str, str, str], UsageSnapshot] = {}

    # -- public API -------------------------------------------------------

    def check_budget(self, project_id: str, model: ModelConfig) -> None:
        """Raise :class:`BudgetExceeded` if the next run would exceed budget."""
        budget = self._settings.agent.model_budget
        if budget.max_tokens is None and budget.max_requests is None:
            return
        provider = model.provider_id
        window = self._window_fn()
        snapshot = self._read(project_id, provider, window)
        if budget.max_tokens is not None and snapshot.tokens >= budget.max_tokens:
            raise BudgetExceeded(
                f"model token budget exhausted for project {project_id} "
                f"provider {provider} window {window} "
                f"({snapshot.tokens}/{budget.max_tokens} tokens)"
            )
        if budget.max_requests is not None and snapshot.requests >= budget.max_requests:
            raise BudgetExceeded(
                f"model request budget exhausted for project {project_id} "
                f"provider {provider} window {window} "
                f"({snapshot.requests}/{budget.max_requests} requests)"
            )

    def record_usage(
        self,
        project_id: str,
        model: ModelConfig,
        token_usage: TokenUsage | None,
        *,
        requests: int = 1,
    ) -> None:
        """Record usage from a completed run (best effort, no secrets)."""
        provider = model.provider_id
        window = self._window_fn()
        tokens = _total_tokens(token_usage)
        self._record(project_id, provider, window, tokens, requests)
        if self._cost_recorder is not None:
            self._cost_recorder(project_id, model, token_usage)

    def snapshot(self, project_id: str, model: ModelConfig) -> UsageSnapshot:
        """Return current usage for ``project_id``/provider/window."""
        return self._read(project_id, model.provider_id, self._window_fn()).copy()

    def remaining(self, project_id: str, model: ModelConfig) -> tuple[int | None, int | None]:
        """Return remaining (tokens, requests), with ``None`` meaning unlimited."""
        budget = self._settings.agent.model_budget
        snapshot = self.snapshot(project_id, model)
        tokens = None if budget.max_tokens is None else max(0, budget.max_tokens - snapshot.tokens)
        requests = (
            None if budget.max_requests is None else max(0, budget.max_requests - snapshot.requests)
        )
        return tokens, requests

    # -- default in-memory store -----------------------------------------

    def _default_read(self, project_id: str, provider: str, window: str) -> UsageSnapshot:
        return self._memory.get((project_id, provider, window), UsageSnapshot()).copy()

    def _default_record(
        self, project_id: str, provider: str, window: str, tokens: int, requests: int
    ) -> None:
        key = (project_id, provider, window)
        snapshot = self._memory.setdefault(key, UsageSnapshot())
        snapshot.tokens += tokens
        snapshot.requests += requests

    @staticmethod
    def _default_window() -> str:
        # Use a UTC date window so budgets reset daily by default.
        return utcnow().date().isoformat()


def _total_tokens(token_usage: TokenUsage | None) -> int:
    if token_usage is None:
        return 0
    if token_usage.total_tokens:
        return token_usage.total_tokens
    return token_usage.input_tokens + token_usage.output_tokens


__all__ = [
    "CostRecorder",
    "ModelProvider",
    "UsageLimiter",
    "UsageRead",
    "UsageRecord",
    "UsageSnapshot",
]
