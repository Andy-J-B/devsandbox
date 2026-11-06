"""API tests: real repositories with fake git/context/planner/engine (plan 10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
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
    assert engine.calls == 1


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
