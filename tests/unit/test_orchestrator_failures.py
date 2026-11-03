"""Unit tests for failure classification, retries, and circuit breakers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from devsandbox.agents.interfaces import AgentRunResult
from devsandbox.common.errors import GitError, PolicyViolation
from devsandbox.gitmgr.interfaces import IntegrationResult
from devsandbox.orchestrator.failures import (
    FailureAction,
    FailureClassifier,
    FailureKind,
    failure_signature,
    normalize_failure_text,
)
from devsandbox.validation.interfaces import CheckResult, ValidationReport


def _classifier(**overrides) -> FailureClassifier:
    defaults = {
        "max_infra_failures": 3,
        "repeat_failure_threshold": 2,
        "backoff_base_seconds": 10.0,
        "backoff_max_seconds": 100.0,
        "backoff_jitter_ratio": 0.0,
        "clock": lambda: datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "jitter": lambda: 1.0,
    }
    defaults.update(overrides)
    return FailureClassifier(**defaults)


def _agent(status: str, exit_code: int | None = 1) -> AgentRunResult:
    return AgentRunResult(
        exit_status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        log_path=None,
        summary=None,
        token_usage=None,
        duration_seconds=1.0,
    )


def test_successful_agent_result_has_no_failure() -> None:
    assert _classifier().from_agent_result(_agent("exited_ok", 0)) is None


@pytest.mark.parametrize(
    ("status", "kind", "counts"),
    [
        ("exited_error", FailureKind.AGENT_ERROR, True),
        ("crashed", FailureKind.AGENT_CRASH, True),
        ("timeout", FailureKind.TIMEOUT, True),
        ("oom", FailureKind.OOM, True),
        ("sandbox_error", FailureKind.CONTAINER_FAILURE, False),
    ],
)
def test_agent_failure_mapping(status: str, kind: FailureKind, counts: bool) -> None:
    record = _classifier().from_agent_result(_agent(status))
    assert record is not None
    assert record.kind is kind
    assert record.counts_against_budget is counts


def test_retry_budget_and_backoff() -> None:
    classifier = _classifier()
    record = classifier.from_agent_result(_agent("exited_error"))
    assert record is not None

    first = classifier.decide(record, attempt_count=1, max_retries=2)
    assert first.action is FailureAction.RETRY
    assert first.retry_not_before == datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)

    second = classifier.decide(record, attempt_count=2, max_retries=2)
    assert second.action is FailureAction.RETRY
    assert second.retry_not_before == datetime(2026, 1, 1, 12, 0, 20, tzinfo=UTC)

    exhausted = classifier.decide(record, attempt_count=3, max_retries=2)
    assert exhausted.action is FailureAction.FAIL


def test_backoff_is_capped() -> None:
    classifier = _classifier(backoff_max_seconds=15.0)
    record = classifier.from_agent_result(_agent("timeout"))
    assert record is not None
    decision = classifier.decide(record, attempt_count=10, max_retries=20)
    assert decision.retry_not_before is not None
    assert decision.retry_not_before.second == 15 or decision.retry_not_before.minute == 12


def test_repeated_identical_failure_stops_early() -> None:
    classifier = _classifier(repeat_failure_threshold=2)
    record = classifier.from_agent_result(_agent("exited_error"))
    assert record is not None
    first = classifier.decide(record, attempt_count=1, max_retries=5)
    assert first.action is FailureAction.RETRY
    second = classifier.decide(
        record,
        attempt_count=2,
        max_retries=5,
        previous_failure={"signature": record.signature, "consecutive_identical": 1},
    )
    assert second.action is FailureAction.FAIL
    assert second.record.kind is FailureKind.REPEATED_FAILURE
    assert second.record.requires_attention


def test_changed_failure_does_not_trip_repeat_detection() -> None:
    classifier = _classifier()
    first = classifier.from_agent_result(_agent("exited_error"))
    second = classifier.from_agent_result(_agent("timeout"))
    assert first is not None and second is not None
    decision = classifier.decide(
        second,
        attempt_count=2,
        max_retries=5,
        previous_failure={"signature": first.signature, "consecutive_identical": 1},
    )
    assert decision.action is FailureAction.RETRY


def test_infra_failure_does_not_consume_retry_budget_but_has_a_cap() -> None:
    classifier = _classifier(max_infra_failures=2)
    record = classifier.from_exception(GitError("workspace create failed"))
    assert record.kind is FailureKind.INFRA_ERROR
    assert not record.counts_against_budget
    assert classifier.decide(record, attempt_count=1, max_retries=0).action is FailureAction.RETRY
    assert (
        classifier.decide(
            record, attempt_count=1, max_retries=0, infra_failures=2
        ).action
        is FailureAction.FAIL
    )


def test_policy_violation_fails_immediately() -> None:
    classifier = _classifier()
    record = classifier.from_exception(PolicyViolation("network escalation"))
    decision = classifier.decide(record, attempt_count=1, max_retries=99)
    assert decision.action is FailureAction.NEEDS_ATTENTION
    assert decision.record.kind is FailureKind.POLICY_VIOLATION
    assert not decision.record.retryable


def test_budget_exceeded_defers_instead_of_failing() -> None:
    classifier = _classifier()
    record = classifier.budget_exceeded("token budget exhausted")
    decision = classifier.decide(record, attempt_count=3, max_retries=0)
    assert decision.action is FailureAction.DEFER
    assert decision.retry_not_before is None


def test_validation_failure_classification() -> None:
    classifier = _classifier()
    failed = ValidationReport(
        status="failed",
        checks=[CheckResult(check="tests", passed=False, output="boom")],
        summary="pytest failed",
    )
    record = classifier.from_validation(failed)
    assert record.kind is FailureKind.VALIDATION_FAILURE
    assert record.retryable
    assert classifier.decide(record, attempt_count=1, max_retries=1).action is FailureAction.RETRY

    no_changes = classifier.from_validation(
        ValidationReport(status="failed", summary="no changes detected in workspace")
    )
    assert no_changes.kind is FailureKind.NO_CHANGES

    restricted = classifier.from_validation(
        ValidationReport(status="failed", summary="restricted path touched: .github/workflows/x")
    )
    assert restricted.kind is FailureKind.RESTRICTED_PATH
    assert not restricted.retryable
    assert (
        classifier.decide(restricted, attempt_count=1, max_retries=5).action
        is FailureAction.NEEDS_ATTENTION
    )


def test_integration_failure_classification() -> None:
    classifier = _classifier()
    conflict = classifier.from_integration(
        IntegrationResult(status="conflict", conflicting_files=["src/a.py"])
    )
    assert conflict.kind is FailureKind.GIT_CONFLICT
    assert conflict.retryable
    assert classifier.decide(conflict, attempt_count=1, max_retries=1).action is (
        FailureAction.RETRY
    )

    rejected = classifier.from_integration(
        IntegrationResult(status="rejected", detail="staging moved")
    )
    assert rejected.kind is FailureKind.INTERRUPTED_INTEGRATION
    assert not rejected.counts_against_budget


def test_signature_normalization_ignores_volatile_values() -> None:
    a = normalize_failure_text("failed at /tmp/ws-123/src/a.py after 42s sha=abcdef123456")
    b = normalize_failure_text("failed at /tmp/ws-999/src/a.py after 7s sha=000000000000")
    assert a == b
    assert failure_signature(FailureKind.AGENT_ERROR, "boom") == failure_signature(
        FailureKind.AGENT_ERROR, "boom"
    )
    assert failure_signature(FailureKind.AGENT_ERROR, "boom") != failure_signature(
        FailureKind.TIMEOUT, "boom"
    )
