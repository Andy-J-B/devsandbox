"""Unit tests for integration validation and baseline attribution (plan 09)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.fakes.gitmgr import FakeGitManager
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.common.errors import SandboxError, ValidationError
from devsandbox.common.types import CheckSpec, RepoHandle
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.interfaces import SandboxResult
from devsandbox.validation.models import FailureKind
from devsandbox.validation.validator import ValidatorImpl


class FakeCheckout:
    """Materialise a fake revision by writing a marker file."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[RepoHandle, str, Path]] = []

    async def checkout(self, repo: RepoHandle, revision: str, dest: Path) -> None:
        self.calls.append((repo, revision, dest))
        if self.error is not None:
            raise self.error
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "REVISION").write_text(revision, encoding="utf-8")


def _repo() -> RepoHandle:
    return RepoHandle(
        project_id="proj-1",
        path="/tmp/fake/proj-1/repo.git",
        main_branch="main",
        staging_branch="devsandbox/proj-1/staging",
    )


def _checks() -> list[CheckSpec]:
    return [CheckSpec(name="tests", run="pytest -q", kind="test")]


def _queue(
    sandbox: FakeSandboxManager,
    tmp_path: Path,
    *,
    exit_code: int | None = 0,
    stderr: str = "",
    stdout: str = "",
) -> None:
    index = len(sandbox.calls) + len(sandbox.results) + 1
    out = tmp_path / f"int-stdout-{index}.log"
    err = tmp_path / f"int-stderr-{index}.log"
    out.write_text(stdout, encoding="utf-8")
    err.write_text(stderr, encoding="utf-8")
    sandbox.queue_result(
        SandboxResult(
            container_id=f"int-container-{index}",
            exit_code=exit_code,
            timed_out=False,
            oom_killed=False,
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
    checkout: FakeCheckout,
    *,
    git: FakeGitManager | None = None,
) -> ValidatorImpl:
    return ValidatorImpl(
        sandbox,
        git or FakeGitManager(),
        settings=DevSandboxSettings(data_root=str(tmp_path / "data")),
        data_root=tmp_path / "data",
        checkout=checkout,
    )


@pytest.mark.asyncio
async def test_passing_candidate_is_accepted_without_baseline_run(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path)
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["previous"]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "passed"
    assert report.baseline is None
    assert [call[1] for call in checkout.calls] == ["candidate"]
    assert len(sandbox.calls) == 1
    assert sandbox.calls[0].labels["devsandbox.phase"] == "integration"


@pytest.mark.asyncio
async def test_new_failure_is_blamed_on_the_candidate(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1, stderr="new breakage")
    _queue(sandbox, tmp_path, exit_code=0)
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["previous"]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "failed"
    assert report.blame == "agent"
    assert report.baseline is not None
    assert report.baseline.checked
    assert report.baseline.baseline_revision == "previous"
    assert report.baseline.candidate_failed_checks == ["tests"]
    assert report.baseline.newly_failing_checks == ["tests"]
    assert [call[1] for call in checkout.calls] == ["candidate", "previous"]
    # Validation is read-only with respect to staging.
    assert not any(
        call["op"] in {"reset_staging_to", "integrate_to_staging", "commit_integration"}
        for call in git.calls
    )


@pytest.mark.asyncio
async def test_pre_existing_failure_is_attributed_to_baseline(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1, stderr="already broken")
    _queue(sandbox, tmp_path, exit_code=1, stderr="already broken")
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["previous"]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "passed"
    assert report.pre_existing_failure
    assert report.blame == "baseline"
    assert report.baseline is not None
    assert report.baseline.pre_existing_failures == ["tests"]
    assert report.baseline.newly_failing_checks == []
    assert report.summary_for_retry == ""


@pytest.mark.asyncio
async def test_only_new_failures_are_reported_for_retry(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    checks = [
        CheckSpec(name="tests", run="pytest", kind="test"),
        CheckSpec(name="lint", run="ruff", kind="lint"),
    ]
    # candidate: tests fails, lint fails
    _queue(sandbox, tmp_path, exit_code=1)
    _queue(sandbox, tmp_path, exit_code=1)
    # baseline: tests passes, lint fails
    _queue(sandbox, tmp_path, exit_code=0)
    _queue(sandbox, tmp_path, exit_code=1)
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["previous"]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", checks)

    assert report.status == "failed"
    assert report.blame == "agent"
    assert report.baseline is not None
    assert report.baseline.newly_failing_checks == ["tests"]
    assert report.baseline.pre_existing_failures == ["lint"]
    assert [failure.check for failure in report.failures] == ["tests"]
    assert "tests [test_failure]" in report.summary_for_retry
    assert "- lint [" not in report.summary_for_retry
    assert "pre-existing failures" in report.summary_for_retry


@pytest.mark.asyncio
async def test_no_previous_staging_head_skips_baseline(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1)
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = [None]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "failed"
    assert report.baseline is not None
    assert not report.baseline.checked
    assert "no previous staging head" in report.baseline.detail
    assert [call[1] for call in checkout.calls] == ["candidate"]


@pytest.mark.asyncio
async def test_staging_head_error_does_not_mask_failure(tmp_path: Path) -> None:
    class BrokenGit(FakeGitManager):
        async def staging_head(self, repo):
            raise RuntimeError("ref store unavailable")

    sandbox = FakeSandboxManager()
    _queue(sandbox, tmp_path, exit_code=1)
    checkout = FakeCheckout()
    validator = _validator(tmp_path, sandbox, checkout, git=BrokenGit())

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "failed"
    assert report.baseline is not None
    assert not report.baseline.checked
    assert "could not read previous staging head" in report.baseline.detail


@pytest.mark.asyncio
async def test_baseline_infrastructure_error_only_warns(tmp_path: Path) -> None:
    class BaselineFails(FakeSandboxManager):
        def __init__(self) -> None:
            super().__init__()
            self._runs = 0

        async def run(self, req):  # type: ignore[no-untyped-def]
            self._runs += 1
            if self._runs == 2:
                raise SandboxError("baseline sandbox failed")
            return await super().run(req)

    sandbox = BaselineFails()
    _queue(sandbox, tmp_path, exit_code=1)
    checkout = FakeCheckout()
    git = FakeGitManager()
    git.staging_heads["proj-1"] = ["previous"]
    validator = _validator(tmp_path, sandbox, checkout, git=git)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "failed"
    assert report.blame == "agent"
    assert report.baseline is not None
    assert report.baseline.baseline_status == "error"
    assert any("baseline comparison unavailable" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_checkout_failure_is_an_infrastructure_error(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    checkout = FakeCheckout(error=ValidationError("candidate ref missing"))
    validator = _validator(tmp_path, sandbox, checkout)

    report = await validator.validate_integration(_repo(), "candidate", _checks())

    assert report.status == "error"
    assert report.failure_kind is FailureKind.INFRA_ERROR
    assert report.blame == "infrastructure"
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_integration_requires_checks(tmp_path: Path) -> None:
    sandbox = FakeSandboxManager()
    checkout = FakeCheckout()
    validator = _validator(tmp_path, sandbox, checkout)

    report = await validator.validate_integration(_repo(), "candidate", [])

    assert report.status == "error"
    assert "no validation checks configured" in report.summary
    assert checkout.calls == []
