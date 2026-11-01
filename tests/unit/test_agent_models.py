"""Tests for model budget enforcement and usage accounting."""

from __future__ import annotations

import pytest
from tests.agent_helpers import make_settings

from devsandbox.agents.models import UsageLimiter, UsageSnapshot
from devsandbox.common.errors import BudgetExceeded
from devsandbox.common.types import ModelConfig, TokenUsage

MODEL = ModelConfig(provider="openrouter", model="free/model")


def test_no_budget_allows_everything() -> None:
    limiter = UsageLimiter(make_settings())
    limiter.check_budget("p1", MODEL)
    limiter.record_usage("p1", MODEL, TokenUsage(total_tokens=10_000))
    assert limiter.remaining("p1", MODEL) == (None, None)


def test_request_budget_blocks_when_exhausted() -> None:
    limiter = UsageLimiter(make_settings(max_requests=1))
    limiter.check_budget("p1", MODEL)
    limiter.record_usage("p1", MODEL, None)
    with pytest.raises(BudgetExceeded):
        limiter.check_budget("p1", MODEL)
    assert limiter.remaining("p1", MODEL)[1] == 0


def test_token_budget_blocks_when_exhausted() -> None:
    limiter = UsageLimiter(make_settings(max_tokens=100))
    limiter.record_usage("p1", MODEL, TokenUsage(input_tokens=60, output_tokens=40))
    with pytest.raises(BudgetExceeded):
        limiter.check_budget("p1", MODEL)


def test_budget_is_per_project_and_window() -> None:
    limiter = UsageLimiter(make_settings(max_requests=1), window=lambda: "day-1")
    limiter.record_usage("p1", MODEL, None)
    limiter.check_budget("p2", MODEL)  # different project unaffected
    with pytest.raises(BudgetExceeded):
        limiter.check_budget("p1", MODEL)


def test_snapshot_and_remaining() -> None:
    limiter = UsageLimiter(make_settings(max_tokens=1_000, max_requests=5))
    limiter.record_usage(
        "p1", MODEL, TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
    )
    snapshot = limiter.snapshot("p1", MODEL)
    assert snapshot == UsageSnapshot(tokens=150, requests=1)
    assert limiter.remaining("p1", MODEL) == (850, 4)


def test_injected_store_and_cost_recorder_are_used() -> None:
    store: dict[tuple[str, str, str], UsageSnapshot] = {}
    costs: list[tuple[str, ModelConfig, TokenUsage | None]] = []

    def read(project_id: str, provider: str, window: str) -> UsageSnapshot:
        return store.get((project_id, provider, window), UsageSnapshot()).copy()

    def record(project_id: str, provider: str, window: str, tokens: int, requests: int) -> None:
        snapshot = store.setdefault((project_id, provider, window), UsageSnapshot())
        snapshot.tokens += tokens
        snapshot.requests += requests

    limiter = UsageLimiter(
        make_settings(max_tokens=100),
        read=read,
        record=record,
        cost_recorder=lambda project_id, model, usage: costs.append((project_id, model, usage)),
        window=lambda: "window-1",
    )
    usage = TokenUsage(total_tokens=100)
    limiter.record_usage("p1", MODEL, usage)
    assert store[("p1", "openrouter", "window-1")] == UsageSnapshot(tokens=100, requests=1)
    assert costs == [("p1", MODEL, usage)]
    with pytest.raises(BudgetExceeded):
        limiter.check_budget("p1", MODEL)
