"""Unit tests for host-side validation pre-checks (plan 09)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fakes.gitmgr import FakeGitManager

from devsandbox.common.ids import AttemptId
from devsandbox.common.types import TaskSpec
from devsandbox.gitmgr.interfaces import ChangeSet, Workspace
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.diff import DiffSummary
from devsandbox.validation.models import FailureKind
from devsandbox.validation.prechecks import run_prechecks


class FakeDiffProvider:
    """Return a scripted diff summary."""

    def __init__(self, summary: DiffSummary) -> None:
        self.summary = summary
        self.calls: list[tuple[Workspace, ChangeSet]] = []

    async def diff(self, ws: Workspace, cs: ChangeSet) -> DiffSummary:
        self.calls.append((ws, cs))
        return self.summary


def _workspace(tmp_path: Path) -> Workspace:
    path = tmp_path / "workspace"
    path.mkdir()
    return Workspace(
        attempt_id=AttemptId("attempt-1"),
        path=str(path),
        base_commit="base",
        branch="devsandbox/attempt-1",
    )


def _task(**overrides: object) -> TaskSpec:
    values: dict[str, object] = {"id": "task-1", "title": "Do the thing"}
    values.update(overrides)
    return TaskSpec.model_validate(values)


def _diff(**overrides: object) -> DiffSummary:
    values: dict[str, object] = {"files_changed": ["src/app.py"]}
    values.update(overrides)
    return DiffSummary(**values)  # type: ignore[arg-type]


async def _run(
    tmp_path: Path,
    *,
    task: TaskSpec | None = None,
    cs: ChangeSet | None = None,
    diff: DiffSummary | None = None,
    git: FakeGitManager | None = None,
    config: ValidationConfig | None = None,
    ws: Workspace | None = None,
):
    workspace = ws or _workspace(tmp_path)
    change_set = cs or ChangeSet(
        base_commit="base", head_commit="head", files_changed=["src/app.py"]
    )
    return await run_prechecks(
        task or _task(),
        workspace,
        change_set,
        git or FakeGitManager(),
        config or ValidationConfig(),
        diff_provider=FakeDiffProvider(diff or _diff()),
    )


@pytest.mark.asyncio
async def test_empty_change_set_fails(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        cs=ChangeSet(base_commit="base", head_commit="base"),
        diff=DiffSummary(),
    )

    assert outcome.fatal
    assert not outcome.infra_error
    assert [failure.kind for failure in outcome.failures] == [FailureKind.NO_CHANGES]


@pytest.mark.asyncio
async def test_empty_change_set_allowed_by_task(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        task=_task(allow_no_changes=True),
        cs=ChangeSet(base_commit="base", head_commit="base"),
        diff=DiffSummary(),
    )

    assert not outcome.fatal
    assert outcome.failures == []
    assert "allows no changes" in outcome.warnings[0]


@pytest.mark.asyncio
async def test_restricted_path_fails(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        task=_task(restricted_files=["src/secret.py"]),
        cs=ChangeSet(
            base_commit="base",
            head_commit="head",
            files_changed=["src/secret.py", "src/app.py"],
        ),
    )

    assert outcome.fatal
    failure = outcome.failures[0]
    assert failure.kind is FailureKind.RESTRICTED_PATH
    assert "src/secret.py" in failure.summary


@pytest.mark.asyncio
async def test_restricted_path_infrastructure_error(tmp_path: Path) -> None:
    class BrokenGit(FakeGitManager):
        async def check_restricted_paths(self, cs, restricted):
            raise RuntimeError("git unavailable")

    outcome = await _run(tmp_path, git=BrokenGit())

    assert outcome.infra_error
    assert outcome.failures[0].kind is FailureKind.INFRA_ERROR


@pytest.mark.asyncio
async def test_size_limits_fail(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        cs=ChangeSet(base_commit="base", head_commit="head"),
        diff=_diff(files_changed=[f"f{i}.py" for i in range(4)], additions=90, deletions=20),
        config=ValidationConfig(max_changed_files=3, max_diff_lines=100),
    )

    kinds = [failure.kind for failure in outcome.failures]
    assert FailureKind.SIZE_LIMIT in kinds
    assert any("4 files" in failure.summary for failure in outcome.failures)
    assert any("110 changed lines" in failure.summary for failure in outcome.failures)


@pytest.mark.asyncio
async def test_forbidden_binary_and_extension_fail(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        diff=_diff(
            files_changed=["tool.exe", "image.png", "src/app.py"],
            binary_files=["image.png"],
        ),
    )

    failures = [
        failure for failure in outcome.failures if failure.kind is FailureKind.FORBIDDEN_FILE
    ]
    assert len(failures) == 1
    assert "tool.exe" in failures[0].summary
    assert "image.png" in failures[0].summary


@pytest.mark.asyncio
async def test_unavailable_diff_only_warns(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        diff=DiffSummary(available=False, error="not a repo"),
    )

    assert not outcome.fatal
    assert outcome.warnings and "diff unavailable" in outcome.warnings[0]


@pytest.mark.asyncio
async def test_truncated_diff_warns(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, diff=_diff(truncated=True))

    assert not outcome.fatal
    assert any("truncated" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_expected_files_warn_by_default(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        task=_task(expected_files=["docs/**"]),
        diff=_diff(files_changed=["src/app.py"]),
    )

    assert not outcome.fatal
    assert any("expected files" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_expected_files_fail_mode(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        task=_task(expected_files=["docs/**"], expected_files_mode="fail"),
        diff=_diff(files_changed=["src/app.py"]),
    )

    assert outcome.fatal
    assert outcome.failures[0].kind is FailureKind.EXPECTED_FILES


@pytest.mark.asyncio
async def test_expected_files_match_passes(tmp_path: Path) -> None:
    outcome = await _run(
        tmp_path,
        task=_task(expected_files=["docs/**"], expected_files_mode="fail"),
        diff=_diff(files_changed=["docs/validation.md"]),
    )

    assert not outcome.fatal


@pytest.mark.asyncio
async def test_missing_workspace_is_infrastructure_error(tmp_path: Path) -> None:
    workspace = Workspace(
        attempt_id=AttemptId("attempt-1"),
        path=str(tmp_path / "missing"),
        base_commit="base",
        branch="devsandbox/attempt-1",
    )
    outcome = await _run(tmp_path, ws=workspace)

    assert outcome.infra_error
    assert outcome.failures[-1].check == "workspace"
