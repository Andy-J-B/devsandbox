"""Unit tests for the test-tampering heuristics (plan 09)."""

from __future__ import annotations

import pytest

from devsandbox.gitmgr.interfaces import ChangeSet
from devsandbox.validation.config import ValidationConfig
from devsandbox.validation.diff import DiffSummary
from devsandbox.validation.tampering import detect_tampering, is_test_path


def _cs(*files: str) -> ChangeSet:
    return ChangeSet(base_commit="base", head_commit="head", files_changed=list(files))


def _diff(*, text: str = "", **overrides: object) -> DiffSummary:
    return DiffSummary(text=text, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("src/app_test.py", True),
        ("src/__tests__/app.ts", True),
        ("src/app.test.ts", True),
        ("src/app.spec.js", True),
        ("pkg/server_test.go", True),
        ("spec/models/user_spec.rb", True),
        ("src/app.py", False),
        ("docs/testing.md", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


def test_deleted_test_file_is_flagged() -> None:
    signal = detect_tampering(
        _cs("tests/test_app.py"),
        _diff(deleted_files=["tests/test_app.py"], files_changed=["tests/test_app.py"]),
    )

    assert signal.detected
    assert signal.findings[0].kind == "deleted_test"
    assert signal.findings[0].path == "tests/test_app.py"


def test_deleted_non_test_file_is_not_flagged() -> None:
    signal = detect_tampering(
        _cs("src/old.py"),
        _diff(deleted_files=["src/old.py"], files_changed=["src/old.py"]),
    )

    assert not signal.detected


def test_added_pytest_skip_is_flagged() -> None:
    text = (
        "diff --git a/tests/test_app.py b/tests/test_app.py\n"
        "--- a/tests/test_app.py\n"
        "+++ b/tests/test_app.py\n"
        "@@ -1 +1 @@\n"
        "-def test_value():\n"
        "+@pytest.mark.skip(reason='flaky')\n"
        "+def test_value():\n"
    )
    signal = detect_tampering(_cs("tests/test_app.py"), _diff(text=text))

    assert signal.detected
    assert signal.findings[0].kind == "skipped_test"
    assert "skip" in signal.findings[0].detail


@pytest.mark.parametrize(
    "marker",
    [
        'pytest.skip("because")',
        '@pytest.mark.skip(reason="x")',
        "@pytest.mark.xfail",
        'unittest.skip("x")',
        'test.skip("x", () => {})',
        'it.skip("x", () => {})',
        'xit("x", () => {})',
        "@Ignore",
        "@Disabled",
        't.Skip("x")',
        "#[ignore]",
    ],
)
def test_skip_markers_are_flagged(marker: str) -> None:
    text = f"+++ b/tests/test_app.py\n+{marker}\n"
    signal = detect_tampering(_cs("tests/test_app.py"), _diff(text=text))

    assert any(finding.kind == "skipped_test" for finding in signal.findings)


def test_removed_assertions_are_flagged() -> None:
    text = "+++ b/src/app.py\n-assert first\n-assert second\n+return True\n"
    signal = detect_tampering(
        _cs("src/app.py"),
        _diff(text=text),
        ValidationConfig(assert_removal_threshold=2),
    )

    assert any(finding.kind == "removed_assert" for finding in signal.findings)
    assert "2 more assertions removed" in signal.findings[0].detail


def test_balanced_assertions_are_not_flagged() -> None:
    text = "+++ b/src/app.py\n-assert first\n-assert second\n+assert first\n+assert second\n"
    signal = detect_tampering(_cs("src/app.py"), _diff(text=text))

    assert not signal.detected


def test_assert_removal_threshold_can_be_disabled() -> None:
    text = "+++ b/src/app.py\n-assert first\n-assert second\n"
    signal = detect_tampering(
        _cs("src/app.py"),
        _diff(text=text),
        ValidationConfig(assert_removal_threshold=0),
    )

    assert not signal.detected


def test_test_config_edit_is_flagged() -> None:
    signal = detect_tampering(_cs("conftest.py"), _diff(files_changed=["conftest.py"]))

    assert signal.detected
    assert signal.findings[0].kind == "edited_test_config"


def test_nested_jest_config_is_flagged_by_basename() -> None:
    signal = detect_tampering(
        _cs("frontend/jest.config.js"),
        _diff(files_changed=["frontend/jest.config.js"]),
    )

    assert signal.detected
    assert signal.findings[0].kind == "edited_test_config"


def test_custom_test_config_patterns_are_honoured() -> None:
    config = ValidationConfig(test_config_patterns=["custom-test.cfg"])
    signal = detect_tampering(
        _cs("custom-test.cfg", "conftest.py"),
        _diff(files_changed=["custom-test.cfg", "conftest.py"]),
        config,
    )

    assert [finding.path for finding in signal.findings] == ["custom-test.cfg"]


def test_pyproject_without_pytest_config_is_not_flagged() -> None:
    signal = detect_tampering(
        _cs("pyproject.toml"),
        _diff(text="+++ b/pyproject.toml\n+name = 'x'\n", files_changed=["pyproject.toml"]),
    )

    assert not signal.detected


def test_pyproject_with_pytest_config_is_flagged() -> None:
    signal = detect_tampering(
        _cs("pyproject.toml"),
        _diff(
            text="+++ b/pyproject.toml\n+[tool.pytest.ini_options]\n",
            files_changed=["pyproject.toml"],
        ),
    )

    assert signal.detected
    assert signal.findings[0].kind == "edited_test_config"


def test_ci_workflow_edit_is_flagged() -> None:
    signal = detect_tampering(
        _cs(".github/workflows/ci.yml"),
        _diff(files_changed=[".github/workflows/ci.yml"]),
    )

    assert signal.detected
    assert signal.findings[0].kind == "disabled_ci"


def test_unrelated_change_is_clean() -> None:
    text = "+++ b/src/app.py\n+value = 1\n"
    signal = detect_tampering(_cs("src/app.py"), _diff(text=text))

    assert not signal.detected
    assert signal.summary == "no test-tampering signals"


def test_action_comes_from_config() -> None:
    signal = detect_tampering(
        _cs("conftest.py"),
        _diff(files_changed=["conftest.py"]),
        ValidationConfig(tamper_mode="warn"),
    )

    assert signal.detected
    assert signal.action == "warn"


def test_findings_are_deduplicated() -> None:
    text = "+++ b/tests/test_app.py\n+pytest.skip('a')\n+pytest.skip('a')\n"
    signal = detect_tampering(_cs("tests/test_app.py"), _diff(text=text))

    assert len(signal.findings) == 1
