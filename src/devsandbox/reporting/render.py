"""Report renderers: stable JSON, Markdown, and optional HTML-lite (plan 10)."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence

from .models import ProjectReport, ReportAttempt, ReportTask


def _fmt(value: object | None, *, empty: str = "—") -> str:
    if value is None or value == "":
        return empty
    return str(value)


def _fmt_dt(value: object | None) -> str:
    return _fmt(value.isoformat() if hasattr(value, "isoformat") else value)


def _table(headers: list[str], rows: Sequence[Sequence[object | None]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def _attempts_markdown(attempts: list[ReportAttempt]) -> str:
    if not attempts:
        return "_No attempts._"
    rows = [
        [
            attempt.number,
            attempt.status,
            attempt.outcome,
            _fmt_dt(attempt.started_at),
            f"{attempt.duration_seconds:.1f}s",
            attempt.head_commit[:12] if attempt.head_commit else None,
            attempt.validation_status,
        ]
        for attempt in attempts
    ]
    return _table(
        ["#", "Status", "Outcome", "Started", "Duration", "Head", "Validation"],
        rows,
    )


def _checks_markdown(task: ReportTask) -> str:
    if not task.checks:
        return ""
    rows = [
        [
            check.name,
            check.status,
            "yes" if check.required else "no",
            f"{check.duration_seconds:.1f}s",
            check.exit_code,
            check.summary[:120],
        ]
        for check in task.checks
    ]
    return "\n\n" + _table(["Check", "Status", "Required", "Duration", "Exit", "Summary"], rows)


def _task_markdown(task: ReportTask) -> str:
    lines = [
        f"### {task.key} — {task.title}",
        "",
        f"- **Status:** `{task.status}`",
        f"- **Attempts:** {task.attempt_count} / retries {task.max_retries}",
        f"- **Duration:** {task.duration_seconds:.1f}s",
    ]
    if task.blocked_by:
        lines.append(f"- **Depends on:** {', '.join(task.blocked_by)}")
    if task.blocked_reason:
        lines.append(f"- **Blocked reason:** {task.blocked_reason}")
    if task.files_changed:
        lines.append(f"- **Files changed:** {', '.join(task.files_changed)}")
    if task.commits:
        commits = ", ".join(
            f"`{commit.sha[:12]}` {commit.subject}".strip() for commit in task.commits
        )
        lines.append(f"- **Commits:** {commits}")
    if task.failure is not None:
        lines.append(f"- **Failure:** `{task.failure.kind}` — {task.failure.summary}")
        if task.failure.log_tail_pointer:
            lines.append(f"- **Log pointer:** `{task.failure.log_tail_pointer}`")
    lines.extend(["", "**Attempts**", "", _attempts_markdown(task.attempts)])
    checks = _checks_markdown(task)
    if checks:
        lines.extend(["", "**Validation checks**", checks])
    return "\n".join(lines)


def render_markdown(report: ProjectReport) -> str:
    """Render a deterministic human-readable Markdown report."""
    header = report.header
    lines = [
        f"# DevSandbox report — {header.name}",
        "",
        _table(
            ["Field", "Value"],
            [
                ["Project id", header.project_id],
                ["Mode", header.mode],
                ["Status", header.status],
                ["Spec hash", header.spec_hash],
                ["Repository", header.repo_source],
                ["Main branch", header.main_branch],
                ["Base commit", header.base_commit],
                ["Staging branch", header.staging_branch],
                ["Staging head", header.staging_head],
                ["Plan", f"v{header.plan_version} ({header.plan_id})" if header.plan_id else None],
                ["Started", _fmt_dt(header.started_at)],
                ["Finished", _fmt_dt(header.finished_at)],
                ["Generated", _fmt_dt(header.generated_at)],
            ],
        ),
        "",
        "## Summary",
        "",
        _table(
            [
                "Total",
                "Completed",
                "Failed",
                "Blocked",
                "Cancelled",
                "Pending",
                "Ready",
                "Running",
                "Retry wait",
            ],
            [
                [
                    report.counts.total,
                    report.counts.completed,
                    report.counts.failed,
                    report.counts.blocked,
                    report.counts.cancelled,
                    report.counts.pending,
                    report.counts.ready,
                    report.counts.running,
                    report.counts.retry_wait,
                ]
            ],
        ),
        "",
        "## Tasks",
        "",
    ]
    if report.tasks:
        lines.append("\n\n".join(_task_markdown(task) for task in report.tasks))
    else:
        lines.append("_No tasks._")

    lines.extend(["", "## Integration history", ""])
    if report.integrations:
        lines.append(
            _table(
                ["Task", "Attempt", "Status", "Before", "After", "Conflicts", "Detail"],
                [
                    [
                        item.task_id,
                        item.attempt_id,
                        item.status,
                        item.staging_before,
                        item.staging_after,
                        ", ".join(item.conflicting_files),
                        item.detail,
                    ]
                    for item in report.integrations
                ],
            )
        )
    else:
        lines.append("_No integrations._")

    lines.extend(["", "## Test results", ""])
    if report.test_results:
        lines.append(
            _table(
                ["Check", "Passed", "Failed", "Timeout", "Error", "Skipped", "Total"],
                [
                    [
                        item.check,
                        item.passed,
                        item.failed,
                        item.timeout,
                        item.error,
                        item.skipped,
                        item.total,
                    ]
                    for item in report.test_results
                ],
            )
        )
    else:
        lines.append("_No parsed test results._")

    costs = report.costs
    lines.extend(
        [
            "",
            "## Costs",
            "",
            _table(
                [
                    "Infra USD",
                    "Inference USD",
                    "Total USD",
                    "Tokens",
                    "Requests",
                    "Token budget left",
                    "Request budget left",
                ],
                [
                    [
                        f"{costs.infra_usd:.4f}",
                        f"{costs.inference_usd:.4f}",
                        f"{costs.total_usd:.4f}",
                        costs.tokens,
                        costs.requests,
                        costs.budget_tokens_remaining,
                        costs.budget_requests_remaining,
                    ]
                ],
            ),
            "",
            "## Resource / limit events",
            "",
        ]
    )
    if report.resource_events:
        lines.append(
            _table(
                ["Type", "Task", "Attempt", "At", "Detail"],
                [
                    [
                        event.type,
                        event.task_id,
                        event.attempt_id,
                        _fmt_dt(event.at),
                        event.detail,
                    ]
                    for event in report.resource_events
                ],
            )
        )
    else:
        lines.append("_No resource or limit events._")

    outstanding = report.outstanding
    lines.extend(["", "## Outstanding work", ""])
    if outstanding.unresolved_failures:
        lines.append("**Unresolved failures**\n")
        lines.extend(f"- {item}" for item in outstanding.unresolved_failures)
    if outstanding.blocked_tasks:
        lines.append("\n**Blocked tasks**\n")
        lines.extend(f"- {item}" for item in outstanding.blocked_tasks)
    if outstanding.suggested_actions:
        lines.append("\n**Suggested actions**\n")
        lines.extend(f"- {item}" for item in outstanding.suggested_actions)
    if outstanding.warnings:
        lines.append("\n**Warnings**\n")
        lines.extend(f"- {item}" for item in outstanding.warnings)
    if not any(
        [
            outstanding.unresolved_failures,
            outstanding.blocked_tasks,
            outstanding.suggested_actions,
            outstanding.warnings,
        ]
    ):
        lines.append("_No outstanding work._")

    lines.extend(["", "## Staging branch", ""])
    lines.extend(f"- `{item}`" for item in report.staging_instructions)
    lines.append("")
    return "\n".join(lines)


def render_json(report: ProjectReport, *, indent: int = 2) -> str:
    """Render stable pretty JSON for the API/CLI."""
    return json.dumps(report.model_dump(mode="json"), indent=indent, sort_keys=True)


def render_html(report: ProjectReport) -> str:
    """Render a small self-contained HTML page (optional HTML-lite renderer)."""
    body = html.escape(render_markdown(report))
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>DevSandbox report — {html.escape(report.header.name)}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}"
        "pre{white-space:pre-wrap;line-height:1.45}</style></head>"
        f"<body><pre>{body}</pre></body></html>"
    )


__all__ = ["render_html", "render_json", "render_markdown"]
