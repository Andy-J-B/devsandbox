"""Structured validation outcomes (plan 09).

The models in this module are deliberately independent of the orchestrator's
database rows: they are persisted verbatim in
``attempts.validation_report_json`` and can be replayed after a crash.  All
fields are JSON-serialisable (no ``Path`` objects), and every free-text field
is expected to have been passed through :func:`devsandbox.common.redaction.redact`
before it reaches this layer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CheckStatus = Literal["passed", "failed", "timeout", "error", "skipped"]
ReportStatus = Literal["passed", "failed", "error"]
Blame = Literal["agent", "baseline", "infrastructure", "unknown"]


class FailureKind(StrEnum):
    """Stable taxonomy for validation failures.

    The string values intentionally match the orchestrator's failure labels so
    plan 08 can classify a failed report without importing this package's
    internals.
    """

    TEST_FAILURE = "test_failure"
    LINT_FAILURE = "lint_failure"
    BUILD_FAILURE = "build_failure"
    TIMEOUT = "timeout"
    NO_CHANGES = "no_changes"
    RESTRICTED_PATH = "restricted_path"
    TAMPERING = "tampering"
    SIZE_LIMIT = "size_limit"
    INFRA_ERROR = "infra_error"
    FORBIDDEN_FILE = "forbidden_file"
    EXPECTED_FILES = "expected_files"
    UNKNOWN = "unknown"


class CheckResult(BaseModel):
    """Outcome of one validation check.

    ``check``/``passed``/``output`` are retained for backwards compatibility
    with the plan-01 protocol stub and the orchestrator.  Newer callers should
    read ``status`` plus the structured evidence fields.
    """

    check: str
    passed: bool = False
    output: str = ""
    status: CheckStatus = "failed"
    required: bool = True
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""
    tests_total: int | None = None
    tests_failed: int | None = None
    tests_skipped: int | None = None
    tests_errors: int | None = None
    failure_kind: FailureKind | None = None
    command: str = ""
    container_id: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _sync_passed(self) -> CheckResult:
        """Keep the legacy ``passed`` flag consistent with ``status``."""
        fields = self.model_fields_set
        if "status" not in fields and "passed" in fields:
            self.status = "passed" if self.passed else "failed"
        else:
            self.passed = self.status == "passed"
        return self

    @property
    def required_failure(self) -> bool:
        """Return whether this result is a hard failure (not skipped)."""
        return self.status in {"failed", "timeout", "error"}


class ValidationFailure(BaseModel):
    """A retry-ready description of one validation failure."""

    check: str
    kind: FailureKind
    summary: str
    log_tail: str = ""
    exit_code: int | None = None


class Evidence(BaseModel):
    """A command/exit-code/duration record for audit and reporting."""

    check: str
    command: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    container_id: str | None = None
    status: CheckStatus = "failed"


class TamperingFinding(BaseModel):
    """One suspicious test/CI modification detected in a change set."""

    kind: Literal[
        "deleted_test",
        "skipped_test",
        "removed_assert",
        "edited_test_config",
        "disabled_ci",
    ]
    path: str
    detail: str


class TamperingSignal(BaseModel):
    """Aggregated test-tampering heuristic result."""

    detected: bool = False
    action: Literal["fail", "warn", "ignore"] = "warn"
    findings: list[TamperingFinding] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        """Return a compact human-readable summary."""
        if not self.detected:
            return "no test-tampering signals"
        return "; ".join(f"{item.kind} in {item.path}" for item in self.findings)


class BaselineComparison(BaseModel):
    """Comparison of a candidate against the previous/baseline revision."""

    checked: bool = False
    baseline_revision: str | None = None
    baseline_status: Literal["passed", "failed", "error", "unknown"] = "unknown"
    baseline_failed_checks: list[str] = Field(default_factory=list)
    candidate_failed_checks: list[str] = Field(default_factory=list)
    newly_failing_checks: list[str] = Field(default_factory=list)
    pre_existing_failures: list[str] = Field(default_factory=list)
    blamed_on_baseline: bool = False
    detail: str = ""


class ValidationReport(BaseModel):
    """Result of a task or integration validation pass.

    ``status == "error"`` means the validation *infrastructure* failed (for
    example the sandbox could not start).  It must not be counted as an agent
    failure by plan 08's retry budget.
    """

    status: ReportStatus = "failed"
    checks: list[CheckResult] = Field(default_factory=list)
    failures: list[ValidationFailure] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    summary_for_retry: str = ""
    failure_kind: FailureKind | None = None
    tampering: TamperingSignal | None = None
    pre_existing_failure: bool = False
    baseline: BaselineComparison | None = None
    blame: Blame = "unknown"

    @property
    def passed(self) -> bool:
        """Return whether the report is a pass."""
        return self.status == "passed"

    @property
    def has_infra_error(self) -> bool:
        """Return whether any failure is classified as infrastructure."""
        if self.failure_kind is FailureKind.INFRA_ERROR:
            return True
        return any(failure.kind is FailureKind.INFRA_ERROR for failure in self.failures)


class BaselineReport(ValidationReport):
    """Validation of an unmodified base revision.

    Subclasses :class:`ValidationReport` so the orchestrator's baseline hook
    (``Callable[[RepoHandle, str, list[CheckSpec]], Awaitable[ValidationReport]]``)
    accepts it without a protocol change.
    """

    revision: str = ""
    failed_checks: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


__all__ = [
    "BaselineComparison",
    "BaselineReport",
    "Blame",
    "CheckResult",
    "CheckStatus",
    "Evidence",
    "FailureKind",
    "ReportStatus",
    "TamperingFinding",
    "TamperingSignal",
    "ValidationFailure",
    "ValidationReport",
]
