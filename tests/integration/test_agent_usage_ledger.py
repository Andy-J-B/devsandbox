"""DB-backed model budget and cost-ledger reconciliation (plan 04/08)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from devsandbox.agents.models import UsageLimiter, UsageSnapshot
from devsandbox.agents.usage import SqlUsageStore, parse_window
from devsandbox.common.errors import BudgetExceeded
from devsandbox.common.types import ModelConfig, TokenUsage
from devsandbox.db.models import CostEntryModel
from devsandbox.db.repositories import CostRepo

MODEL = ModelConfig(provider="openrouter", model="free/model")


def _limited(settings, **budget: int):
    agent = settings.agent.model_copy(
        update={"model_budget": settings.agent.model_budget.model_copy(update=budget)}
    )
    return settings.model_copy(update={"agent": agent})


async def test_sql_usage_store_enforces_budget_across_limiter_instances(
    db_engine, settings
) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    store = SqlUsageStore(maker)
    limited = _limited(settings, max_requests=1, max_tokens=100)
    first = UsageLimiter(
        limited,
        read_async=store.read,
        record_async=store.record,
        window_seconds=3600,
    )

    await first.acheck_budget("proj-1", MODEL)
    await first.arecord_usage("proj-1", MODEL, TokenUsage(total_tokens=40))

    # A fresh limiter has no in-memory state; the DB counter is authoritative.
    second = UsageLimiter(
        limited,
        read_async=store.read,
        record_async=store.record,
        window_seconds=3600,
    )
    assert await second.asnapshot("proj-1", MODEL) == UsageSnapshot(tokens=40, requests=1)
    with pytest.raises(BudgetExceeded):
        await second.acheck_budget("proj-1", MODEL)


async def test_usage_counters_and_inference_ledger_reconcile(db_engine, settings) -> None:
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    store = SqlUsageStore(maker)
    limiter = UsageLimiter(
        _limited(settings, max_tokens=10_000),
        read_async=store.read,
        record_async=store.record,
        window_seconds=3600,
    )
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    await limiter.arecord_usage("proj-1", MODEL, usage)

    window = parse_window(limiter._window())
    async with maker() as session:
        costs = CostRepo(session)
        await costs.add_cost(
            CostEntryModel(
                project_id="proj-1",
                kind="inference",
                amount_usd=0.0,
                units_json=usage.model_dump(mode="json"),
                note="agent:opencode",
            )
        )
        await session.commit()
        counted = await costs.usage_in_window("proj-1", MODEL.provider_id or "", window)
        inferred = await costs.inference_usage_totals("proj-1", note_prefix="agent:")

    assert counted == (15, 1)
    assert inferred == (15, 1)


def test_parse_window_accepts_date_only_and_naive_values() -> None:
    assert parse_window("2026-01-01").tzinfo is UTC
    assert parse_window("2026-01-01T12:00:00") == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
