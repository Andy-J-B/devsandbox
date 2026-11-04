"""Independent task and integration validation (plan 09).

The validator is deliberately paranoid: it never reads the agent's exit status,
never trusts an agent summary, and never reuses the agent's container.  It
first runs cheap host-side checks, then executes orchestrator-approved checks in
fresh containers, then applies test-tampering and baseline-attribution rules.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..common.time import utcnow
from ..common.types import CheckSpec, RepoHandle, TaskSpec
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings, get_settings
from ..gitmgr.interfaces import ChangeSet, GitManager, Workspace
from ..sandbox.interfaces import SandboxManager
from .config import ValidationConfig
from .diff import DiffProvider, DiffSummary, GitDiffProvider
from .models import (
    BaselineComparison,
    BaselineReport,
    CheckResult,
    Evidence,
    FailureKind,
    TamperingSignal,
    ValidationFailure,
    ValidationReport,
)
from .prechecks import run_prechecks
from .runner import CheckRunner
from .tampering import detect_tampering
from .workspace import (
    GitRevisionCheckout,
    RevisionCheckout,
    cleanup_scratch,
    copy_workspace,
    reset_scratch_workspace,
    scratch_workspace_path,
)

_RETRY_SUMMARY_BYTES = 6_000
_LOG_EXCERPT_BYTES = 1_500


def _cap(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[... truncated ...]"


def _failure_from_check(result: CheckResult) -> ValidationFailure:
    kind = result.failure_kind or FailureKind.UNKNOWN
    summary = result.reason or f"check {result.check} {result.status}"
    return ValidationFailure(
        check=result.check,
        kind=kind,
        summary=summary,
        log_tail=_cap(result.output, _LOG_EXCERPT_BYTES),
        exit_code=result.exit_code,
    )


def _evidence_from_results(results: list[CheckResult]) -> list[Evidence]:
    return [
        Evidence(
            check=result.check,
            command=result.command,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            container_id=result.container_id,
            status=result.status,
        )
        for result in results
    ]


def _required_failed_names(results: list[CheckResult]) -> list[str]:
    return [result.check for result in results if result.required and result.required_failure]


def _optional_failed_names(results: list[CheckResult]) -> list[str]:
    return [result.check for result in results if not result.required and result.required_failure]


def _summary_for_retry(
    task_label: str,
    failures: list[ValidationFailure],
    warnings: list[str],
    tampering: TamperingSignal | None,
) -> str:
    lines = [f"Validation failed for {task_label}."]
    if failures:
        lines.append("Failed checks / pre-checks:")
        for failure in failures:
            line = f"- {failure.check} [{failure.kind.value}]: {failure.summary}"
            if failure.exit_code is not None:
                line += f" (exit {failure.exit_code})"
            lines.append(line)
            if failure.log_tail:
                lines.append("  log tail:")
                for log_line in failure.log_tail.splitlines()[-20:]:
                    lines.append(f"  {log_line}")
    if tampering is not None and tampering.detected:
        lines.append("Test-tampering signals:")
        for finding in tampering.findings:
            lines.append(f"- {finding.kind} in {finding.path}: {finding.detail}")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
    lines.append(
        "Fix the underlying issue; do not weaken or delete tests. All required checks must pass."
    )
    return _cap("\n".join(lines), _RETRY_SUMMARY_BYTES)


def _report_from_results(
    *,
    task_label: str,
    results: list[CheckResult],
    config: ValidationConfig,
    warnings: list[str] | None = None,
    tampering: TamperingSignal | None = None,
) -> ValidationReport:
    warnings = list(warnings or [])
    required_failures = [
        result for result in results if result.required and result.required_failure
    ]
    optional_failures = [
        result for result in results if not result.required and result.required_failure
    ]
    failures = [_failure_from_check(result) for result in required_failures]
    if optional_failures:
        if config.fail_on_optional_failure:
            failures.extend(_failure_from_check(result) for result in optional_failures)
        else:
            warnings.extend(
                f"optional check failed: {result.check}: {result.reason or result.status}"
                for result in optional_failures
            )

    infra_results = [result for result in required_failures if result.status == "error"]
    agent_results = [
        result for result in required_failures if result.status in {"failed", "timeout"}
    ]
    if agent_results:
        status: str = "failed"
        failure_kind: FailureKind | None = agent_results[0].failure_kind
        blame = "agent"
    elif infra_results:
        status = "error"
        failure_kind = FailureKind.INFRA_ERROR
        blame = "infrastructure"
    elif failures:
        status = "failed"
        failure_kind = failures[0].kind
        blame = "agent"
    else:
        status = "passed"
        failure_kind = None
        blame = "unknown"

    if status == "passed":
        summary = f"validation passed ({len(results)} checks)"
        if warnings:
            summary += "; warnings: " + "; ".join(warnings[:5])
    elif status == "error":
        detail = "; ".join(f"{failure.check}: {failure.summary}" for failure in failures[:5])
        summary = f"validation infrastructure error: {detail or 'sandbox/check execution failed'}"
    else:
        detail = "; ".join(f"{failure.check}: {failure.summary}" for failure in failures[:5])
        summary = f"validation failed: {detail or 'one or more required checks failed'}"

    report = ValidationReport(
        status=status,  # type: ignore[arg-type]
        checks=results,
        failures=failures,
        evidence=_evidence_from_results(results),
        warnings=warnings,
        summary=summary,
        summary_for_retry=(
            ""
            if status == "passed"
            else (
                f"Validation infrastructure error: {summary}. "
                "Retry without code changes; this failure does not count against the task."
                if status == "error"
                else _summary_for_retry(task_label, failures, warnings, tampering)
            )
        ),
        failure_kind=failure_kind,
        tampering=tampering,
        blame=blame,  # type: ignore[arg-type]
    )
    return _apply_tampering(report, tampering, task_label)


def _apply_tampering(
    report: ValidationReport,
    tampering: TamperingSignal | None,
    task_label: str = "the task",
) -> ValidationReport:
    if tampering is None or not tampering.detected:
        return report
    if report.status == "error":
        # Infrastructure errors must stay infrastructure errors: the agent is
        # not blamed and the retry budget is not consumed.
        return report
    if tampering.action == "warn":
        report.warnings.append(f"test-tampering signal: {tampering.summary}")
        return report
    if tampering.action == "ignore":
        return report
    failure = ValidationFailure(
        check="test-tampering",
        kind=FailureKind.TAMPERING,
        summary=tampering.summary,
        log_tail="\n".join(
            f"{finding.kind} {finding.path}: {finding.detail}" for finding in tampering.findings
        ),
    )
    report.failures.append(failure)
    report.status = "failed"
    report.failure_kind = FailureKind.TAMPERING
    report.blame = "agent"
    report.summary = f"validation failed: test-tampering detected ({tampering.summary})"
    report.summary_for_retry = _summary_for_retry(
        task_label, report.failures, report.warnings, tampering
    )
    return report


def _error_report(
    summary: str,
    *,
    failures: list[ValidationFailure] | None = None,
    warnings: list[str] | None = None,
    tampering: TamperingSignal | None = None,
) -> ValidationReport:
    failure_list = list(failures or [])
    if not failure_list:
        failure_list = [
            ValidationFailure(
                check="validation",
                kind=FailureKind.INFRA_ERROR,
                summary=summary,
            )
        ]
    return ValidationReport(
        status="error",
        failures=failure_list,
        warnings=list(warnings or []),
        summary=f"validation infrastructure error: {summary}",
        summary_for_retry=(
            f"Validation infrastructure error: {summary}. Retry without code changes."
        ),
        failure_kind=FailureKind.INFRA_ERROR,
        tampering=tampering,
        blame="infrastructure",
    )


class ValidatorImpl:
    """Independent validator implementing the plan-01 ``Validator`` protocol."""

    def __init__(
        self,
        sandbox: SandboxManager,
        git: GitManager,
        *,
        config: ValidationConfig | None = None,
        settings: DevSandboxSettings | None = None,
        data_root: str | Path | None = None,
        default_checks: list[CheckSpec] | None = None,
        checkout: RevisionCheckout | None = None,
        diff_provider: DiffProvider | None = None,
        baseline: BaselineReport | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings or get_settings()
        self._config = config or ValidationConfig.from_settings(self._settings)
        self._policy = SecurityPolicy(self._settings)
        self._data_root = Path(data_root or self._settings.data_root)
        self._sandbox = sandbox
        self._git = git
        self._default_checks = list(default_checks or [])
        self._checkout = checkout or GitRevisionCheckout()
        self._diff = diff_provider or GitDiffProvider()
        self._runner = CheckRunner(sandbox, self._config, policy=self._policy, clock=clock)
        self._baseline = baseline
        self._clock = clock

    @property
    def baseline(self) -> BaselineReport | None:
        """Return the most recently recorded baseline report."""
        return self._baseline

    def set_baseline(self, report: BaselineReport | None) -> None:
        """Install a baseline report produced by :meth:`run_baseline`."""
        self._baseline = report

    # -- task validation ---------------------------------------------------

    async def validate_task(self, task: TaskSpec, ws: Workspace, cs: ChangeSet) -> ValidationReport:
        """Validate one task change set without trusting the agent."""
        pre = await run_prechecks(task, ws, cs, self._git, self._config, diff_provider=self._diff)
        diff = pre.diff or DiffSummary(available=False, error="diff unavailable")
        tampering = detect_tampering(cs, diff, self._config)

        if pre.infra_error:
            return _error_report(
                pre.failures[0].summary if pre.failures else "pre-check failed",
                failures=pre.failures,
                warnings=pre.warnings,
                tampering=tampering,
            )
        if pre.failures:
            report = ValidationReport(
                status="failed",
                failures=pre.failures,
                warnings=pre.warnings,
                summary="validation failed: "
                + "; ".join(f"{failure.check}: {failure.summary}" for failure in pre.failures),
                summary_for_retry=_summary_for_retry(
                    f"task {task.id}", pre.failures, pre.warnings, tampering
                ),
                failure_kind=pre.failures[0].kind,
                tampering=tampering,
                blame="agent",
            )
            return _apply_tampering(report, tampering, f"task {task.id}")

        checks = list(task.checks) or list(self._default_checks)
        config_error = self._checks_config_error(checks)
        if config_error is not None:
            return _error_report(config_error, warnings=pre.warnings, tampering=tampering)

        scratch = scratch_workspace_path(self._data_root, str(ws.attempt_id))
        try:
            await copy_workspace(ws.path, scratch)
            results = await self._runner.run_checks(
                checks,
                scratch,
                attempt_id=str(ws.attempt_id),
                phase="task",
                task_id=task.id,
            )
        except Exception as exc:
            return _error_report(
                f"could not run task validation: {type(exc).__name__}: {exc}",
                warnings=pre.warnings,
                tampering=tampering,
            )
        finally:
            if not self._config.keep_scratch_on_failure:
                cleanup_scratch(self._data_root, str(ws.attempt_id))

        report = _report_from_results(
            task_label=f"task {task.id}",
            results=results,
            config=self._config,
            warnings=pre.warnings,
            tampering=tampering,
        )
        return self._apply_baseline(report, results, f"task {task.id}")

    # -- integration validation -------------------------------------------

    async def validate_integration(
        self, repo: RepoHandle, staging_commit: str, checks: list[CheckSpec]
    ) -> ValidationReport:
        """Validate a candidate staging commit before it is committed."""
        config_error = self._checks_config_error(checks)
        if config_error is not None:
            return _error_report(config_error)

        run_id = f"{repo.project_id}-int-{staging_commit[:12]}"
        scratch = reset_scratch_workspace(self._data_root, run_id)
        try:
            await self._checkout.checkout(repo, staging_commit, scratch)
            results = await self._runner.run_checks(
                checks,
                scratch,
                attempt_id=run_id,
                phase="integration",
                project_id=repo.project_id,
            )
        except Exception as exc:
            return _error_report(
                f"could not validate integration candidate {staging_commit[:12]}: "
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            if not self._config.keep_scratch_on_failure:
                cleanup_scratch(self._data_root, run_id)

        report = _report_from_results(
            task_label=f"integration candidate {staging_commit[:12]}",
            results=results,
            config=self._config,
        )
        if report.status == "failed":
            comparison = await self._compare_integration_baseline(
                repo, staging_commit, checks, results
            )
            report = self._apply_comparison(
                report,
                results,
                comparison,
                task_label=f"integration candidate {staging_commit[:12]}",
            )
        return report

    # -- baseline ----------------------------------------------------------

    async def run_baseline(
        self,
        repo: RepoHandle,
        revision: str,
        checks: list[CheckSpec] | None = None,
    ) -> BaselineReport:
        """Run configured checks on an unmodified revision and record them."""
        selected = list(checks) if checks is not None else list(self._default_checks)
        started = self._clock().isoformat()
        config_error = self._checks_config_error(selected)
        if config_error is not None:
            report = BaselineReport(
                status="error",
                revision=revision,
                failed_checks=[],
                summary=f"validation infrastructure error: {config_error}",
                summary_for_retry=config_error,
                failure_kind=FailureKind.INFRA_ERROR,
                blame="infrastructure",
                started_at=started,
                finished_at=self._clock().isoformat(),
            )
            self._baseline = report
            return report

        run_id = f"{repo.project_id}-baseline-{revision[:12]}"
        scratch = reset_scratch_workspace(self._data_root, run_id)
        try:
            await self._checkout.checkout(repo, revision, scratch)
            results = await self._runner.run_checks(
                selected,
                scratch,
                attempt_id=run_id,
                phase="baseline",
                project_id=repo.project_id,
            )
        except Exception as exc:
            report = BaselineReport(
                status="error",
                revision=revision,
                failed_checks=[],
                summary=f"baseline infrastructure error: {type(exc).__name__}: {exc}",
                summary_for_retry=f"baseline infrastructure error: {exc}",
                failure_kind=FailureKind.INFRA_ERROR,
                blame="infrastructure",
                started_at=started,
                finished_at=self._clock().isoformat(),
            )
            self._baseline = report
            return report
        finally:
            if not self._config.keep_scratch_on_failure:
                cleanup_scratch(self._data_root, run_id)

        base = _report_from_results(
            task_label=f"baseline {revision[:12]}",
            results=results,
            config=self._config,
        )
        failed_checks = _required_failed_names(results)
        report = BaselineReport(
            **base.model_dump(),
            revision=revision,
            failed_checks=failed_checks,
            started_at=started,
            finished_at=self._clock().isoformat(),
        )
        self._baseline = report
        return report

    # -- attribution -------------------------------------------------------

    def _apply_baseline(
        self,
        report: ValidationReport,
        results: list[CheckResult],
        task_label: str = "the task",
    ) -> ValidationReport:
        if report.status != "failed" or self._baseline is None:
            return report
        failed = _required_failed_names(results)
        if not failed:
            return report
        comparison = BaselineComparison(
            checked=True,
            baseline_revision=self._baseline.revision or None,
            baseline_status=self._baseline.status,
            baseline_failed_checks=list(self._baseline.failed_checks),
            candidate_failed_checks=failed,
            newly_failing_checks=[
                name for name in failed if name not in self._baseline.failed_checks
            ],
            pre_existing_failures=[name for name in failed if name in self._baseline.failed_checks],
            blamed_on_baseline=all(name in self._baseline.failed_checks for name in failed),
            detail="compared task checks against the recorded baseline",
        )
        return self._apply_comparison(report, results, comparison, task_label=task_label)

    def _apply_comparison(
        self,
        report: ValidationReport,
        results: list[CheckResult],
        comparison: BaselineComparison,
        *,
        task_label: str = "the integration candidate",
    ) -> ValidationReport:
        report.baseline = comparison
        if not comparison.checked:
            return report
        if not comparison.candidate_failed_checks:
            return report
        if comparison.baseline_status == "error":
            report.warnings.append(
                "baseline comparison unavailable (baseline checks errored); "
                "cannot attribute pre-existing failures"
            )
            return report
        if comparison.pre_existing_failures:
            report.pre_existing_failure = True
            report.warnings.append(
                "pre-existing failures (also failing on baseline): "
                + ", ".join(comparison.pre_existing_failures)
            )
        if comparison.newly_failing_checks:
            report.blame = "agent"
            report.failures = [
                failure
                for failure in report.failures
                if failure.check in comparison.newly_failing_checks
            ]
            report.failure_kind = (
                report.failures[0].kind if report.failures else report.failure_kind
            )
            report.summary = "validation failed: new failures introduced: " + ", ".join(
                comparison.newly_failing_checks
            )
            report.summary_for_retry = _summary_for_retry(
                task_label,
                report.failures,
                report.warnings,
                report.tampering,
            )
            return report
        # Only pre-existing failures: do not blame the agent.
        report.status = "passed"
        report.failure_kind = None
        report.blame = "baseline"
        report.failures = []
        report.summary = "validation passed with pre-existing failures: " + ", ".join(
            comparison.pre_existing_failures
        )
        report.summary_for_retry = ""
        return report

    async def _compare_integration_baseline(
        self,
        repo: RepoHandle,
        staging_commit: str,
        checks: list[CheckSpec],
        candidate_results: list[CheckResult],
    ) -> BaselineComparison:
        candidate_failed = _required_failed_names(candidate_results)
        try:
            previous = await self._git.staging_head(repo)
        except Exception as exc:
            return BaselineComparison(
                checked=False,
                candidate_failed_checks=candidate_failed,
                detail=f"could not read previous staging head: {exc}",
            )
        if previous is None or previous == staging_commit:
            return BaselineComparison(
                checked=False,
                candidate_failed_checks=candidate_failed,
                detail="no previous staging head to compare against",
            )

        run_id = f"{repo.project_id}-base-{previous[:12]}"
        scratch = reset_scratch_workspace(self._data_root, run_id)
        try:
            await self._checkout.checkout(repo, previous, scratch)
            base_results = await self._runner.run_checks(
                checks,
                scratch,
                attempt_id=run_id,
                phase="baseline",
                project_id=repo.project_id,
            )
        except Exception as exc:
            return BaselineComparison(
                checked=True,
                baseline_revision=previous,
                baseline_status="error",
                candidate_failed_checks=candidate_failed,
                detail=f"baseline validation errored: {type(exc).__name__}: {exc}",
            )
        finally:
            if not self._config.keep_scratch_on_failure:
                cleanup_scratch(self._data_root, run_id)

        baseline_failed = _required_failed_names(base_results)
        baseline_status: str = "passed"
        if any(result.status == "error" for result in base_results):
            baseline_status = "error"
        elif baseline_failed:
            baseline_status = "failed"
        newly_failing = [name for name in candidate_failed if name not in baseline_failed]
        pre_existing = [name for name in candidate_failed if name in baseline_failed]
        return BaselineComparison(
            checked=True,
            baseline_revision=previous,
            baseline_status=baseline_status,  # type: ignore[arg-type]
            baseline_failed_checks=baseline_failed,
            candidate_failed_checks=candidate_failed,
            newly_failing_checks=newly_failing,
            pre_existing_failures=pre_existing,
            blamed_on_baseline=not newly_failing,
            detail="compared candidate against the previous staging head",
        )

    def _checks_config_error(self, checks: list[CheckSpec]) -> str | None:
        if not checks:
            return "no validation checks configured; refusing to mark work valid"
        if self._config.require_required_check and not any(check.required for check in checks):
            return "no required validation check configured; refusing to mark work valid"
        return None


__all__ = ["ValidatorImpl"]
