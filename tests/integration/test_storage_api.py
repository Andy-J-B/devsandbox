"""API tests for storage reporting and retention garbage collection."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.api_helpers import TOKEN, make_app, make_container, make_settings

from devsandbox.config.settings import StorageSettings

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api(db_engine, tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    settings = make_settings(str(data_root))
    settings.storage = StorageSettings(
        workspace_retention_hours=1,
        log_retention_hours=1,
        validation_retention_hours=1,
        keep_latest_log_directories=0,
    )
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, data_root, maker


def _old_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("data\n", encoding="utf-8")
    stamp = time.time() - 10 * 3600
    os.utime(path, (stamp, stamp))
    os.utime(path.parent, (stamp, stamp))
    return path


async def test_storage_endpoint_reports_categories(api) -> None:
    client, data_root, _ = api
    _old_file(data_root / "logs" / "attempt-1" / "stdout.log")
    _old_file(data_root / "workspaces" / "p1" / "attempt-1" / "file.txt")

    response = await client.get("/v1/system/storage", headers=HEADERS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["data_root"] == str(data_root)
    assert payload["categories"]["logs"]["files"] == 1
    assert payload["categories"]["workspaces"]["files"] == 1
    assert payload["filesystem_total_bytes"] > 0


async def test_gc_endpoint_previews_then_removes_and_audits(api) -> None:
    client, data_root, maker = api
    log = _old_file(data_root / "logs" / "attempt-1" / "stdout.log")
    workspace = _old_file(data_root / "workspaces" / "p1" / "attempt-1" / "file.txt")

    preview = await client.post("/v1/system/gc?dry_run=true", headers=HEADERS)

    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["dry_run"] is True
    assert len(preview_payload["removed_paths"]) == 2
    assert log.exists()
    assert workspace.exists()

    collected = await client.post("/v1/system/gc", headers=HEADERS)

    assert collected.status_code == 200, collected.text
    collected_payload = collected.json()
    assert collected_payload["dry_run"] is False
    assert len(collected_payload["removed_paths"]) == 2
    assert not log.parent.exists()
    assert not workspace.parent.exists()

    from sqlalchemy import select

    from devsandbox.db.models import EventModel

    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(EventModel).where(EventModel.type == "STORAGE_GC_COMPLETED")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 2
    assert all(row.project_id == "system" for row in rows)


async def test_gc_endpoint_requires_auth(api) -> None:
    client, _, _ = api
    anonymous = await client.get("/v1/system/storage")
    assert anonymous.status_code == 401
