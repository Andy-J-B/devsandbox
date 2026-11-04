"""Unit tests for the sandboxed validation check runner (plan 09)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.common.errors import SandboxError
from devsandbox.common.types import CheckSpec, NetworkPolicy, ResourceProfile
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import DevSandboxSettings, SecurityConfig
from devsandbox.sandbox.interfaces import SandboxResult
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.models import FailureKind
from devsandbox.validation.runner import CheckRunner, safe_attempt_id


def _result(
    tmp_path: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    timed_out: bool = False,
    oom_killed: bool = False,
    container_id: str = "container-1",
) -> SandboxResult:
    out = tmp_path / "stdout.log"
    err = tmp_path / "stderr.log"
    out.write_text(stdout)
    err.write_text(stderr)
    return SandboxResult(
        container_id=container_id,
        exit_code=exit_code,
        timed_out=timed_out,
        oom_killed=oom_killed,
        stdout_path=out,
        stderr_path=err,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        peak_memory_mb=None,
    )


def _runner(
    sandbox: FakeSandboxManager,
    tmp_path: Path,
    *,
    config: ValidationConfig | None = None,
    policy: SecurityPolicy | None = None,
) -> CheckRunner:
    return CheckRunner(sandbox, config or ValidationConfig(), policy=policy)


@pytest.mark.asyncio
async def test_passing_check_maps_to_passed(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, stdout="ok"))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest -q", kind="test"), tmp_path, attempt_id="a1"
    )

    assert result.status == "passed"
    assert result.passed is True
    assert result.exit_code == 0
    assert result.duration_seconds == 1.0
    request = sandbox.calls[0]
    assert request.secret_env == {}
    assert request.network.mode == "none"
    assert request.image == "devsandbox/validator:latest"
    assert request.resources == ResourceProfile(
        cpu_cores=1.0, memory_mb=2048, disk_mb=4096, pids_limit=512
    )
    assert request.labels["devsandbox.check"] == "tests"
    assert sandbox.destroyed == ["container-1"]


@pytest.mark.asyncio
async def test_failing_check_maps_to_failed_kind(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, stderr="assertion failed", exit_code=1))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest -q", kind="test"), tmp_path, attempt_id="a1"
    )

    assert result.status == "failed"
    assert result.passed is False
    assert result.failure_kind is FailureKind.TEST_FAILURE
    assert "exit code 1" in result.reason
    assert "assertion failed" in result.stderr_tail


@pytest.mark.asyncio
async def test_expected_exit_codes_are_honoured(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, exit_code=5))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="migrations", run="check", expected_exit_codes=[0, 5]),
        tmp_path,
        attempt_id="a1",
    )

    assert result.status == "passed"


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_timeout()
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="sleep 600", timeout_seconds=1), tmp_path, attempt_id="a1"
    )

    assert result.status == "timeout"
    assert result.failure_kind is FailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_sandbox_error_maps_to_infra_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.fail(SandboxError("docker unavailable"))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest -q"), tmp_path, attempt_id="a1"
    )

    assert result.status == "error"
    assert result.failure_kind is FailureKind.INFRA_ERROR
    assert "docker unavailable" in result.reason


@pytest.mark.asyncio
async def test_output_is_redacted_and_truncated(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    secret = "ghp_" + "a" * 30
    sandbox.queue_result(_result(tmp_path, stdout="x" * 5000 + "\n" + secret, stderr="err"))
    runner = _runner(sandbox, tmp_path, config=ValidationConfig(max_output_bytes=256))
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest -q"), tmp_path, attempt_id="a1"
    )

    assert "ghp_" not in result.stdout_tail
    assert "[REDACTED]" in result.stdout_tail
    assert "truncated" in result.stdout_tail
    assert len(result.output.encode("utf-8")) <= 256 + len(
        "\n[... output truncated by devsandbox ...]\n"
    )


@pytest.mark.asyncio
async def test_success_and_failure_regex(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, stdout="ALL GOOD"))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest", success_regex=r"ALL GOOD"), tmp_path, attempt_id="a1"
    )
    assert result.status == "passed"

    sandbox.queue_result(_result(tmp_path, stdout="1 failed"))
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest", failure_regex=r"\d+ failed"),
        tmp_path,
        attempt_id="a2",
    )
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_invalid_regex_is_infra_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, exit_code=0))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest", success_regex="["), tmp_path, attempt_id="a1"
    )
    assert result.status == "error"
    assert result.failure_kind is FailureKind.INFRA_ERROR


@pytest.mark.asyncio
async def test_junit_results_are_parsed(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    reports.joinpath("junit.xml").write_text(
        '<testsuite tests="4" failures="1" errors="0" skipped="2"></testsuite>'
    )
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, exit_code=0))
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest", junit_xml_path="reports/junit.xml"),
        tmp_path,
        attempt_id="a1",
    )

    assert result.tests_total == 4
    assert result.tests_failed == 1
    assert result.tests_skipped == 2
    assert result.tests_errors == 0


@pytest.mark.asyncio
async def test_working_dir_escape_is_rejected(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    runner = _runner(sandbox, tmp_path)
    result = await runner.run_check(
        CheckSpec(name="tests", run="pytest", working_dir="../outside"),
        tmp_path,
        attempt_id="a1",
    )

    assert result.status == "error"
    assert result.failure_kind is FailureKind.INFRA_ERROR
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_allowlist_network_is_rejected(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    runner = _runner(
        sandbox,
        tmp_path,
        config=ValidationConfig(
            network=NetworkPolicy(mode="allowlist", allowed_hosts=["pypi.org"])
        ),
    )
    result = await runner.run_check(
        CheckSpec(
            name="setup",
            run="pip install x",
            network="allowlist",
            allowed_hosts=["pypi.org"],
            kind="setup",
        ),
        tmp_path,
        attempt_id="a1",
    )

    assert result.status == "error"
    assert result.failure_kind is FailureKind.INFRA_ERROR
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_policy_narrows_network_and_resources(tmp_path: Path) -> None:
    settings = DevSandboxSettings(security=SecurityConfig(allow_network_default=False))
    policy = SecurityPolicy(settings)
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path))
    runner = _runner(
        sandbox,
        tmp_path,
        config=ValidationConfig(network=NetworkPolicy(mode="full")),
        policy=policy,
    )
    await runner.run_check(
        CheckSpec(name="setup", run="pip install x", network="full", kind="setup"),
        tmp_path,
        attempt_id="a1",
    )

    assert sandbox.calls[0].network.mode == "none"


@pytest.mark.asyncio
async def test_optional_checks_can_be_short_circuited(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.queue_result(_result(tmp_path, exit_code=1))
    runner = _runner(
        sandbox,
        tmp_path,
        config=ValidationConfig(short_circuit_on_required_failure=True),
    )
    checks = [
        CheckSpec(name="required-tests", run="pytest", required=True),
        CheckSpec(name="optional-lint", run="ruff", required=False),
    ]
    results = await runner.run_checks(checks, tmp_path, attempt_id="a1")

    assert results[0].status == "failed"
    assert results[1].status == "skipped"
    assert len(sandbox.calls) == 1


def test_safe_attempt_id() -> None:
    assert safe_attempt_id("task-1/a1") == "task-1-a1"
    assert safe_attempt_id("...") == "validation"
