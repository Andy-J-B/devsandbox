"""Project reporting: read-only report builder and renderers (plan 10)."""

from __future__ import annotations

from .builder import build_report
from .models import (
    ProjectReport,
    ReportAttempt,
    ReportCheckResult,
    ReportCommit,
    ReportCosts,
    ReportCounts,
    ReportFailure,
    ReportHeader,
    ReportIntegration,
    ReportOutstanding,
    ReportResourceEvent,
    ReportTask,
    ReportTestAggregate,
)
from .render import render_html, render_json, render_markdown

__all__ = [
    "ProjectReport",
    "ReportAttempt",
    "ReportCheckResult",
    "ReportCommit",
    "ReportCosts",
    "ReportCounts",
    "ReportFailure",
    "ReportHeader",
    "ReportIntegration",
    "ReportOutstanding",
    "ReportResourceEvent",
    "ReportTask",
    "ReportTestAggregate",
    "build_report",
    "render_html",
    "render_json",
    "render_markdown",
]
