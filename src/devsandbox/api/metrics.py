"""Prometheus exposition for the HTTP API (plan 10, item 19).

The metrics surface is intentionally small and dependency-free:

* HTTP request counters and duration summaries keyed by method, matched route
  template, and status code.  Route *templates* (``/v1/tasks/{task_id}``) are
  used instead of raw paths so a hostile or noisy client cannot create
  unbounded label cardinality.
* A few process/engine gauges supplied by the metrics endpoint at scrape time.

No project ids, task ids, tokens, or other user data become labels.  The
endpoint is unauthenticated like ``/healthz`` and ``/readyz``; bind the API to
loopback (the default) or keep ``/metrics`` on an internal-only ingress.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_HELP: dict[str, str] = {
    "devsandbox_info": "Static build information for the API process.",
    "devsandbox_uptime_seconds": "Seconds since the API application was created.",
    "devsandbox_http_requests_total": "Total HTTP requests handled, by route template and status.",
    "devsandbox_http_request_duration_seconds": (
        "HTTP request duration in seconds, by route template."
    ),
    "devsandbox_projects": "Projects grouped by persisted lifecycle status.",
    "devsandbox_tasks": "Tasks grouped by persisted lifecycle status.",
    "devsandbox_engine_running_tasks": "Attempts currently executing in this engine.",
    "devsandbox_engine_ready_tasks": "Tasks waiting in the ready queue.",
    "devsandbox_engine_deferred_tasks": "Tasks currently deferred by the scheduler.",
    "devsandbox_engine_attempts_total": "Attempts recorded for known projects.",
    "devsandbox_engine_paused": "1 when at least one project is paused, else 0.",
    "devsandbox_engine_lease_present": "1 when an orchestrator lease row exists, else 0.",
    "devsandbox_engine_budget_projects": "Projects with a recorded model budget.",
    "devsandbox_pressure": "Current host pressure state (one series set to 1).",
}

_TYPES: dict[str, str] = {
    "devsandbox_info": "gauge",
    "devsandbox_uptime_seconds": "gauge",
    "devsandbox_http_requests_total": "counter",
    "devsandbox_http_request_duration_seconds": "summary",
    "devsandbox_projects": "gauge",
    "devsandbox_tasks": "gauge",
    "devsandbox_engine_running_tasks": "gauge",
    "devsandbox_engine_ready_tasks": "gauge",
    "devsandbox_engine_deferred_tasks": "gauge",
    "devsandbox_engine_attempts_total": "gauge",
    "devsandbox_engine_paused": "gauge",
    "devsandbox_engine_lease_present": "gauge",
    "devsandbox_engine_budget_projects": "gauge",
    "devsandbox_pressure": "gauge",
}

_LABEL_VALUES = {
    "devsandbox_http_requests_total": ("method", "route", "status"),
    "devsandbox_http_request_duration_seconds": ("method", "route"),
}


def _escape(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(values: Mapping[str, str]) -> str:
    if not values:
        return ""
    rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(values.items()))
    return "{" + rendered + "}"


def _family_header(name: str) -> list[str]:
    kind = _TYPES[name]
    return [f"# HELP {name} {_HELP[name]}", f"# TYPE {name} {kind}"]


@dataclass
class _Summary:
    count: int = 0
    total: float = 0.0


class HttpMetrics:
    """Thread-safe request counters and duration summaries for one app."""

    def __init__(self, *, started_at: float | None = None) -> None:
        self._lock = threading.Lock()
        self._started_at = time.monotonic() if started_at is None else started_at
        self._requests: dict[tuple[str, str, str], int] = {}
        self._durations: dict[tuple[str, str], _Summary] = {}

    @property
    def uptime_seconds(self) -> float:
        """Seconds elapsed since this metrics registry was created."""
        return max(0.0, time.monotonic() - self._started_at)

    def observe(self, method: str, route: str, status: int, duration_seconds: float) -> None:
        """Record one completed request with bounded-cardinality labels."""
        method = method.upper()
        # Status is already bounded, but normalise odd values defensively.
        status_label = str(int(status))
        bounded_route = route if route.startswith("/") else "unmatched"
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            key = (method, bounded_route, status_label)
            self._requests[key] = self._requests.get(key, 0) + 1
            summary = self._durations.setdefault((method, bounded_route), _Summary())
            summary.count += 1
            summary.total += duration

    def render(self) -> str:
        """Render the request families in Prometheus text format."""
        with self._lock:
            requests = dict(self._requests)
            durations = dict(self._durations)
        lines = _family_header("devsandbox_http_requests_total")
        for (method, route, status), count in sorted(requests.items()):
            lines.append(
                f"devsandbox_http_requests_total"
                f"{_labels({'method': method, 'route': route, 'status': status})} {count}"
            )
        lines.extend(_family_header("devsandbox_http_request_duration_seconds"))
        for (method, route), summary in sorted(durations.items()):
            labels = {"method": method, "route": route}
            lines.append(
                f"devsandbox_http_request_duration_seconds_sum{_labels(labels)} {summary.total:.6f}"
            )
            lines.append(
                f"devsandbox_http_request_duration_seconds_count{_labels(labels)} {summary.count}"
            )
        return "\n".join(lines)


def render_metrics(
    *,
    version: str,
    uptime_seconds: float,
    http: HttpMetrics | None = None,
    gauges: Mapping[str, float] | None = None,
    grouped: Mapping[str, Mapping[str, float]] | None = None,
) -> str:
    """Build the complete exposition document.

    ``gauges`` holds unlabelled values; ``grouped`` holds named label families
    such as ``{"devsandbox_tasks": {"ready": 2}}``.
    """
    gauge_values = dict(gauges or {})
    grouped_values = dict(grouped or {})
    ordered = [
        "devsandbox_info",
        "devsandbox_uptime_seconds",
        *(name for name in grouped_values if name in _TYPES),
        *(name for name in gauge_values if name in _TYPES),
        "devsandbox_http_requests_total",
        "devsandbox_http_request_duration_seconds",
    ]
    # Keep a deterministic order even if callers provide unknown names.
    ordered = list(dict.fromkeys(ordered))
    lines: list[str] = []

    def emit_header(name: str) -> None:
        if name in _HELP:
            lines.extend(_family_header(name))

    for name in ordered:
        if name == "devsandbox_info":
            emit_header(name)
            lines.append(f"devsandbox_info{_labels({'version': version})} 1")
        elif name == "devsandbox_uptime_seconds":
            emit_header(name)
            lines.append(f"devsandbox_uptime_seconds {max(0.0, uptime_seconds):.3f}")
        elif name in grouped_values:
            emit_header(name)
            for label, value in sorted(grouped_values[name].items()):
                lines.append(f"{name}{_labels({'status': label})} {_format_number(value)}")
        elif name in gauge_values:
            emit_header(name)
            lines.append(f"{name} {_format_number(gauge_values[name])}")
        elif name == "devsandbox_http_requests_total":
            if http is not None:
                lines.append(http.render())
        elif name == "devsandbox_http_request_duration_seconds":
            # Rendered together with the request counter above.
            continue
    return "\n".join(lines) + "\n"


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    rendered = f"{value:.6f}".rstrip("0")
    return rendered.rstrip(".") or "0"


def exposition_names() -> Iterable[str]:
    """Names emitted by :func:`render_metrics` (useful for tests/docs)."""
    return tuple(_HELP)


__all__ = ["CONTENT_TYPE", "HttpMetrics", "exposition_names", "render_metrics"]
