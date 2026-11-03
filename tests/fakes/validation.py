"""Fake validator implementing the Validator protocol."""

from __future__ import annotations

from collections.abc import Callable

from devsandbox.validation.interfaces import CheckResult, ValidationReport


def _passed(label: str = "tests") -> ValidationReport:
    return ValidationReport(
        status="passed", checks=[CheckResult(check=label, passed=True, output="ok")]
    )


def _failed(summary: str, label: str = "tests") -> ValidationReport:
    return ValidationReport(
        status="failed",
        checks=[CheckResult(check=label, passed=False, output=summary)],
        summary=summary,
    )


class FakeValidator:
    """Scriptable fake validator.

    Reports can be queued per stream (task/integration), selected by callback,
    or supplied as the default.  This keeps engine tests expressive without
    coupling them to any real validation logic.
    """

    def __init__(
        self,
        *,
        task_reports: list[ValidationReport] | None = None,
        integration_reports: list[ValidationReport] | None = None,
        task_callback: Callable[[object, object, object], ValidationReport] | None = None,
        integration_callback: Callable[[object, str, object], ValidationReport] | None = None,
        default: ValidationReport | None = None,
    ) -> None:
        self._task_reports = list(task_reports or [])
        self._integration_reports = list(integration_reports or [])
        self._task_callback = task_callback
        self._integration_callback = integration_callback
        self._default = default or _passed()
        self.task_calls: list[tuple[object, object, object]] = []
        self.integration_calls: list[tuple[object, str, object]] = []
        # Historical aliases used by earlier plan tests.
        self.task_reports: list[ValidationReport] = []
        self.integration_reports: list[ValidationReport] = []

    @staticmethod
    def passed(summary: str = "") -> ValidationReport:
        """Build a passing report."""
        report = _passed()
        return report.model_copy(update={"summary": summary}) if summary else report

    @staticmethod
    def failed(summary: str = "validation failed") -> ValidationReport:
        """Build a failing report."""
        return _failed(summary)

    def queue_task(self, report: ValidationReport) -> None:
        """Queue a task validation report."""
        self._task_reports.append(report)

    def queue_integration(self, report: ValidationReport) -> None:
        """Queue an integration validation report."""
        self._integration_reports.append(report)

    async def validate_task(self, task, ws, cs) -> ValidationReport:
        """Validate a task change set."""
        self.task_calls.append((task, ws, cs))
        if self._task_callback is not None:
            report = self._task_callback(task, ws, cs)
        elif self._task_reports:
            report = self._task_reports.pop(0)
        else:
            report = self._default
        self.task_reports.append(report)
        return report

    async def validate_integration(self, repo, staging_commit: str, checks) -> ValidationReport:
        """Validate an integration commit."""
        self.integration_calls.append((repo, staging_commit, checks))
        if self._integration_callback is not None:
            report = self._integration_callback(repo, staging_commit, checks)
        elif self._integration_reports:
            report = self._integration_reports.pop(0)
        else:
            report = self._default
        self.integration_reports.append(report)
        return report
