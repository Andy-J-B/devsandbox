"""CLI tests: Typer runner against the in-process ASGI app (plan 10)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.api_helpers import make_container, make_settings, spec_yaml
from tests.orchestrator_helpers import seed_attempt, seed_project
from typer.testing import CliRunner

from devsandbox.cli.app import app
from devsandbox.common.types import TaskStatus
from devsandbox.db.models import Base
from devsandbox.spec.models import load_spec

runner = CliRunner()


@pytest.fixture
def cli_settings(cli_db, tmp_path, monkeypatch):
    """Point the local CLI at the test database and fake external services."""
    settings = make_settings(str(tmp_path / "data"), database_url=cli_db)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("devsandbox.cli.client.get_settings", lambda: settings)

    def _fake_build_container(local_settings):
        engine = create_async_engine(local_settings.database_url)
        maker = async_sessionmaker(bind=engine, expire_on_commit=False)
        return make_container(settings=local_settings, session_factory=maker)

    monkeypatch.setattr("devsandbox.api.bootstrap.build_container", _fake_build_container)
    return settings


@pytest.fixture
def cli_db(postgres_url):
    """Create a clean schema for the CLI's own event loops."""

    async def _reset() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_reset())
    yield postgres_url
    asyncio.run(_reset())


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _json(result) -> object:
    return json.loads(result.stdout)


def _write_spec(tmp_path: Path, *, name: str = "cli-demo") -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(spec_yaml(name=name), encoding="utf-8")
    return path


def test_help_lists_commands(cli_settings) -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in (
        "submit",
        "plan",
        "show-plan",
        "approve",
        "start",
        "status",
        "tasks",
        "logs",
        "events",
        "report",
        "validate-spec",
    ):
        assert command in result.stdout


def test_validate_spec_offline(cli_settings, tmp_path) -> None:
    spec = _write_spec(tmp_path)
    result = _invoke("--json", "validate-spec", str(spec))
    assert result.exit_code == 0
    payload = _json(result)
    assert payload["name"] == "cli-demo"
    assert payload["repo"]["path"]

    broken = tmp_path / "broken.yaml"
    broken.write_text("name: broken\ngoals: nope\n", encoding="utf-8")
    failed = _invoke("validate-spec", str(broken))
    assert failed.exit_code == 2
    assert "invalid project spec" in failed.stdout or "invalid project spec" in failed.stderr


def test_full_supervised_flow(cli_settings, tmp_path) -> None:
    spec = _write_spec(tmp_path)

    submitted = _invoke("--local", "--json", "submit", str(spec))
    assert submitted.exit_code == 0, submitted.stdout
    project = _json(submitted)
    project_id = project["id"]

    planned = _invoke("--local", "--json", "plan", project_id)
    assert planned.exit_code == 0, planned.stdout
    plan = _json(planned)
    plan_id = plan["id"]
    assert plan["status"] == "proposed"

    show = _invoke("--local", "show-plan", project_id, "--md")
    assert show.exit_code == 0
    assert plan["tasks"][0]["key"] in show.stdout

    approved = _invoke("--local", "--json", "approve", plan_id)
    assert approved.exit_code == 0, approved.stdout
    assert _json(approved)["status"] == "active"

    started = _invoke("--local", "--json", "start", project_id)
    assert started.exit_code == 0, started.stdout
    assert _json(started)["status"] == "executing"

    status = _invoke("--local", "--json", "status", project_id)
    assert status.exit_code == 0
    assert _json(status)["status"] == "executing"

    tasks = _invoke("--local", "--json", "tasks", project_id)
    assert tasks.exit_code == 0
    task_rows = _json(tasks)
    assert len(task_rows) == len(plan["tasks"])

    events = _invoke("--local", "--json", "events", project_id)
    assert events.exit_code == 0
    assert any(event["type"] == "project.created" for event in _json(events))

    out = tmp_path / "report.md"
    report = _invoke("--local", "report", project_id, "--out", str(out))
    assert report.exit_code == 0, report.stdout
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# DevSandbox report")
    assert plan["tasks"][0]["key"] in text

    json_report = _invoke("--local", "--json", "report", project_id)
    assert json_report.exit_code == 0
    assert _json(json_report)["header"]["project_id"] == project_id


def test_cancelled_project_exits_non_zero(cli_settings, tmp_path) -> None:
    spec = _write_spec(tmp_path, name="cancel-demo")
    submitted = _invoke("--local", "--json", "submit", str(spec))
    project_id = _json(submitted)["id"]

    cancelled = _invoke("--local", "--json", "cancel", project_id)
    assert cancelled.exit_code == 0
    assert _json(cancelled)["status"] == "cancelled"

    status = _invoke("--local", "--json", "status", project_id)
    assert status.exit_code == 1
    assert _json(status)["status"] == "cancelled"


def test_api_errors_are_reported(cli_settings) -> None:
    result = _invoke("--local", "status", "missing-project")
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower()


def test_logs_show_redacted_attempt_output(cli_settings, cli_db, tmp_path) -> None:
    async def _seed() -> str:
        engine = create_async_engine(cli_db)
        maker = async_sessionmaker(bind=engine, expire_on_commit=False)
        spec = load_spec(spec_yaml(name="log-demo"))
        _, _, tasks = await seed_project(
            maker,
            spec,
            keys=["t-a"],
            statuses={"t-a": TaskStatus.RUNNING},
            project_status="executing",
        )
        task = tasks["t-a"]
        await seed_attempt(maker, task.id, attempt_id="att-cli")
        await engine.dispose()
        return task.id

    task_id = asyncio.run(_seed())
    log_dir = Path(cli_settings.data_root) / "logs" / "att-cli"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "stdout.log").write_text("agent output\napi_key=clisecretvalue\n", encoding="utf-8")

    result = _invoke("--local", "logs", task_id, "--attempt", "1", "--stream", "stdout")
    assert result.exit_code == 0, result.stdout
    assert "agent output" in result.stdout
    assert "clisecretvalue" not in result.stdout
    assert "[REDACTED]" in result.stdout
