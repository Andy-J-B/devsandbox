"""API tests: real repositories with fake git/context/planner/engine (plan 10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.api_helpers import (
    TOKEN,
    FakeEngine,
    make_app,
    make_container,
    make_settings,
    spec_yaml,
)
from tests.orchestrator_helpers import seed_attempt, seed_project

from devsandbox.common.types import TaskStatus
from devsandbox.db.models import AttemptModel
from devsandbox.spec.models import load_spec

HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api(db_engine, tmp_path):
    """An httpx client bound to an in-process app with a real test database."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    settings = make_settings(str(data_root))
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    container = make_container(settings=settings, session_factory=maker)
    app = make_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, container


def _error_code(response: httpx.Response) -> str:
    payload = response.json()
    return str(payload["error"]["code"])


async def _submit(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, Any]:
    response = await client.post(
        "/v1/projects",
        json={"spec_yaml": spec_yaml(**kwargs)},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_auth_required_and_error_envelope(api) -> None:
    client, _ = api
    anonymous = await client.get("/v1/projects")
    assert anonymous.status_code == 401
    assert _error_code(anonymous) == "unauthorized"
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"

    wrong = await client.get("/v1/projects", headers={"Authorization": "Bearer wrong-token"})
    assert wrong.status_code == 401

    allowed = await client.get("/v1/projects", headers=HEADERS)
    assert allowed.status_code == 200
    assert allowed.json() == []
    assert allowed.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Basic abcdef",
        "Bearer ",
        "Bearer wrong-token",
        "bearer wrong-token",
    ],
)
async def test_auth_failures_are_401_without_echoing_the_token(api, authorization) -> None:
    client, _ = api
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.get("/v1/projects", headers=headers)

    assert response.status_code == 401
    assert _error_code(response) == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["X-Request-ID"]
    assert "wrong-token" not in response.text
    assert "Retry-After" not in response.headers


