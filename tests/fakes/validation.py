"""Fake validator implementing the Validator protocol."""

from __future__ import annotations

from devsandbox.validation.interfaces import CheckResult, ValidationReport


class FakeValidator:
    """Scriptable fake validator."""

    def __init__(self) -> None:
        self.task_reports: list[ValidationReport] = []
        self.integration_reports: list[ValidationReport] = []

    async def validate_task(self, task, ws, cs) -> ValidationReport:
        """Validate a task change set."""
        report = ValidationReport(
            status="passed", checks=[CheckResult(check="tests", passed=True, output="ok")]
        )
        self.task_reports.append(report)
        return report

    async def validate_integration(self, repo, staging_commit: str, checks) -> ValidationReport:
        """Validate an integration commit."""
        report = ValidationReport(
            status="passed", checks=[CheckResult(check="tests", passed=True, output="ok")]
        )
        self.integration_reports.append(report)
        return report
