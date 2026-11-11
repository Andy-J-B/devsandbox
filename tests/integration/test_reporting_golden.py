"""Golden-file report tests from seeded database scenarios (plan 10)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.planner_helpers import make_spec
from tests.reporting_helpers import seed_report_scenario

from devsandbox.config.settings import DevSandboxSettings
from devsandbox.db.models import TaskModel
from devsandbox.reporting.builder import build_report
from devsandbox.reporting.render import render_markdown

SCENARIOS = [
    "all_success",
    "mixed_failures",
    "blocked_chain",
    "conflict",
    "policy_denial",
]
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden" / "reports"


def _clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> DevSandboxSettings:
    return DevSandboxSettings(
        data_root=str(tmp_path / "data"),
        database_url="postgresql+asyncpg://unused/unused",
        agent={"model_budget": {"max_tokens": 5000, "max_requests": 100}},
    )


async def _build(db_engine, tmp_path: Path, scenario: str):
    settings = _settings(tmp_path)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    project_id = await seed_report_scenario(maker, scenario, make_spec(name=scenario))
    report = await build_report(project_id, maker, settings=settings, clock=_clock)
    return report


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_report_matches_golden_files(db_engine, tmp_path, scenario) -> None:
    report = await _build(db_engine, tmp_path, scenario)
    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    markdown = render_markdown(report)
    if not markdown.endswith("\n"):
        markdown += "\n"

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    json_path = GOLDEN_DIR / f"{scenario}.json"
    markdown_path = GOLDEN_DIR / f"{scenario}.md"
    if os.environ.get("UPDATE_GOLDEN"):
        json_path.write_text(payload, encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")
    assert json_path.exists(), f"missing golden file {json_path}; run UPDATE_GOLDEN=1"
    assert markdown_path.exists(), f"missing golden file {markdown_path}; run UPDATE_GOLDEN=1"
    assert payload == json_path.read_text(encoding="utf-8")
    assert markdown == markdown_path.read_text(encoding="utf-8")


async def test_staging_instructions_point_at_project_mirror(db_engine, tmp_path) -> None:
    report = await _build(db_engine, tmp_path, "all_success")
    instructions = "\n".join(report.staging_instructions)
    mirror = f'"$DEVSANDBOX_DATA_ROOT/repos/{report.header.project_id}/repo.git"'

    assert f"git fetch {mirror}" in instructions
    assert "git merge --no-ff" in instructions


async def test_costs_are_split_and_budget_remaining(db_engine, tmp_path) -> None:
    report = await _build(db_engine, tmp_path, "all_success")
    assert report.costs.infra_usd == 0.25
    assert report.costs.inference_usd == 0.10
    assert report.costs.total_usd == 0.35
    assert report.costs.tokens == 1000
    assert report.costs.requests == 10
    assert report.costs.budget_tokens_remaining == 4000
    assert report.costs.budget_requests_remaining == 90


async def test_report_redacts_secrets_and_is_read_only(db_engine, tmp_path) -> None:
    settings = _settings(tmp_path)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    project_id = await seed_report_scenario(maker, "mixed_failures", make_spec(name="canary"))
    canary = "sk-abcdefghijklmnopqrstuvwxyz012345"
    async with maker() as session:
        result = await session.execute(
            select(TaskModel).where(TaskModel.id == "task-mixed_failures-test")
        )
        task = result.scalar_one()
        task.result_json = {"note": f"leaked key {canary}", "files_changed": []}
        await session.commit()

    first = await build_report(project_id, maker, settings=settings, clock=_clock)
    second = await build_report(project_id, maker, settings=settings, clock=_clock)
    assert first == second
    serialized = json.dumps(first.model_dump(mode="json"))
    markdown = render_markdown(first)
    assert canary not in serialized
    assert canary not in markdown
    assert "[REDACTED]" in serialized


async def test_conflict_and_policy_warnings_are_reported(db_engine, tmp_path) -> None:
    conflict = await _build(db_engine, tmp_path, "conflict")
    assert conflict.integrations[0].status == "conflict"
    assert conflict.integrations[0].conflicting_files == ["src/app.py"]
    assert any("rebase task merge" in action for action in conflict.outstanding.suggested_actions)

    policy = await _build(db_engine, tmp_path, "policy_denial")
    joined = " ".join(policy.warnings)
    assert "policy denial" in joined
    assert "tampering" in joined
    assert "baseline" in joined
    assert policy.outstanding.warnings == sorted(set(policy.warnings))