async def test_bearer_scheme_is_case_insensitive(api) -> None:
    client, _ = api
    response = await client.get("/v1/projects", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 200


async def test_token_rotation_accepts_primary_and_old_tokens(db_engine, tmp_path) -> None:
    settings = make_settings(
        str(tmp_path / "data"),
        token="new-token",
        additional_tokens=["old-token-1", "old-token-2"],
    )
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for token in ("new-token", "old-token-1", "old-token-2"):
            response = await client.get(
                "/v1/projects", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200, token

        retired = settings.model_copy(
            update={"api": settings.api.model_copy(update={"additional_tokens": []})}
        )
        app = make_app(make_container(settings=retired, session_factory=maker))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            old = await client.get("/v1/projects", headers={"Authorization": "Bearer old-token-1"})
            assert old.status_code == 401
            current = await client.get(
                "/v1/projects", headers={"Authorization": "Bearer new-token"}
            )
            assert current.status_code == 200


async def test_rate_limit_blocks_expensive_requests_and_sets_retry_after(
    db_engine, tmp_path
) -> None:
    settings = make_settings(str(tmp_path / "data"), rate_limit=2)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(2):
            allowed = await client.post("/v1/projects/missing/plans", headers=HEADERS)
            assert allowed.status_code != 429

        limited = await client.post("/v1/projects/missing/plans", headers=HEADERS)
        assert limited.status_code == 429
        assert _error_code(limited) == "rate_limited"
        assert limited.headers["Retry-After"] == "60"
        assert limited.headers["X-Request-ID"]

        # Cheap reads are not rate-limited by the expensive-endpoint bucket.
        listed = await client.get("/v1/projects", headers=HEADERS)
        assert listed.status_code == 200


async def test_rate_limit_is_per_token_and_never_consumed_by_auth_failures(
    db_engine, tmp_path
) -> None:
    settings = make_settings(
        str(tmp_path / "data"),
        token="primary-token",
        additional_tokens=["rotation-token"],
        rate_limit=1,
    )
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(5):
            denied = await client.post(
                "/v1/projects/missing/plans",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
            assert denied.status_code == 401

        primary = await client.post(
            "/v1/projects/missing/plans",
            headers={"Authorization": "Bearer primary-token"},
        )
        assert primary.status_code != 429

        old = await client.post(
            "/v1/projects/missing/plans",
            headers={"Authorization": "Bearer rotation-token"},
        )
        assert old.status_code != 429

        exhausted = await client.post(
            "/v1/projects/missing/plans",
            headers={"Authorization": "Bearer primary-token"},
        )
        assert exhausted.status_code == 429


async def test_health_and_readiness(api) -> None:
    client, container = api
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    # No docker probe configured: readiness must fail closed.
    not_ready = await client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["checks"] == {"database": True, "docker": False}

    object.__setattr__(container, "docker_ping", lambda: True)
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


async def test_metrics_are_prometheus_text_and_do_not_leak_ids(api) -> None:
    client, container = api

    # The scrape endpoint itself is unauthenticated, like the health probes.
    scrape = await client.get("/metrics")
    assert scrape.status_code == 200
    assert scrape.headers["content-type"].startswith("text/plain")
    assert "devsandbox_info" in scrape.text
    assert "devsandbox_uptime_seconds" in scrape.text

    project = await _submit(client, name="metrics-demo")
    listed = await client.get("/v1/projects", headers=HEADERS)
    assert listed.status_code == 200

    scrape = await client.get("/metrics")
    assert scrape.status_code == 200
    body = scrape.text
    # Request counters use route templates, not raw paths, so project ids and
    # other user data never become labels.
    assert 'route="/v1/projects"' in body
    assert 'method="GET"' in body
    assert 'status="200"' in body
    assert project["id"] not in body
    # Persisted state is exported as aggregate status gauges.
    assert 'devsandbox_projects{status="loaded"} 1' in body
    assert "devsandbox_engine_running_tasks" in body
    assert "devsandbox_pressure{status=" in body

    # Aggregate gauges update from the database on each scrape.
    async with container.session_factory() as session:
        await session.execute(
            text("UPDATE projects SET status = 'executing' WHERE id = :id"),
            {"id": project["id"]},
        )
        await session.commit()
    scrape = await client.get("/metrics")
    assert 'devsandbox_projects{status="executing"} 1' in scrape.text
    assert 'devsandbox_projects{status="loaded"} 0' not in scrape.text


async def test_metrics_endpoint_can_be_disabled(db_engine, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "data"))
    settings = settings.model_copy(
        update={"api": settings.api.model_copy(update={"metrics_enabled": False})}
    )
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/metrics")
    assert response.status_code == 404


async def test_raw_yaml_submission_and_list(api) -> None:
    client, _ = api
    response = await client.post(
        "/v1/projects",
        content=spec_yaml(name="raw-yaml").encode(),
        headers={**HEADERS, "Content-Type": "application/yaml"},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["name"] == "raw-yaml"
    assert project["status"] == "loaded"
    assert project["mode"] == "supervised"

    listed = await client.get("/v1/projects", headers=HEADERS)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [project["id"]]
    assert listed.json()[0]["counts"]["total"] == 0


async def test_invalid_spec_returns_field_errors(api) -> None:
    client, _ = api
    response = await client.post(
        "/v1/projects",
        json={"spec_yaml": "name: broken\ngoals: not-a-list\n"},
        headers=HEADERS,
    )
    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["error"]["code"] == "invalid_spec"
    assert payload["error"]["details"]
    assert any(entry["loc"][-1] == "goals" for entry in payload["error"]["details"])


async def test_spec_size_limit(db_engine, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "data"), max_spec_bytes=32)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/projects",
            json={"spec_yaml": spec_yaml()},
            headers=HEADERS,
        )
    assert response.status_code == 413
    assert _error_code(response) == "spec_too_large"


async def test_supervised_flow_end_to_end(api) -> None:
    client, _ = api
    project = await _submit(client)
    project_id = project["id"]

    plan_response = await client.post(f"/v1/projects/{project_id}/plans", headers=HEADERS)
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    plan_id = plan["id"]
    assert plan["status"] == "proposed"
    assert plan["version"] == 1
    assert plan["tasks"]

    detail = await client.get(f"/v1/projects/{project_id}", headers=HEADERS)
    assert detail.json()["status"] == "awaiting_approval"
    assert detail.json()["active_plan_id"] == plan_id

    plans = await client.get(f"/v1/projects/{project_id}/plans", headers=HEADERS)
    assert [row["id"] for row in plans.json()] == [plan_id]
    markdown = await client.get(f"/v1/projects/{project_id}/plans?format=markdown", headers=HEADERS)
    assert markdown.status_code == 200
    assert "Plan" in markdown.text
    assert plan["tasks"][0]["key"] in markdown.text

    plan_detail = await client.get(f"/v1/plans/{plan_id}", headers=HEADERS)
    assert plan_detail.json()["id"] == plan_id

    approved = await client.post(
        f"/v1/plans/{plan_id}/approve", headers=HEADERS, json={"approver": "tester"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "active"

    executing = await client.get(f"/v1/projects/{project_id}", headers=HEADERS)
    assert executing.json()["status"] == "executing"

    started = await client.post(f"/v1/projects/{project_id}/start", headers=HEADERS)
    assert started.status_code == 200
    assert started.json()["status"] == "executing"

    tasks = await client.get(f"/v1/projects/{project_id}/tasks", headers=HEADERS)
    assert tasks.status_code == 200
    assert len(tasks.json()) == len(plan["tasks"])
    task = tasks.json()[0]
    assert task["status"] == "pending"
    assert task["dependencies"] == []

    task_detail = await client.get(f"/v1/tasks/{task['id']}", headers=HEADERS)
    assert task_detail.status_code == 200
    assert task_detail.json()["key"] == task["key"]
    assert task_detail.json()["success_criteria"]

    report = await client.get(f"/v1/projects/{project_id}/report", headers=HEADERS)
    assert report.status_code == 200, report.text
    report_body = report.json()
    assert report_body["header"]["project_id"] == project_id
    assert report_body["header"]["status"] == "executing"
    assert report_body["counts"]["total"] == len(plan["tasks"])
    assert report_body["staging_instructions"]

    report_md = await client.get(
        f"/v1/projects/{project_id}/report?format=markdown", headers=HEADERS
    )
    assert report_md.status_code == 200
    assert report_md.text.startswith("# DevSandbox report")
    report_html = await client.get(f"/v1/projects/{project_id}/report?format=html", headers=HEADERS)
    assert report_html.status_code == 200
    assert "<html" in report_html.text.lower()


async def test_plan_edit_revalidates(api) -> None:
    client, _ = api
    project = await _submit(client, name="edit-demo")
    plan = (await client.post(f"/v1/projects/{project['id']}/plans", headers=HEADERS)).json()
    task = plan["tasks"][0]
    updated_task = {**task, "title": "Edited title"}
    patched = await client.patch(
        f"/v1/plans/{plan['id']}",
        headers=HEADERS,
        json={"upsert_tasks": [updated_task]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["tasks"][0]["title"] == "Edited title"


async def test_reject_plan_keeps_project_awaiting_approval(api) -> None:
    client, _ = api
    project = await _submit(client, name="reject-demo")
    plan = (await client.post(f"/v1/projects/{project['id']}/plans", headers=HEADERS)).json()
    rejected = await client.post(
        f"/v1/plans/{plan['id']}/reject",
        headers=HEADERS,
        json={"reason": "needs smaller tasks"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "needs smaller tasks"

    detail = await client.get(f"/v1/projects/{project['id']}", headers=HEADERS)
    assert detail.json()["status"] == "awaiting_approval"

    # A rejected plan is not active, so execution cannot start.
    start = await client.post(f"/v1/projects/{project['id']}/start", headers=HEADERS)
    assert start.status_code == 409
    assert _error_code(start) == "invalid_transition"


async def test_state_conflicts_return_409(api) -> None:
    client, _ = api
    project = await _submit(client, name="conflict-demo")
    start = await client.post(f"/v1/projects/{project['id']}/start", headers=HEADERS)
    assert start.status_code == 409

    plan = (await client.post(f"/v1/projects/{project['id']}/plans", headers=HEADERS)).json()
    approved = await client.post(f"/v1/plans/{plan['id']}/approve", headers=HEADERS)
    assert approved.status_code == 200
    again = await client.post(f"/v1/plans/{plan['id']}/approve", headers=HEADERS)
    assert again.status_code == 409


async def test_autonomous_mode_is_policy_gated(db_engine, tmp_path) -> None:
    settings = make_settings(str(tmp_path / "data"), allow_autonomous=False)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    app = make_app(make_container(settings=settings, session_factory=maker))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        project = await _submit(client, name="policy-demo")
        denied = await client.post(
            f"/v1/projects/{project['id']}/mode",
            headers=HEADERS,
            json={"mode": "autonomous"},
        )
    assert denied.status_code == 403
    assert _error_code(denied) == "policy_violation"


async def test_autonomous_submission_executes_without_approval(api) -> None:
    client, _ = api
    project = await _submit(client, name="auto-demo", mode="autonomous")
    plan = (await client.post(f"/v1/projects/{project['id']}/plans", headers=HEADERS)).json()
    assert plan["status"] == "active"
    detail = await client.get(f"/v1/projects/{project['id']}", headers=HEADERS)
    assert detail.json()["status"] == "executing"


async def test_manual_retry_only_reopens_blocked_tasks(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="retry-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["blocked", "failed"],
        statuses={"blocked": TaskStatus.BLOCKED, "failed": TaskStatus.FAILED},
        project_status="executing",
    )
    blocked = tasks["blocked"]
    failed = tasks["failed"]
    async with container.session_factory() as session:
        stored = await session.get(type(blocked), blocked.id)
        assert stored is not None
        stored.blocked_reason = "dependency failed"
        await session.commit()

    response = await client.post(f"/v1/tasks/{blocked.id}/retry", headers=HEADERS, json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"

    refused = await client.post(f"/v1/tasks/{failed.id}/retry", headers=HEADERS, json={})
    assert refused.status_code == 409
    assert _error_code(refused) == "task_not_retryable"

    # Budget reset is explicit and audited through the same transition call.
    await seed_project(
        container.session_factory,
        spec,
        project_id="proj-2",
        plan_id="plan-2",
        keys=["blocked-2"],
        statuses={"blocked-2": TaskStatus.BLOCKED},
        project_status="executing",
    )
    reset = await client.post(
        "/v1/tasks/plan-2-blocked-2/retry",
        headers=HEADERS,
        json={"reset_budget": True},
    )
    assert reset.status_code == 200
    assert reset.json()["reset_budget"] is True


async def test_logs_are_tail_limited_redacted_and_traversal_safe(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="logs-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["t-a"],
        statuses={"t-a": TaskStatus.RUNNING},
        project_status="executing",
    )
    task = tasks["t-a"]
    await seed_attempt(container.session_factory, task.id, attempt_id="att-logs")

    log_dir = Path(container.settings.data_root) / "logs" / "att-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "stdout.log").write_text(
        "line one\napi_key=supersecretvalue\nline three\n", encoding="utf-8"
    )
    (log_dir / "stderr.log").write_text("warning only\n", encoding="utf-8")

    response = await client.get(
        f"/v1/tasks/{task.id}/attempts/1/logs?stream=stdout&tail=2",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    chunk = response.json()
    assert chunk["lines"] == 2
    assert chunk["truncated"] is True
    assert "supersecretvalue" not in chunk["content"]
    assert "[REDACTED]" in chunk["content"]

    stderr = await client.get(f"/v1/tasks/{task.id}/attempts/1/logs?stream=stderr", headers=HEADERS)
    assert stderr.status_code == 200
    assert stderr.json()["content"] == "warning only"

    async with container.session_factory() as session:
        attempt = await session.get(AttemptModel, "att-logs")
        assert attempt is not None
        attempt.agent_log_path = "/etc/passwd"
        await session.commit()
    traversal = await client.get(
        f"/v1/tasks/{task.id}/attempts/1/logs?stream=agent", headers=HEADERS
    )
    assert traversal.status_code == 400
    assert _error_code(traversal) == "unsafe_log_path"


async def test_task_cancel_is_idempotent_and_blocks_dependents(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="cancel-task-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["t-a", "t-b"],
        dependencies={"t-b": ["t-a"]},
        statuses={"t-a": TaskStatus.RUNNING, "t-b": TaskStatus.READY},
        project_status="executing",
    )
    task_id = tasks["t-a"].id
    dependent_id = tasks["t-b"].id
    engine = container.engine
    assert engine is not None
    engine.running_task_ids.add(task_id)

    cancelled = await client.post(f"/v1/tasks/{task_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["task_id"] == task_id
    assert body["status"] == "cancelled"
    assert body["aborted"] is True
    assert body["blocked_dependents"] == [dependent_id]
    assert engine.cancelled_tasks == [task_id]

    task = await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)
    assert task.json()["status"] == "cancelled"
    dependent = await client.get(f"/v1/tasks/{dependent_id}", headers=HEADERS)
    assert dependent.json()["status"] == "blocked"
    assert "cancelled" in (dependent.json()["blocked_reason"] or "")

    again = await client.post(f"/v1/tasks/{task_id}/cancel", headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["already_cancelled"] is True
    assert again.json()["aborted"] is False

    events = await client.get("/v1/projects/proj-1/events", headers=HEADERS)
    assert any(event["type"] == "task.cancelled" for event in events.json())


async def test_task_cancel_rejects_terminal_tasks_and_unknown_ids(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="cancel-terminal-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["done", "paused"],
        statuses={"done": TaskStatus.COMPLETED, "paused": TaskStatus.BLOCKED},
        project_status="executing",
    )

    for key in ("done", "paused"):
        response = await client.post(f"/v1/tasks/{tasks[key].id}/cancel", headers=HEADERS)
        assert response.status_code == 409, (key, response.text)
        assert _error_code(response) == "invalid_transition"

    missing = await client.post("/v1/tasks/missing/cancel", headers=HEADERS)
    assert missing.status_code == 404


async def test_project_pause_resume_and_cancel_controls(api) -> None:
    client, container = api
    project = await _submit(client, name="controls-demo")
    project_id = project["id"]
    plan = (await client.post(f"/v1/projects/{project_id}/plans", headers=HEADERS)).json()
    approved = await client.post(f"/v1/plans/{plan['id']}/approve", headers=HEADERS)
    assert approved.status_code == 200

    paused = await client.post(f"/v1/projects/{project_id}/pause", headers=HEADERS)
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    # Pause is idempotent and keeps the project paused.
    assert (await client.post(f"/v1/projects/{project_id}/pause", headers=HEADERS)).json()[
        "status"
    ] == "paused"

    resumed = await client.post(f"/v1/projects/{project_id}/resume", headers=HEADERS)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "executing"
    # Resume of an executing project is a no-op, not an error.
    assert (await client.post(f"/v1/projects/{project_id}/resume", headers=HEADERS)).json()[
        "status"
    ] == "executing"

    cancelled = await client.post(f"/v1/projects/{project_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert container.engine is not None
    assert container.engine.cancelled_projects == [project_id]
    again = await client.post(f"/v1/projects/{project_id}/cancel", headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"

    tasks = await client.get(f"/v1/projects/{project_id}/tasks", headers=HEADERS)
    assert tasks.json()
    assert all(task["status"] == "cancelled" for task in tasks.json())


async def test_task_pause_and_resume_hold_is_durable(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="pause-task-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["t-a"],
        statuses={"t-a": TaskStatus.READY},
        project_status="executing",
    )
    task_id = tasks["t-a"].id

    paused = await client.post(f"/v1/tasks/{task_id}/pause", headers=HEADERS)
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    assert paused.json()["changed"] is True

    again = await client.post(f"/v1/tasks/{task_id}/pause", headers=HEADERS)
    assert again.status_code == 200
    assert again.json()["changed"] is False

    detail = await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)
    assert detail.json()["paused"] is True
    assert detail.json()["status"] == "ready"
    listed = await client.get("/v1/projects/proj-1/tasks", headers=HEADERS)
    assert listed.json()[0]["paused"] is True
    report = await client.get("/v1/projects/proj-1/report", headers=HEADERS)
    assert report.json()["tasks"][0]["paused"] is True

    resumed = await client.post(f"/v1/tasks/{task_id}/resume", headers=HEADERS)
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert (await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)).json()["paused"] is False

    events = await client.get("/v1/projects/proj-1/events", headers=HEADERS)
    event_types = {event["type"] for event in events.json()}
    assert {"task.paused", "task.resumed"} <= event_types


async def test_task_pause_only_accepts_queued_states(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="pause-state-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["running", "done"],
        statuses={"running": TaskStatus.RUNNING, "done": TaskStatus.COMPLETED},
        project_status="executing",
    )

    for key in ("running", "done"):
        response = await client.post(f"/v1/tasks/{tasks[key].id}/pause", headers=HEADERS)
        assert response.status_code == 409, (key, response.text)
        assert _error_code(response) == "invalid_transition"

    # Unknown tasks are 404 for both pause and resume.
    assert (await client.post("/v1/tasks/missing/pause", headers=HEADERS)).status_code == 404
    assert (await client.post("/v1/tasks/missing/resume", headers=HEADERS)).status_code == 404


async def test_project_cancel_aborts_in_flight_attempts(api) -> None:
    client, container = api
    spec = load_spec(spec_yaml(name="cancel-running-demo"))
    _, _, tasks = await seed_project(
        container.session_factory,
        spec,
        keys=["t-a"],
        statuses={"t-a": TaskStatus.RUNNING},
        project_status="executing",
    )
    engine = container.engine
    assert engine is not None
    engine.running_projects[tasks["t-a"].id] = "proj-1"

    response = await client.post("/v1/projects/proj-1/cancel", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert engine.cancelled_projects == ["proj-1"]
    task = await client.get(f"/v1/tasks/{tasks['t-a'].id}", headers=HEADERS)
    assert task.json()["status"] == "cancelled"


async def test_events_and_sse(api) -> None:
    client, _ = api
    project = await _submit(client, name="events-demo")
    events = await client.get(f"/v1/projects/{project['id']}/events", headers=HEADERS)
    assert events.status_code == 200
    assert events.json()
    first_id = events.json()[0]["id"]

    since = await client.get(
        f"/v1/projects/{project['id']}/events?since={first_id}", headers=HEADERS
    )
    assert since.status_code == 200

    async with client.stream(
        "GET",
        f"/v1/projects/{project['id']}/events/stream?once=true",
        headers=HEADERS,
    ) as stream:
        assert stream.status_code == 200
        body = (await stream.aread()).decode()
    assert "event: project.created" in body
    assert "data: " in body


async def test_system_status(api) -> None:
    client, container = api
    engine = container.engine
    assert isinstance(engine, FakeEngine)
    response = await client.get("/v1/system/status", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"]["lease_holder"] == "test-holder"
    assert body["engine"]["budget_usage"] == {"proj-1": 1234}
    assert body["resources"]["cpu_count"] == 4
    assert body["api"]["auth_required"] is True
    assert body["audit"] is None
    assert engine.calls == 1


async def test_system_status_reports_audit_shipper(api) -> None:
    _client, container = api

    class _Shipper:
        def status(self) -> dict[str, Any]:
            return {"enabled": True, "shipped": 7, "last_event_id": "evt-7"}

    import dataclasses

    app = make_app(dataclasses.replace(container, audit_shipper=_Shipper()))  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client2:
        response = await client2.get("/v1/system/status", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["audit"] == {
        "enabled": True,
        "shipped": 7,
        "last_event_id": "evt-7",
    }


async def test_unknown_ids_return_404(api) -> None:
    client, _ = api
    project = await client.get("/v1/projects/missing", headers=HEADERS)
    assert project.status_code == 404
    assert _error_code(project) == "project_not_found"
    task = await client.get("/v1/tasks/missing", headers=HEADERS)
    assert task.status_code == 404
    plan = await client.get("/v1/plans/missing", headers=HEADERS)
    assert plan.status_code == 404


async def test_no_policy_mutation_routes(api) -> None:
    _client, container = api
    app = make_app(container)

    def _paths(routes: Any, prefix: str = "") -> set[str]:
        found: set[str] = set()
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                context = route.include_context
                found |= _paths(
                    route.original_router.routes, prefix + str(getattr(context, "prefix", ""))
                )
            elif hasattr(route, "path"):
                found.add(prefix + str(route.path))
        return found

    forbidden = ("security", "policy", "config", "limits", "push", "main-merge")
    paths = _paths(app.routes)
    for path in paths:
        lowered = path.lower()
        assert not any(token in lowered for token in forbidden), path
    # The only escalation call is explicit and policy-checked.
    assert "/v1/projects/{project_id}/mode" in paths
