"""Fake validator implementing the Validator protocol.

Scriptable per task key / integration commit, with queue and callback fallbacks.
Plan 09 extends the plan-01 stub with explicit keyed outcomes and structured
failure/error helpers so orchestrator and validation tests stay expressive.
"""

from __future__ import annotations

from collections.abc import Callable

from devsandbox.validation.interfaces import CheckResult, ValidationReport
from devsandbox.validation.models import FailureKind, ValidationFailure


def _passed(label: str = "tests") -> ValidationReport:
    return ValidationReport(
        status="passed",
        checks=[CheckResult(check=label, passed=True, output="ok")],
        summary="validation passed",
        blame="agent",
    )


def _failed(summary: str, label: str = "tests") -> ValidationReport:
    return ValidationReport(
        status="failed",
        checks=[CheckResult(check=label, passed=False, output=summary)],
        failures=[ValidationFailure(check=label, kind=FailureKind.TEST_FAILURE, summary=summary)],
        summary=summary,
        summary_for_retry=summary,
        blame="agent",
    )


class FakeValidator:
    """Scriptable fake validator.

    Reports can be selected by task id / integration commit, queued per stream,
    selected by callback, or supplied as the default.  This keeps engine tests
    expressive without coupling them to real validation logic.
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
        self.task_outcomes: dict[str, ValidationReport] = {}
        self.integration_outcomes: dict[str, ValidationReport] = {}
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

    @staticmethod
    def failed_with_kind(summary: str, kind: FailureKind, label: str = "tests") -> ValidationReport:
        """Build a failing report with an explicit failure kind."""
        report = _failed(summary, label)
        report.failure_kind = kind
        report.failures = [ValidationFailure(check=label, kind=kind, summary=summary)]
        return report

    @staticmethod
    def error(summary: str = "validation infrastructure error") -> ValidationReport:
        """Build an infrastructure-error report."""
        return ValidationReport(
            status="error",
            checks=[CheckResult(check="sandbox", status="error", reason=summary)],
            failures=[
                ValidationFailure(check="sandbox", kind=FailureKind.INFRA_ERROR, summary=summary)
            ],
            summary=summary,
            summary_for_retry=summary,
            failure_kind=FailureKind.INFRA_ERROR,
            blame="infrastructure",
        )

    def queue_task(self, report: ValidationReport) -> None:
        """Queue a task validation report."""
        self._task_reports.append(report)

    def queue_integration(self, report: ValidationReport) -> None:
        """Queue an integration validation report."""
        self._integration_reports.append(report)

    def set_task_report(self, key: str, report: ValidationReport) -> None:
        """Script a report for a task id/key."""
        self.task_outcomes[key] = report

    def set_integration_report(self, key: str, report: ValidationReport) -> None:
        """Script a report for an integration commit."""
        self.integration_outcomes[key] = report

    async def validate_task(self, task, ws, cs) -> ValidationReport:
        """Validate a task change set."""
        self.task_calls.append((task, ws, cs))
        key = str(getattr(task, "id", task))
        if key in self.task_outcomes:
            report = self.task_outcomes[key]
        elif self._task_callback is not None:
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
        if staging_commit in self.integration_outcomes:
            report = self.integration_outcomes[staging_commit]
        elif self._integration_callback is not None:
            report = self._integration_callback(repo, staging_commit, checks)
        elif self._integration_reports:
            report = self._integration_reports.pop(0)
        else:
            report = self._default
        self.integration_reports.append(report)
        return report
