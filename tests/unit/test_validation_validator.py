"""Unit tests for independent task validation (plan 09)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes.gitmgr import FakeGitManager
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.common.errors import SandboxError
from devsandbox.common.ids import AttemptId
from devsandbox.common.types import CheckSpec, TaskSpec
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.gitmgr.interfaces import ChangeSet, Workspace
from devsandbox.sandbox.interfaces import SandboxResult
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.diff import DiffSummary
from devsandbox.validation.models import BaselineReport, FailureKind
from devsandbox.validation.validator import ValidatorImpl


class FakeDiffProvider:
    """Return a scripted diff summary."""

    def __init__(self, summary: DiffSummary | None = None) -> None:
        self.summary = summary or DiffSummary(files_changed=["src/app.py"])
        self.calls: list[tuple[Workspace, ChangeSet]] = []

    async def diff(self, ws: Workspace, cs: ChangeSet) -> DiffSummary:
        self.calls.append((ws, cs))
        return self.summary


def _settings(tmp_path: Path) -> DevSandboxSettings:
    return DevSandboxSettings(data_root=str(tmp_path / "data"))


def _workspace(tmp_path: Path, *, files: dict[str, str] | None = None) -> Workspace:
    path = tmp_path / "workspace"
    path.mkdir(exist_ok=True)
    for name, content in (files or {"src/app.py": "value = 1\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return Workspace(
        attempt_id=AttemptId("attempt-1"),
        path=str(path),
        base_commit="base",
        branch="devsandbox/attempt-1",
    )


def _task(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": "task-1",
        "title": "Do the thing",
        "checks": [CheckSpec(name="tests", run="pytest -q", kind="test")],
    }
    values.update(overrides)
    return TaskSpec.model_validate(values)


def _cs(*files: str) -> ChangeSet:
    return ChangeSet(
        base_commit="base",
        head_commit="head",
        files_changed=list(files) or ["src/app.py"],
    )


def _queue(
    sandbox: FakeSandboxManager,
    tmp_path: Path,
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    oom_killed: bool = False,
) -> None:
    index = len(sandbox.calls) + len(sandbox.results) + 1
    out = tmp_path / f"stdout-{index}.log"
    err = tmp_path / f"stderr-{index}.log"
    out.write_text(stdout, encoding="utf-8")
    err.write_text(stderr, encoding="utf-8")
    sandbox.queue_result(
        SandboxResult(
            container_id=f"container-{index}",
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            stdout_path=out,
            stderr_path=err,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            peak_memory_mb=None,
        )
    )


def _validator(
    tmp_path: Path,
    sandbox: FakeSandboxManager,
    *,
    git: FakeGitManager | None = None,
    diff: DiffSummary | None = None,
    config: ValidationConfig | None = None,
) -> ValidatorImpl:
    return ValidatorImpl(
        sandbox,
        git or FakeGitManager(),
        settings=_settings(tmp_path),
        config=config,
        data_root=tmp_path / "data",
        diff_provider=FakeDiffProvider(diff),
    )


@pytest.mark.asyncio
async def test_lying_agent_with_no_changes_fails(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    validator = _validator(tmp_path, sandbox, diff=DiffSummary())
    ws = _workspace(tmp_path)

    report = await validator.validate_task(
        _task(), ws, ChangeSet(base_commit="base", head_commit="base")
    )

    assert report.status == "failed"
    assert report.failure_kind is FailureKind.NO_CHANGES
    assert report.blame == "agent"
    assert "no changes" in report.summary_for_retry
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_required_failure_is_structured_and_retry_ready(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1, stderr="assert 1 == 2")
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "failed"
    assert report.failure_kind is FailureKind.TEST_FAILURE
    assert report.blame == "agent"
    assert len(report.checks) == 1
    assert report.checks[0].status == "failed"
    assert report.evidence[0].command == "pytest -q"
    assert "tests [test_failure]" in report.summary_for_retry
    assert "assert 1 == 2" in report.summary_for_retry
    assert "do not weaken or delete tests" in report.summary_for_retry

    request = sandbox.calls[0]
    assert request.secret_env == {}
    assert request.network.mode == "none"


@pytest.mark.asyncio
async def test_infrastructure_error_is_not_an_agent_failure(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.fail(SandboxError("docker daemon unavailable"))
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "error"
    assert report.failure_kind is FailureKind.INFRA_ERROR
    assert report.blame == "infrastructure"
    assert report.has_infra_error
    assert "infrastructure" in report.summary_for_retry.lower()


@pytest.mark.asyncio
async def test_optional_failure_is_only_a_warning(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=0)
    _queue(sandbox, tmp_path, exit_code=1, stdout="lint error")
    checks = [
        CheckSpec(name="tests", run="pytest", required=True, kind="test"),
        CheckSpec(name="lint", run="ruff", required=False, kind="lint"),
    ]
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(checks=checks), ws, _cs())

    assert report.status == "passed"
    assert report.checks[1].status == "failed"
    assert any("optional check failed: lint" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_optional_failure_can_be_configured_to_fail(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=0)
    _queue(sandbox, tmp_path, exit_code=1)
    checks = [
        CheckSpec(name="tests", run="pytest", required=True, kind="test"),
        CheckSpec(name="lint", run="ruff", required=False, kind="lint"),
    ]
    validator = _validator(
        tmp_path, sandbox, config=ValidationConfig(fail_on_optional_failure=True)
    )
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(checks=checks), ws, _cs())

    assert report.status == "failed"
    assert report.failure_kind is FailureKind.LINT_FAILURE


@pytest.mark.asyncio
async def test_no_checks_configured_is_an_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(checks=[]), ws, _cs())

    assert report.status == "error"
    assert report.failure_kind is FailureKind.INFRA_ERROR
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_no_required_check_is_an_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)
    checks = [CheckSpec(name="lint", run="ruff", required=False)]

    report = await validator.validate_task(_task(checks=checks), ws, _cs())

    assert report.status == "error"
    assert "no required validation check" in report.summary
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_tampering_fails_a_passing_check(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=0)
    text = (
        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
        "+++ b/tests/test_app.py\n"
        "+@pytest.mark.skip(reason='flaky')\n"
    )
    validator = _validator(
        tmp_path,
        sandbox,
        diff=DiffSummary(files_changed=["tests/test_app.py"], text=text),
    )
    ws = _workspace(tmp_path, files={"tests/test_app.py": "def test_x():\n    pass\n"})

    report = await validator.validate_task(_task(), ws, _cs("tests/test_app.py"))

    assert report.status == "failed"
    assert report.failure_kind is FailureKind.TAMPERING
    assert report.tampering is not None and report.tampering.detected
    assert report.failures[-1].check == "test-tampering"
    assert "test-tampering" in report.summary_for_retry


@pytest.mark.asyncio
async def test_tampering_does_not_mask_an_infrastructure_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    sandbox.fail(SandboxError("docker daemon unavailable"))
    text = "+++ b/tests/test_app.py\n+@pytest.mark.skip(reason='flaky')\n"
    validator = _validator(
        tmp_path,
        sandbox,
        diff=DiffSummary(files_changed=["tests/test_app.py"], text=text),
    )
    ws = _workspace(tmp_path, files={"tests/test_app.py": "def test_x():\n    pass\n"})

    report = await validator.validate_task(_task(), ws, _cs("tests/test_app.py"))

    assert report.status == "error"
    assert report.failure_kind is FailureKind.INFRA_ERROR
    assert report.blame == "infrastructure"
    assert report.tampering is not None and report.tampering.detected


@pytest.mark.asyncio
async def test_tampering_warn_mode_keeps_pass(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=0)
    validator = _validator(
        tmp_path,
        sandbox,
        config=ValidationConfig(tamper_mode="warn"),
        diff=DiffSummary(files_changed=["conftest.py"]),
    )
    ws = _workspace(tmp_path, files={"conftest.py": ""})

    report = await validator.validate_task(_task(), ws, _cs("conftest.py"))

    assert report.status == "passed"
    assert report.tampering is not None and report.tampering.detected
    assert any("test-tampering" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_pre_existing_baseline_failure_is_not_blamed_on_agent(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1, stderr="already broken")
    validator = _validator(tmp_path, sandbox)
    validator.set_baseline(
        BaselineReport(
            status="failed",
            revision="base-rev",
            failed_checks=["tests"],
            summary="baseline failed",
        )
    )
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "passed"
    assert report.pre_existing_failure
    assert report.blame == "baseline"
    assert report.baseline is not None
    assert report.baseline.pre_existing_failures == ["tests"]
    assert report.summary_for_retry == ""


@pytest.mark.asyncio
async def test_new_failure_against_baseline_is_blamed_on_agent(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1)
    validator = _validator(tmp_path, sandbox)
    validator.set_baseline(
        BaselineReport(
            status="failed",
            revision="base-rev",
            failed_checks=["lint"],
            summary="baseline failed",
        )
    )
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "failed"
    assert report.blame == "agent"
    assert report.baseline is not None
    assert report.baseline.newly_failing_checks == ["tests"]


@pytest.mark.asyncio
async def test_baseline_error_only_warns(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1)
    validator = _validator(tmp_path, sandbox)
    validator.set_baseline(
        BaselineReport(
            status="error",
            revision="base-rev",
            failed_checks=[],
            summary="baseline errored",
        )
    )
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "failed"
    assert any("baseline comparison unavailable" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_task_runs_in_a_disposable_scratch_copy(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=0)
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path)

    report = await validator.validate_task(_task(), ws, _cs())

    assert report.status == "passed"
    scratch = Path(sandbox.calls[0].workspace_path)
    assert scratch != Path(ws.path)
    assert scratch.is_relative_to(tmp_path / "data" / "validation")
    assert not scratch.exists()  # cleaned up after validation
    assert (Path(ws.path) / "src" / "app.py").exists()


@pytest.mark.asyncio
async def test_restricted_path_fails_before_running_containers(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    validator = _validator(tmp_path, sandbox)
    ws = _workspace(tmp_path, files={"src/secret.py": "shh\n"})

    report = await validator.validate_task(
        _task(restricted_files=["src/secret.py"]),
        ws,
        _cs("src/secret.py"),
    )

    assert report.status == "failed"
    assert report.failure_kind is FailureKind.RESTRICTED_PATH
    assert sandbox.calls == []
