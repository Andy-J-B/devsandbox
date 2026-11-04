"""Heuristic test-tampering detection (plan 09).

Agents can make a failing suite "pass" by deleting tests, adding skip markers,
removing assertions, or weakening test/CI configuration.  This module does not
try to be a proof; it produces explainable signals that the validator either
fails or warns on according to policy.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from ..gitmgr.interfaces import ChangeSet
from .config import ValidationConfig
from .diff import DiffSummary
from .models import TamperingFinding, TamperingSignal

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/"
    r"|(^|/)test_[^/]+\.py$"
    r"|_test\.py$"
    r"|\.test\.[cm]?[jt]sx?$"
    r"|\.spec\.[cm]?[jt]sx?$"
    r"|_test\.go$"
    r"|_spec\.rb$",
    re.IGNORECASE,
)
_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpytest\.skip\b"),
    re.compile(r"@pytest\.mark\.skip"),
    re.compile(r"@pytest\.mark\.xfail"),
    re.compile(r"\bunittest\.skip\b"),
    re.compile(r"@unittest\.skip"),
    re.compile(r"\btest\.skip\s*\("),
    re.compile(r"\bit\.skip\s*\("),
    re.compile(r"\bdescribe\.skip\s*\("),
    re.compile(r"\bxit\s*\("),
    re.compile(r"\bxdescribe\s*\("),
    re.compile(r"@Ignore\b"),
    re.compile(r"@Disabled\b"),
    re.compile(r"\bt\.Skip(?:f|Now)?\s*\("),
    re.compile(r"#\[ignore\]"),
)
_ASSERT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bassert\b"),
    re.compile(r"\bassertEqual\b"),
    re.compile(r"\bassertThat\b"),
    re.compile(r"\bexpect\s*\("),
    re.compile(r"\.should\b"),
)
_PYTEST_CONFIG_MARKERS = ("[tool.pytest", "testpaths", "addopts", "[pytest]")


def is_test_path(path: str) -> bool:
    """Return whether ``path`` looks like a test file."""
    return bool(_TEST_PATH_RE.search(path.replace("\\", "/")))


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _is_test_config(path: str, text: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    if name == "pyproject.toml":
        return any(marker in text for marker in _PYTEST_CONFIG_MARKERS)
    # Match both the full path (for ``.github/workflows/**``) and the basename
    # (for nested ``frontend/jest.config.js``).
    return _matches_any(path, patterns) or _matches_any(name, patterns)


def _diff_lines(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(added, removed)`` as ``(path, line)`` pairs."""
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    current = ""
    for raw_line in text.splitlines():
        if raw_line.startswith("+++ b/"):
            current = raw_line[6:]
            continue
        if raw_line.startswith("+++ "):
            current = raw_line[4:]
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            added.append((current, raw_line[1:]))
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            removed.append((current, raw_line[1:]))
    return added, removed


def detect_tampering(
    cs: ChangeSet,
    diff: DiffSummary,
    config: ValidationConfig | None = None,
) -> TamperingSignal:
    """Detect suspicious test/CI edits in a change set."""
    config = config or ValidationConfig()
    action = config.tamper_mode
    findings: list[TamperingFinding] = []
    # Union of both sources so an incomplete change set cannot hide an edit.
    files = list(dict.fromkeys([*cs.files_changed, *diff.files_changed]))

    for path in diff.deleted_files:
        if is_test_path(path):
            findings.append(
                TamperingFinding(
                    kind="deleted_test",
                    path=path,
                    detail="test file deleted by the change set",
                )
            )

    for path in files:
        if path.startswith(".github/workflows/"):
            findings.append(
                TamperingFinding(
                    kind="disabled_ci",
                    path=path,
                    detail="CI workflow edited by the change set",
                )
            )
        elif _is_test_config(path, diff.text, config.test_config_patterns):
            findings.append(
                TamperingFinding(
                    kind="edited_test_config",
                    path=path,
                    detail="test/CI configuration edited by the change set",
                )
            )

    added, removed = _diff_lines(diff.text)
    for path, line in added:
        for pattern in _SKIP_PATTERNS:
            if pattern.search(line):
                findings.append(
                    TamperingFinding(
                        kind="skipped_test",
                        path=path or "<unknown>",
                        detail=f"added skip/disable marker: {line.strip()[:200]}",
                    )
                )
                break

    removed_asserts: dict[str, int] = {}
    added_asserts: dict[str, int] = {}
    for path, line in removed:
        if any(pattern.search(line) for pattern in _ASSERT_PATTERNS):
            removed_asserts[path] = removed_asserts.get(path, 0) + 1
    for path, line in added:
        if any(pattern.search(line) for pattern in _ASSERT_PATTERNS):
            added_asserts[path] = added_asserts.get(path, 0) + 1
    threshold = config.assert_removal_threshold
    if threshold > 0:
        for path, count in sorted(removed_asserts.items()):
            net = count - added_asserts.get(path, 0)
            if net >= threshold:
                findings.append(
                    TamperingFinding(
                        kind="removed_assert",
                        path=path or "<unknown>",
                        detail=f"{net} more assertions removed than added",
                    )
                )

    # De-duplicate while preserving order (a file can produce several findings).
    unique: list[TamperingFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding.kind, finding.path, finding.detail)
        if key not in seen:
            seen.add(key)
            unique.append(finding)

    return TamperingSignal(detected=bool(unique), action=action, findings=unique)


__all__ = ["detect_tampering", "is_test_path"]
