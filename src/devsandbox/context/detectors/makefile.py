"""Generic Makefile detector."""

from __future__ import annotations

import re

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)")
_TEST_TARGETS = ("test", "tests", "check", "test-all", "unit", "test-unit")
_BUILD_TARGETS = ("build", "all", "compile")
_LINT_TARGETS = ("lint", "format", "fmt", "style")


def _targets(files: RepoFiles) -> tuple[str, list[str]]:
    for name in ("GNUmakefile", "Makefile", "makefile"):
        text = files.read(name)
        if text is None:
            continue
        targets: list[str] = []
        for raw_line in text.splitlines():
            if raw_line.startswith(("\t", "#", " ")):
                continue
            match = _TARGET_RE.match(raw_line)
            if match and not match.group(1).startswith("."):
                targets.append(match.group(1))
        return name, targets
    return "", []


class MakefileDetector:
    """Detect explicit Makefile test/build/lint targets."""

    name = "make"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Makefile findings for ``files``."""
        manifest, targets = _targets(files)
        if not manifest:
            return Detection(ecosystem=self.name)
        target_set = set(targets)

        test_commands: list[DetectedCommand] = []
        for target in _TEST_TARGETS:
            if target in target_set:
                test_commands.append(
                    DetectedCommand(
                        command=f"make {target}",
                        source=f"{manifest} target {target}",
                        confidence=CommandConfidence.HIGH,
                    )
                )
                break

        build_commands: list[DetectedCommand] = []
        for target in _BUILD_TARGETS:
            if target in target_set:
                build_commands.append(
                    DetectedCommand(
                        command=f"make {target}",
                        source=f"{manifest} target {target}",
                        confidence=CommandConfidence.MEDIUM,
                    )
                )
                break

        lint_commands: list[DetectedCommand] = []
        for target in _LINT_TARGETS:
            if target in target_set:
                lint_commands.append(
                    DetectedCommand(
                        command=f"make {target}",
                        source=f"{manifest} target {target}",
                        confidence=CommandConfidence.MEDIUM,
                    )
                )
                break

        return Detection(
            ecosystem=self.name,
            package_managers=["make"],
            manifests=[manifest],
            build_commands=build_commands,
            test_commands=test_commands,
            lint_commands=lint_commands,
            key_files=[manifest],
        )


__all__ = ["MakefileDetector"]
