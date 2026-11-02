"""Deterministic repository analyzer.

Analysis runs entirely against a read-only bare repository snapshot.  It never
checks out, imports, builds, or executes repository code and requires no LLM
calls.  Detectors are small plugins registered per ecosystem; the analyzer
merges their findings into a single :class:`~devsandbox.common.types.RepoProfile`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..common.types import (
    CheckSpec,
    CommandConfidence,
    DetectedCommand,
    RepoHandle,
    RepoProfile,
)
from .detectors import DEFAULT_DETECTORS, Detection, Detector, RepoFiles
from .source import GitSnapshot, is_binary_path, is_ignored_path, is_secret_path

_EXTENSION_LANGUAGES: dict[str, str] = {
    ".bash": "Shell",
    ".c": "C",
    ".cc": "C++",
    ".clj": "Clojure",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".cxx": "C++",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".go": "Go",
    ".h": "C",
    ".hpp": "C++",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".mjs": "JavaScript",
    ".php": "PHP",
    ".py": "Python",
    ".pyi": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
}

_CODE_LANGUAGES = frozenset(_EXTENSION_LANGUAGES.values()) - {"HTML", "CSS", "SQL"}

_MANIFEST_NAMES = frozenset(
    {
        "Cargo.lock",
        "Cargo.toml",
        "Gemfile",
        "Gemfile.lock",
        "GNUmakefile",
        "Makefile",
        "Package.resolved",
        "Pipfile",
        "Pipfile.lock",
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "composer.lock",
        "go.mod",
        "go.sum",
        "makefile",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pom.xml",
        "poetry.lock",
        "pyproject.toml",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "uv.lock",
        "yarn.lock",
    }
)

_CONFIG_NAMES = frozenset(
    {
        ".editorconfig",
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.json",
        ".flake8",
        ".golangci.yml",
        ".golangci.yaml",
        ".nvmrc",
        ".node-version",
        ".pre-commit-config.yaml",
        ".prettierrc",
        ".prettierrc.js",
        ".prettierrc.json",
        ".python-version",
        ".ruff.toml",
        ".travis.yml",
        "checkstyle.xml",
        "clippy.toml",
        "eslint.config.js",
        "eslint.config.mjs",
        "jest.config.js",
        "jest.config.ts",
        "mypy.ini",
        "prettier.config.js",
        "pytest.ini",
        "ruff.toml",
        "rust-toolchain",
        "rust-toolchain.toml",
        "rustfmt.toml",
        "tox.ini",
        "tsconfig.json",
        "vitest.config.ts",
    }
)

_CI_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    ".travis.yml",
)

_DOC_GLOBS: tuple[str, ...] = ("docs/*.md", "docs/**/*.md", "doc/*.md", "doc/**/*.md")

_TEST_LINE_RE = re.compile(
    r"(?i)\b("
    r"pytest|py\.test|tox|nox|"
    r"go test|cargo test|cargo nextest|"
    r"npm test|npm run test|yarn test|pnpm test|bun test|"
    r"mvn test|mvnw test|gradle test|gradlew test|"
    r"make test|make check|rspec|phpunit|dotnet test|"
    r"python -m pytest|python -m unittest"
    r")\b"
)

_CONFIDENCE_ORDER: dict[CommandConfidence, int] = {
    CommandConfidence.HIGH: 0,
    CommandConfidence.MEDIUM: 1,
    CommandConfidence.LOW: 2,
}

_MAX_PRELOAD_FILES = 2_000
_MAX_PRELOAD_BYTES = 8_000_000
_MAX_PRELOAD_FILE_BYTES = 128_000
_MAX_TREE_LINES = 200
_TREE_DEPTH = 3
_README_EXCERPT_CHARS = 4_000


def _is_candidate(path: str, size: int) -> bool:
    if size > _MAX_PRELOAD_FILE_BYTES:
        return False
    if is_ignored_path(path) or is_binary_path(path) or is_secret_path(path):
        return False
    if "/" not in path:
        return True
    name = path.rsplit("/", 1)[-1]
    if name in _MANIFEST_NAMES or name in _CONFIG_NAMES:
        return True
    return any(
        path.startswith(prefix)
        for prefix in (
            ".github/workflows/",
            ".circleci/",
            "docs/",
            "doc/",
            "docker/",
        )
    )


async def _preload(snapshot: GitSnapshot) -> RepoFiles:
    selected: list[str] = []
    total_bytes = 0
    for entry in snapshot.entries:
        if not _is_candidate(entry.path, entry.size):
            continue
        if len(selected) >= _MAX_PRELOAD_FILES:
            break
        if total_bytes + entry.size > _MAX_PRELOAD_BYTES:
            break
        selected.append(entry.path)
        total_bytes += entry.size
    contents = await snapshot.read_texts(selected, max_chars_per_file=_MAX_PRELOAD_FILE_BYTES)
    return RepoFiles(paths=snapshot.files, contents=contents)


def _merge_detections(detections: list[Detection]) -> Detection:
    merged = Detection(ecosystem="")
    ecosystems: list[str] = []
    for detection in detections:
        if detection.detected:
            ecosystems.append(detection.ecosystem)
        merged.package_managers.extend(detection.package_managers)
        merged.manifests.extend(detection.manifests)
        merged.frameworks.extend(detection.frameworks)
        merged.build_commands.extend(detection.build_commands)
        merged.test_commands.extend(detection.test_commands)
        merged.lint_commands.extend(detection.lint_commands)
        merged.test_layout.extend(detection.test_layout)
        merged.conventions.extend(detection.conventions)
        merged.key_files.extend(detection.key_files)
        merged.warnings.extend(detection.warnings)
        for key, value in detection.environment_hints.items():
            merged.environment_hints.setdefault(key, value)

    def _dedupe(values: list[str]) -> list[str]:
        return sorted(set(values))

    merged.package_managers = _dedupe(merged.package_managers)
    merged.manifests = _dedupe(merged.manifests)
    merged.frameworks = _dedupe(merged.frameworks)
    merged.test_layout = _dedupe(merged.test_layout)
    merged.conventions = _dedupe(merged.conventions)
    merged.key_files = _dedupe(merged.key_files)
    merged.warnings = _dedupe(merged.warnings)
    merged.build_commands = _dedupe_commands(merged.build_commands)
    merged.test_commands = _dedupe_commands(merged.test_commands)
    merged.lint_commands = _dedupe_commands(merged.lint_commands)
    merged.ecosystem = ", ".join(sorted(set(ecosystems)))
    return merged


def _dedupe_commands(commands: list[DetectedCommand]) -> list[DetectedCommand]:
    best: dict[str, DetectedCommand] = {}
    for command in commands:
        existing = best.get(command.command)
        if existing is None:
            best[command.command] = command
            continue
        existing_key = (_CONFIDENCE_ORDER[existing.confidence], existing.source)
        new_key = (_CONFIDENCE_ORDER[command.confidence], command.source)
        if new_key < existing_key:
            best[command.command] = command
    return sorted(
        best.values(),
        key=lambda item: (_CONFIDENCE_ORDER[item.confidence], item.command, item.source),
    )


def _language_stats(
    snapshot: GitSnapshot, line_counts: dict[str, int]
) -> tuple[list[str], dict[str, float], str | None, int]:
    counts: dict[str, int] = {}
    loc_total = 0
    for entry in snapshot.entries:
        if is_ignored_path(entry.path) or is_binary_path(entry.path):
            continue
        if is_secret_path(entry.path):
            continue
        lines = line_counts.get(entry.path)
        if lines is not None:
            loc_total += lines
        language = _EXTENSION_LANGUAGES.get(PurePosixPath(entry.path).suffix.lower())
        if language and language in _CODE_LANGUAGES:
            counts[language] = counts.get(language, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return [], {}, None, loc_total
    share = {
        language: round(count / total, 3)
        for language, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    }
    languages = list(share)
    return languages, share, languages[0], loc_total


def _tree_summary(snapshot: GitSnapshot) -> list[str]:
    lines: set[str] = set()
    for entry in snapshot.entries:
        if is_ignored_path(entry.path) or is_binary_path(entry.path):
            continue
        parts = entry.path.split("/")
        limit = min(len(parts), _TREE_DEPTH)
        for depth in range(1, limit + 1):
            prefix = "/".join(parts[:depth])
            if depth < len(parts):
                lines.add(prefix + "/")
            elif not is_secret_path(entry.path):
                lines.add(entry.path)
    ordered = sorted(lines)
    if len(ordered) > _MAX_TREE_LINES:
        ordered = ordered[:_MAX_TREE_LINES]
        ordered.append("... (truncated)")
    return [("  " * (line.count("/") - line.endswith("/"))) + line for line in ordered]


def _ci_commands(files: RepoFiles, warnings: list[str]) -> tuple[list[str], list[DetectedCommand]]:
    paths = sorted({path for pattern in _CI_GLOBS for path in files.glob(pattern)})
    commands: list[DetectedCommand] = []
    for path in paths:
        text = files.read(path)
        if not text:
            continue
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            warnings.append(f"could not parse CI file {path}")
            continue
        for document in documents:
            for line in _walk_ci_strings(document):
                for candidate in line.splitlines():
                    stripped = candidate.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if _TEST_LINE_RE.search(stripped):
                        commands.append(
                            DetectedCommand(
                                command=stripped,
                                source=f"{path} run step",
                                confidence=CommandConfidence.MEDIUM,
                            )
                        )
    return paths, _dedupe_commands(commands)


def _walk_ci_strings(value: Any) -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            results.extend(_walk_ci_strings(item))
    elif isinstance(value, list):
        for item in value:
            results.extend(_walk_ci_strings(item))
    elif isinstance(value, str):
        results.append(value)
    return results


def _docs(files: RepoFiles) -> tuple[list[str], str]:
    found: list[str] = []
    readme_path = ""
    for path in files.paths:
        name = path.rsplit("/", 1)[-1].lower()
        if "/" not in path and name.startswith("readme"):
            found.append(path)
            if not readme_path:
                readme_path = path
        elif "/" not in path and (name.startswith("contributing") or name.startswith("changelog")):
            found.append(path)
    for pattern in _DOC_GLOBS:
        found.extend(files.glob(pattern))
    docs = sorted(set(found))[:50]
    excerpt = files.read(readme_path, max_chars=_README_EXCERPT_CHARS) if readme_path else ""
    return docs, (excerpt or "")


def _generic_conventions(files: RepoFiles) -> list[str]:
    conventions: list[str] = []
    for name, label in (
        (".editorconfig", "editorconfig"),
        (".pre-commit-config.yaml", "pre-commit"),
        (".gitattributes", "gitattributes"),
        (".gitignore", "gitignore"),
        ("Dockerfile", "docker"),
    ):
        if files.by_name(name):
            conventions.append(label)
    for path in files.paths:
        name = path.rsplit("/", 1)[-1]
        upper = name.upper()
        if "/" not in path and (upper.startswith("LICENSE") or upper.startswith("COPYING")):
            conventions.append(f"license ({name})")
        elif "/" not in path and upper.startswith("CONTRIBUTING"):
            conventions.append(f"contributing ({name})")
    return conventions


def _dockerfile_hints(files: RepoFiles) -> dict[str, str]:
    candidates = sorted(
        files.by_name("Dockerfile"),
        key=lambda path: (path.count("/"), path),
    )
    candidates.extend(
        sorted(
            (
                path
                for path in files.glob("**/Dockerfile*")
                if path not in candidates and not path.endswith(".dockerignore")
            ),
            key=lambda path: (path.count("/"), path),
        )
    )
    for path in candidates:
        text = files.read(path) or ""
        for line in text.splitlines():
            match = re.match(r"^\s*FROM\s+([^\s]+)", line, re.IGNORECASE)
            if match:
                return {"dockerfile_base_image": match.group(1)}
    return {}


def _spec_commands(
    validation_commands: Sequence[CheckSpec | str] | None,
) -> list[DetectedCommand]:
    commands: list[DetectedCommand] = []
    for index, check in enumerate(validation_commands or ()):
        if isinstance(check, CheckSpec):
            command = check.run
            name = check.name
        else:
            command = check
            name = f"command-{index + 1}"
        commands.append(
            DetectedCommand(
                command=command,
                source=f"spec.validation.commands[{index}] ({name})",
                confidence=CommandConfidence.HIGH,
            )
        )
    return _dedupe_commands(commands)


class RepoAnalyzer:
    """Deterministic analyzer implementing the ``analyze_repo`` protocol."""

    name = "repo-analyzer"

    def __init__(self, detectors: Sequence[Detector] | None = None) -> None:
        self._detectors = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS

    async def analyze_repo(
        self,
        repo: RepoHandle,
        revision: str,
        *,
        validation_commands: Sequence[CheckSpec | str] | None = None,
    ) -> RepoProfile:
        """Analyze ``revision`` of ``repo`` and return a deterministic profile."""
        snapshot = await GitSnapshot.open(repo.path, revision)
        files = await _preload(snapshot)
        detections = [detector.detect(files) for detector in self._detectors]
        merged = _merge_detections(detections)
        line_counts = await snapshot.line_counts()
        languages, language_share, primary_language, loc_total = _language_stats(
            snapshot, line_counts
        )
        ci_files, ci_commands = _ci_commands(files, merged.warnings)
        docs_found, readme_excerpt = _docs(files)

        build_commands = _dedupe_commands(merged.build_commands)
        test_commands = _dedupe_commands([*merged.test_commands, *ci_commands])
        lint_commands = _dedupe_commands(merged.lint_commands)

        spec_commands = _spec_commands(validation_commands)
        effective_test_commands = spec_commands or test_commands

        conventions = sorted(set(merged.conventions) | set(_generic_conventions(files)))
        environment_hints = dict(merged.environment_hints)
        environment_hints.update(_dockerfile_hints(files))
        environment_hints = dict(sorted(environment_hints.items()))

        warnings = list(merged.warnings)
        if not languages and snapshot.entries:
            warnings.append("no source language detected")
        ecosystems = [name for name in merged.ecosystem.split(", ") if name and name != "make"]
        if len(ecosystems) > 1:
            warnings.append(f"multiple ecosystems detected: {', '.join(ecosystems)}")
        if not test_commands and not effective_test_commands:
            warnings.append("no test command detected")
        if not merged.test_layout:
            warnings.append("no test files detected")
        if not snapshot.entries:
            warnings.append("repository has no tracked files")
        if not merged.manifests:
            warnings.append("no recognized project manifest found")
        if spec_commands:
            warnings.append("spec validation commands override detected test commands")

        suggested_image = "devsandbox/validator-base:latest" if snapshot.entries else None

        return RepoProfile(
            name=repo.project_id,
            revision=revision,
            file_count=len(snapshot.entries),
            total_loc_estimate=loc_total,
            tree_summary=_tree_summary(snapshot),
            languages=languages,
            language_share=language_share,
            primary_language=primary_language,
            package_managers=merged.package_managers,
            manifests=merged.manifests,
            frameworks=merged.frameworks,
            build_commands=[item.command for item in build_commands],
            test_commands=[item.command for item in effective_test_commands],
            lint_commands=[item.command for item in lint_commands],
            build_command_specs=build_commands,
            test_command_specs=test_commands,
            lint_command_specs=lint_commands,
            spec_commands=spec_commands,
            effective_test_commands=[item.command for item in effective_test_commands],
            test_layout=merged.test_layout,
            ci_files=ci_files,
            conventions=conventions,
            docs_found=sorted(set(docs_found)),
            readme_excerpt=readme_excerpt,
            environment_hints=environment_hints,
            suggested_validation_image=suggested_image,
            warnings=sorted(set(warnings)),
            key_files=merged.key_files,
            estimated_files=len(snapshot.entries),
        )


async def analyze_repo(
    repo: RepoHandle,
    revision: str,
    *,
    validation_commands: Sequence[CheckSpec | str] | None = None,
) -> RepoProfile:
    """Convenience wrapper around :class:`RepoAnalyzer`."""
    return await RepoAnalyzer().analyze_repo(
        repo, revision, validation_commands=validation_commands
    )


__all__ = ["RepoAnalyzer", "analyze_repo"]
