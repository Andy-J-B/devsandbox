"""Fast host-side validation pre-checks (plan 09).

These checks never execute agent code and therefore run before any validation
container is started.  They enforce the change-set invariants that must hold
regardless of what the agent claims: non-empty changes, restricted paths, size
caps, forbidden binaries, and expected-file coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pathspec import PathSpec

from ..common.types import TaskSpec
from ..gitmgr.interfaces import ChangeSet, GitManager, Workspace
from .config import ValidationConfig
from .diff import DiffProvider, DiffSummary, GitDiffProvider
from .models import FailureKind, ValidationFailure


@dataclass(frozen=True)
class PrecheckOutcome:
    """Result of host-side pre-checks."""

    failures: list[ValidationFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infra_error: bool = False
    diff: DiffSummary | None = None

    @property
    def fatal(self) -> bool:
        """Return whether container checks should be skipped."""
        return bool(self.failures) or self.infra_error


def _failure(
    check: str, kind: FailureKind, summary: str, *, log_tail: str = ""
) -> ValidationFailure:
    return ValidationFailure(check=check, kind=kind, summary=summary, log_tail=log_tail)


def _has_forbidden_extension(path: str, extensions: list[str]) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(extension.lower()) for extension in extensions)


def _expected_files_mode(task: TaskSpec, config: ValidationConfig) -> str:
    return task.expected_files_mode or config.expected_files_mode


async def run_prechecks(
    task: TaskSpec,
    ws: Workspace,
    cs: ChangeSet,
    git: GitManager,
    config: ValidationConfig | None = None,
    *,
    diff_provider: DiffProvider | None = None,
) -> PrecheckOutcome:
    """Run all host-side pre-checks and return structured findings."""
    config = config or ValidationConfig()
    provider = diff_provider or GitDiffProvider()
    diff: DiffSummary = await provider.diff(ws, cs)
    # Use the union of both sources: the change set is the orchestrator's
    # record, while the diff is read directly from git.  Never let an
    # incomplete change set hide a restricted/forbidden path from pre-checks.
    changed = list(dict.fromkeys([*cs.files_changed, *diff.files_changed]))

    failures: list[ValidationFailure] = []
    warnings: list[str] = []
    infra_error = False

    # 1. Non-empty change set (the "lying agent" guard).
    if not changed:
        if task.allow_no_changes:
            warnings.append("change set is empty but the task allows no changes")
        else:
            failures.append(
                _failure(
                    "change-set",
                    FailureKind.NO_CHANGES,
                    "no changes detected in the attempt workspace",
                )
            )

    # 2. Restricted paths (defence in depth; the orchestrator also checks).
    try:
        violations = await git.check_restricted_paths(cs, list(task.restricted_files))
    except Exception as exc:  # infrastructure/configuration problem, not the agent
        infra_error = True
        failures.append(
            _failure(
                "restricted-paths",
                FailureKind.INFRA_ERROR,
                f"could not check restricted paths: {exc}",
            )
        )
        violations = []
    if violations:
        failures.append(
            _failure(
                "restricted-paths",
                FailureKind.RESTRICTED_PATH,
                "restricted path touched: " + ", ".join(violations),
                log_tail="\n".join(violations),
            )
        )

    # 3. Diff size / forbidden binary caps.
    if not diff.available:
        warnings.append(f"diff unavailable; size/binary checks skipped: {diff.error}")
    else:
        if len(changed) > config.max_changed_files:
            failures.append(
                _failure(
                    "diff-size",
                    FailureKind.SIZE_LIMIT,
                    f"change set touches {len(changed)} files (limit {config.max_changed_files})",
                )
            )
        if diff.changed_lines > config.max_diff_lines:
            failures.append(
                _failure(
                    "diff-size",
                    FailureKind.SIZE_LIMIT,
                    f"change set has {diff.changed_lines} changed lines "
                    f"(limit {config.max_diff_lines})",
                )
            )
        forbidden = sorted(
            {
                path
                for path in changed
                if _has_forbidden_extension(path, config.forbidden_extensions)
            }
            | set(diff.binary_files)
        )
        if forbidden:
            failures.append(
                _failure(
                    "forbidden-files",
                    FailureKind.FORBIDDEN_FILE,
                    "forbidden binary files changed: " + ", ".join(forbidden),
                    log_tail="\n".join(forbidden),
                )
            )
        if diff.truncated:
            warnings.append("diff was truncated; tampering/size heuristics may be incomplete")

    # 4. Expected files (warn/fail per task or policy).
    if task.expected_files and changed:
        spec = PathSpec.from_lines("gitignore", list(task.expected_files))
        matched = [path for path in changed if spec.match_file(path)]
        if not matched:
            mode = _expected_files_mode(task, config)
            message = (
                "none of the expected files were touched: "
                + ", ".join(task.expected_files)
                + f" (changed: {', '.join(changed[:10])})"
            )
            if mode == "fail":
                failures.append(_failure("expected-files", FailureKind.EXPECTED_FILES, message))
            elif mode == "warn":
                warnings.append(message)

    # 5. Workspace existence is an infrastructure concern.
    if not Path(ws.path).is_dir():
        infra_error = True
        failures.append(
            _failure(
                "workspace",
                FailureKind.INFRA_ERROR,
                f"attempt workspace does not exist: {ws.path}",
            )
        )

    return PrecheckOutcome(failures=failures, warnings=warnings, infra_error=infra_error, diff=diff)


__all__ = ["PrecheckOutcome", "run_prechecks"]
