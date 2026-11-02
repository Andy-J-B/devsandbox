"""Deterministic repo analyzer and ecosystem detector tests (plan 05)."""

from __future__ import annotations

import json

import pytest
from tests.context_helpers import build_fixture_repo

from devsandbox.common.types import CheckSpec, CommandConfidence
from devsandbox.context.analyzer import RepoAnalyzer, analyze_repo
from devsandbox.context.detectors import DEFAULT_DETECTORS, Detection, Detector, RepoFiles
from devsandbox.context.detectors.python import PythonDetector


def _commands(profile, kind: str) -> dict[str, tuple[str, str]]:
    specs = getattr(profile, f"{kind}_command_specs")
    return {item.command: (item.source, item.confidence.value) for item in specs}


async def test_python_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert profile.revision == fixture.revision
    assert profile.primary_language == "Python"
    assert "Python" in profile.languages
    assert profile.file_count > 0
    assert profile.total_loc_estimate > 0
    assert "pyproject.toml" in profile.manifests
    assert "pip" in profile.package_managers
    assert "FastAPI" in profile.frameworks
    assert _commands(profile, "test")["pytest"] == (
        "pyproject.toml [tool.pytest.ini_options]",
        "high",
    )
    assert "python -m build" in _commands(profile, "build")
    assert "ruff check ." in _commands(profile, "lint")
    assert "mypy ." in _commands(profile, "lint")
    assert profile.ci_files == [".github/workflows/ci.yml"]
    assert "README.md" in profile.docs_found
    assert profile.environment_hints["python_version"] == "3.12"
    assert profile.suggested_validation_image == "devsandbox/validator-base:latest"
    assert any(line.startswith("src/") for line in profile.tree_summary)
    assert profile.readme_excerpt.startswith("# Fixture Python")


async def test_node_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "node-jest")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert {"JavaScript", "TypeScript"} <= set(profile.languages)
    assert {"React", "Jest"} <= set(profile.frameworks)
    assert profile.package_managers == ["npm"]
    assert _commands(profile, "test")["npm test"] == (
        "package.json scripts.test",
        "high",
    )
    assert "npm run build" in _commands(profile, "build")
    assert "npm run lint" in _commands(profile, "lint")
    assert profile.environment_hints["node_version"] == ">=20"
    assert "*.test.*" in profile.test_layout
    assert "tsconfig.json" in profile.key_files


async def test_go_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert profile.primary_language == "Go"
    assert profile.package_managers == ["go"]
    assert "Gin" in profile.frameworks
    assert _commands(profile, "test")["go test ./..."] == ("go.mod found", "high")
    assert "go vet ./..." in _commands(profile, "lint")
    assert profile.environment_hints["go_version"] == "1.22"
    assert "*_test.go" in profile.test_layout


async def test_mixed_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "mixed")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert {"Python", "TypeScript"} <= set(profile.languages)
    assert {"FastAPI", "React", "Next.js"} <= set(profile.frameworks)
    assert {"npm", "pip"} <= set(profile.package_managers)
    assert any("multiple ecosystems" in warning for warning in profile.warnings)
    assert {"npm test", "pytest"} <= set(profile.test_commands)


async def test_empty_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "empty")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert profile.file_count == 0
    assert profile.languages == []
    assert profile.primary_language is None
    assert profile.manifests == []
    assert profile.test_commands == []
    assert profile.suggested_validation_image is None
    assert "repository has no tracked files" in profile.warnings


async def test_no_tests_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "no-tests")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert profile.primary_language == "Python"
    assert "setup.py" in profile.manifests
    assert profile.test_command_specs == []
    assert "no test command detected" in profile.warnings


async def test_monorepo_profile(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "monorepo")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    assert {"Python", "TypeScript"} <= set(profile.languages)
    assert {"pnpm", "pip", "make"} <= set(profile.package_managers)
    assert "React" in profile.frameworks
    assert {"pnpm test", "pytest", "make test"} <= set(profile.test_commands)
    assert "apps/web/package.json" in profile.manifests
    assert "services/api/pyproject.toml" in profile.manifests
    assert any("multiple ecosystems" in warning for warning in profile.warnings)


async def test_profile_is_byte_identical_for_same_revision(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    analyzer = RepoAnalyzer()
    first = await analyzer.analyze_repo(fixture.handle, fixture.revision)
    second = await analyzer.analyze_repo(fixture.handle, fixture.revision)

    assert first.model_dump_json() == second.model_dump_json()
    assert json.loads(first.model_dump_json()) == json.loads(second.model_dump_json())


async def test_spec_validation_commands_override_detected(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    override = CheckSpec(name="unit", run="python -m pytest -q", timeout_seconds=60)
    profile = await analyze_repo(fixture.handle, fixture.revision, validation_commands=[override])

    assert profile.test_commands == ["python -m pytest -q"]
    assert profile.effective_test_commands == ["python -m pytest -q"]
    assert "pytest" in {item.command for item in profile.test_command_specs}
    assert profile.spec_commands[0].source == "spec.validation.commands[0] (unit)"
    assert "spec validation commands override" in " ".join(profile.warnings)


async def test_analysis_never_executes_repo_code(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    await analyze_repo(fixture.handle, fixture.revision)

    assert not (fixture.source / "CONFTEST_CANARY_EXECUTED").exists()
    assert not (fixture.source / "SETUP_CANARY_EXECUTED").exists()
    assert list(tmp_path.rglob("*CANARY_EXECUTED")) == []


def test_builtin_detectors_conform_to_protocol() -> None:
    assert DEFAULT_DETECTORS
    assert all(isinstance(detector, Detector) for detector in DEFAULT_DETECTORS)


def test_python_detector_is_independently_testable() -> None:
    files = RepoFiles(
        paths=("pyproject.toml", "src/app.py", "tests/test_app.py"),
        contents={
            "pyproject.toml": (
                "[project]\nname='x'\ndependencies=['pytest']\n"
                "[tool.pytest.ini_options]\ntestpaths=['tests']\n"
            )
        },
    )
    detection = PythonDetector().detect(files)
    assert isinstance(detection, Detection)
    assert detection.detected
    assert detection.test_commands[0].command == "pytest"
    assert detection.test_commands[0].confidence is CommandConfidence.HIGH


@pytest.mark.parametrize(
    ("fixture_name", "expected_language"),
    [
        ("python-pytest", "Python"),
        ("node-jest", "TypeScript"),
        ("go", "Go"),
    ],
)
async def test_primary_language_detection(tmp_path, fixture_name, expected_language) -> None:
    fixture = build_fixture_repo(tmp_path, fixture_name)
    profile = await analyze_repo(fixture.handle, fixture.revision)
    assert profile.primary_language == expected_language
