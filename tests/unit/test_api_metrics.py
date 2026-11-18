"""Unit tests for the dependency-free Prometheus exposition (plan 10, item 19)."""

from __future__ import annotations

from devsandbox.api.metrics import HttpMetrics, render_metrics


def test_http_metrics_are_aggregated_by_route_template() -> None:
    metrics = HttpMetrics(started_at=0.0)
    metrics.observe("GET", "/v1/tasks/{task_id}", 200, 0.1)
    metrics.observe("get", "/v1/tasks/{task_id}", 200, 0.2)
    metrics.observe("GET", "/v1/tasks/{task_id}", 404, 0.05)

    rendered = metrics.render()

    assert (
        'devsandbox_http_requests_total{method="GET",route="/v1/tasks/{task_id}",status="200"} 2'
        in rendered
    )
    assert (
        'devsandbox_http_requests_total{method="GET",route="/v1/tasks/{task_id}",status="404"} 1'
        in rendered
    )
    assert (
        'devsandbox_http_request_duration_seconds_count{method="GET",route="/v1/tasks/{task_id}"} 3'
        in rendered
    )
    assert "0.350000" in rendered


def test_label_values_are_escaped_and_deterministically_ordered() -> None:
    metrics = HttpMetrics(started_at=0.0)
    metrics.observe("GET", '/v1/odd"route\n', 500, 0.0)

    rendered = metrics.render()

    assert 'route="/v1/odd\\"route\\n"' in rendered
    assert 'status="500"' in rendered


def test_render_metrics_includes_static_and_dynamic_families() -> None:
    metrics = HttpMetrics(started_at=0.0)
    metrics.observe("POST", "/v1/projects", 201, 0.25)

    rendered = render_metrics(
        version="9.9.9",
        uptime_seconds=12.5,
        http=metrics,
        gauges={
            "devsandbox_engine_running_tasks": 2,
            "devsandbox_engine_paused": 0,
        },
        grouped={
            "devsandbox_projects": {"executing": 1},
            "devsandbox_tasks": {"ready": 3, "running": 1},
            "devsandbox_pressure": {"ok": 1},
        },
    )

    assert 'devsandbox_info{version="9.9.9"} 1' in rendered
    assert "devsandbox_uptime_seconds 12.500" in rendered
    assert 'devsandbox_projects{status="executing"} 1' in rendered
    assert 'devsandbox_tasks{status="ready"} 3' in rendered
    assert 'devsandbox_pressure{status="ok"} 1' in rendered
    assert "devsandbox_engine_running_tasks 2" in rendered
    assert "devsandbox_engine_paused 0" in rendered
    assert 'devsandbox_http_requests_total{method="POST",route="/v1/projects",status="201"} 1' in (
        rendered
    )
    # Every non-summary family carries HELP/TYPE metadata.
    assert "# TYPE devsandbox_projects gauge" in rendered
    assert "# TYPE devsandbox_http_requests_total counter" in rendered
    assert rendered.endswith("\n")


def test_unknown_routes_fall_back_to_a_bounded_label() -> None:
    metrics = HttpMetrics(started_at=0.0)
    metrics.observe("GET", "", 404, 0.0)

    rendered = metrics.render()

    assert 'route="unmatched"' in rendered
