"""Failure classification, retry budgets, backoff, and early-stop detection.

The classifier is pure apart from the injectable clock/jitter.  The engine
persists the resulting :class:`FailureRecord` and performs the actual state
transition, so retry policy remains testable without a database or Docker.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..agents.interfaces import AgentRunResult
from ..common.errors import DevSandboxError, PolicyViolation
from ..common.time import utcnow
from ..gitmgr.interfaces import IntegrationResult
from ..validation.interfaces import ValidationReport

_HEX_RE = re.compile(r"\b[0-9a-f]{7,64}\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")
_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")


class FailureKind(StrEnum):
    """Stable failure taxonomy used by retry accounting and diagnostics."""

    AGENT_ERROR = "agent_error"
    AGENT_CRASH = "agent_crash"
    CONTAINER_FAILURE = "container_failure"
    TIMEOUT = "timeout"
    OOM = "oom"
    VALIDATION_FAILURE = "validation_failure"
    NO_CHANGES = "no_changes"
    RESTRICTED_PATH = "restricted_path"
    TAMPERING = "tampering"
    INFRA_ERROR = "infra_error"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    GIT_CONFLICT = "git_conflict"
    INTERRUPTED_INTEGRATION = "interrupted_integration"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_VIOLATION = "policy_violation"
    REPEATED_FAILURE = "repeated_failure"
    UNKNOWN = "unknown"


class FailureAction(StrEnum):
    """What the orchestrator should do with a failure."""

    RETRY = "retry"
    FAIL = "fail"
    DEFER = "defer"
    NEEDS_ATTENTION = "needs_attention"


class FailureRecord(BaseModel):
    """A classified failure plus the evidence needed for diagnostics."""

    model_config = ConfigDict(frozen=True)

    kind: FailureKind
    retryable: bool
    counts_against_budget: bool
    summary: str
    signature: str = ""
    diagnostics_path: str | None = None
    validation_kind: str | None = None
    requires_attention: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class FailureDecision(BaseModel):
    """A failure record plus the chosen action and backoff deadline."""

    model_config = ConfigDict(frozen=True)

    action: FailureAction
    record: FailureRecord
    retry_not_before: datetime | None = None
    reason: str = ""

    @property
    def retry(self) -> bool:
        """Return whether the task should enter ``RETRY_WAIT``."""
        return self.action is FailureAction.RETRY


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def normalize_failure_text(text: str) -> str:
    """Normalize volatile values so identical failures share a signature."""
    normalized = _HEX_RE.sub("<sha>", text)
    normalized = _PATH_RE.sub("<path>", normalized)
    normalized = _NUMBER_RE.sub("<n>", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip().lower()
    return normalized


def failure_signature(kind: FailureKind, summary: str) -> str:
    """Return a stable signature for ``kind`` plus a normalized summary."""
    digest = hashlib.sha256(normalize_failure_text(summary).encode("utf-8")).hexdigest()[:16]
    return f"{kind.value}:{digest}"


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class FailureClassifier:
    """Classify failures and decide retry/stop using policy-clamped budgets."""

    def __init__(
        self,
        *,
        max_infra_failures: int = 3,
        repeat_failure_threshold: int = 2,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 300.0,
        backoff_jitter_ratio: float = 0.2,
        clock: Callable[[], datetime] = utcnow,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.max_infra_failures = max_infra_failures
        self.repeat_failure_threshold = max(2, repeat_failure_threshold)
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.backoff_jitter_ratio = backoff_jitter_ratio
        self._clock = clock
        self._jitter = jitter or (lambda: 1.0)

    # -- construction of raw records --------------------------------------

    def from_agent_result(self, result: AgentRunResult) -> FailureRecord | None:
        """Return a record for a failed agent result, or ``None`` on success."""
        status = result.exit_status
        if status == "exited_ok":
            return None
        mapping: dict[str, tuple[FailureKind, bool]] = {
            "exited_error": (FailureKind.AGENT_ERROR, True),
            "crashed": (FailureKind.AGENT_CRASH, True),
            "timeout": (FailureKind.TIMEOUT, True),
            "oom": (FailureKind.OOM, True),
            "sandbox_error": (FailureKind.CONTAINER_FAILURE, False),
        }
        kind, counts = mapping.get(status, (FailureKind.UNKNOWN, False))
        summary = f"agent {status} (exit code {result.exit_code})"
        return self.make_record(
            kind,
            summary,
            counts_against_budget=counts,
            retryable=kind is not FailureKind.UNKNOWN,
            detail={
                "exit_status": status,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
        )

    def from_exception(self, exc: BaseException) -> FailureRecord:
        """Classify an exception raised by a leaf module."""
        if isinstance(exc, PolicyViolation):
            return self.make_record(
                FailureKind.POLICY_VIOLATION,
                f"policy violation: {exc}",
                retryable=False,
                counts_against_budget=True,
                requires_attention=True,
            )
        if isinstance(exc, TimeoutError):
            return self.make_record(
                FailureKind.TIMEOUT, f"timeout: {exc}", retryable=True, counts_against_budget=True
            )
        if isinstance(exc, MemoryError):
            return self.make_record(
                FailureKind.OOM, f"out of memory: {exc}", retryable=True, counts_against_budget=True
            )
        if isinstance(exc, DevSandboxError):
            return self.make_record(
                FailureKind.INFRA_ERROR,
                f"{type(exc).__name__}: {exc}",
                retryable=True,
                counts_against_budget=False,
            )
        return self.make_record(
            FailureKind.UNKNOWN,
            f"{type(exc).__name__}: {exc}",
            retryable=False,
            counts_against_budget=True,
            requires_attention=True,
        )

    def from_validation(
        self, report: ValidationReport, *, validation_kind: str | None = None
    ) -> FailureRecord:
        """Classify a failed validation report."""
        text = report.summary or "validation failed"
        lower = text.lower()
        kind = FailureKind.VALIDATION_FAILURE
        retryable = True
        counts = True
        attention = False
        if validation_kind is not None:
            kind = self._kind_from_validation_label(validation_kind)
        elif "no changes" in lower or "empty change" in lower:
            kind = FailureKind.NO_CHANGES
        elif "restricted" in lower or "forbidden" in lower:
            kind = FailureKind.RESTRICTED_PATH
        elif "tamper" in lower or "test deletion" in lower:
            kind = FailureKind.TAMPERING
        if kind in {FailureKind.RESTRICTED_PATH, FailureKind.POLICY_VIOLATION}:
            retryable = False
            attention = True
        report_text = report.summary or (
            "; ".join(
                f"{check.check}: {'pass' if check.passed else 'fail'}" for check in report.checks
            )
            or "validation failed"
        )
        return self.make_record(
            kind,
            report_text,
            retryable=retryable,
            counts_against_budget=counts,
            validation_kind=validation_kind,
            requires_attention=attention,
            detail={"checks": [check.model_dump(mode="json") for check in report.checks]},
        )

    def from_integration(self, result: IntegrationResult) -> FailureRecord:
        """Classify a non-integrated integration result."""
        if result.status == "conflict":
            files = ", ".join(result.conflicting_files) or "unknown files"
            return self.make_record(
                FailureKind.GIT_CONFLICT,
                f"staging merge conflict: {files}",
                retryable=True,
                counts_against_budget=True,
                detail={"conflicting_files": list(result.conflicting_files)},
            )
        if result.status == "noop":
            return self.make_record(
                FailureKind.NO_CHANGES,
                "integration produced no changes",
                retryable=True,
                counts_against_budget=True,
            )
        return self.make_record(
            FailureKind.INTERRUPTED_INTEGRATION,
            result.detail or f"integration {result.status}",
            retryable=True,
            counts_against_budget=False,
            detail={"status": result.status},
        )

    def policy_denied(self, summary: str, *, detail: dict[str, Any] | None = None) -> FailureRecord:
        """Build a policy-denied record (never retried)."""
        return self.make_record(
            FailureKind.POLICY_VIOLATION,
            summary,
            retryable=False,
            counts_against_budget=True,
            requires_attention=True,
            detail=detail or {},
        )

    def budget_exceeded(self, summary: str) -> FailureRecord:
        """Build a budget-deferral record (defer, never fail)."""
        return self.make_record(
            FailureKind.BUDGET_EXCEEDED,
            summary,
            retryable=True,
            counts_against_budget=False,
        )

    @staticmethod
    def _kind_from_validation_label(label: str) -> FailureKind:
        normalized = label.strip().lower()
        mapping = {
            "no_changes": FailureKind.NO_CHANGES,
            "restricted_path": FailureKind.RESTRICTED_PATH,
            "tampering": FailureKind.TAMPERING,
            "infra_error": FailureKind.INFRA_ERROR,
            "timeout": FailureKind.TIMEOUT,
            "test_failure": FailureKind.VALIDATION_FAILURE,
            "lint_failure": FailureKind.VALIDATION_FAILURE,
            "build_failure": FailureKind.VALIDATION_FAILURE,
            "size_limit": FailureKind.VALIDATION_FAILURE,
        }
        return mapping.get(normalized, FailureKind.VALIDATION_FAILURE)

    # -- records -----------------------------------------------------------

    @staticmethod
    def make_record(
        kind: FailureKind,
        summary: str,
        *,
        retryable: bool,
        counts_against_budget: bool,
        diagnostics_path: str | None = None,
        validation_kind: str | None = None,
        requires_attention: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> FailureRecord:
        """Build a record with a normalized signature."""
        return FailureRecord(
            kind=kind,
            retryable=retryable,
            counts_against_budget=counts_against_budget,
            summary=summary,
            signature=failure_signature(kind, summary),
            diagnostics_path=diagnostics_path,
            validation_kind=validation_kind,
            requires_attention=requires_attention,
            detail=detail or {},
        )

    # -- decision ----------------------------------------------------------

    def decide(
        self,
        record: FailureRecord,
        *,
        attempt_count: int,
        max_retries: int,
        previous_failure: dict[str, Any] | None = None,
        infra_failures: int = 0,
    ) -> FailureDecision:
        """Choose retry/fail/defer using the retry and infra budgets.

        ``attempt_count`` counts budgeted attempts already completed (the first
        attempt is not a retry).  ``max_retries`` is the policy-clamped number
        of retries allowed after the first attempt.
        """
        retries_left = max(0, max_retries - max(0, attempt_count - 1))
        previous = previous_failure or {}
        consecutive = 1
        if record.signature and previous.get("signature") == record.signature:
            consecutive = int(previous.get("consecutive_identical", 1)) + 1
        repeated = consecutive >= self.repeat_failure_threshold

        if record.kind is FailureKind.BUDGET_EXCEEDED:
            return FailureDecision(
                action=FailureAction.DEFER,
                record=record,
                reason="model budget exhausted; deferring launch",
            )
        if record.kind is FailureKind.POLICY_VIOLATION or not record.retryable:
            return FailureDecision(
                action=FailureAction.NEEDS_ATTENTION
                if record.requires_attention
                else FailureAction.FAIL,
                record=record,
                reason="non-retryable failure",
            )
        if repeated:
            repeated_record = self.make_record(
                FailureKind.REPEATED_FAILURE,
                f"identical failure repeated {consecutive} times: {record.summary}",
                retryable=False,
                counts_against_budget=record.counts_against_budget,
                diagnostics_path=record.diagnostics_path,
                requires_attention=True,
                detail={
                    **record.detail,
                    "original_kind": record.kind.value,
                    "consecutive_identical": consecutive,
                },
            )
            return FailureDecision(
                action=FailureAction.FAIL,
                record=repeated_record,
                reason="repeated identical failure; stopping early to save model usage",
            )
        if not record.counts_against_budget:
            if infra_failures >= self.max_infra_failures:
                return FailureDecision(
                    action=FailureAction.FAIL,
                    record=record,
                    reason=f"infra failure cap ({self.max_infra_failures}) exhausted",
                )
            return FailureDecision(
                action=FailureAction.RETRY,
                record=record,
                retry_not_before=self._backoff(attempt_count, counts_budget=False),
                reason="infra failure; retry does not consume the task retry budget",
            )
        if retries_left <= 0:
            return FailureDecision(
                action=FailureAction.FAIL,
                record=record,
                reason="retry budget exhausted",
            )
        return FailureDecision(
            action=FailureAction.RETRY,
            record=record,
            retry_not_before=self._backoff(attempt_count, counts_budget=True),
            reason=f"{retries_left} retry attempt(s) left",
        )

    def _backoff(self, attempt_number: int, *, counts_budget: bool) -> datetime:
        base = self.backoff_base_seconds
        exponent = max(0, attempt_number - 1)
        delay = min(self.backoff_max_seconds, base * (2**exponent))
        factor = 1.0
        if self.backoff_jitter_ratio > 0:
            jitter = min(1.0, max(0.0, self._jitter()))
            factor += (jitter - 0.5) * 2.0 * self.backoff_jitter_ratio
        delay = max(0.0, delay * factor)
        return self._clock() + timedelta(seconds=delay)


__all__ = [
    "FailureAction",
    "FailureClassifier",
    "FailureDecision",
    "FailureKind",
    "FailureRecord",
    "failure_signature",
    "normalize_failure_text",
]
