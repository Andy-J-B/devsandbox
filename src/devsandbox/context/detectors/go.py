"""Go ecosystem detector."""

from __future__ import annotations

import re

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_FRAMEWORKS: dict[str, str] = {
    "github.com/gin-gonic/gin": "Gin",
    "github.com/labstack/echo": "Echo",
    "github.com/gofiber/fiber": "Fiber",
    "github.com/go-chi/chi": "chi",
    "google.golang.org/grpc": "gRPC",
}

_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)
_GO_RE = re.compile(r"^\s*go\s+(\S+)", re.MULTILINE)


def _go_mod(files: RepoFiles, path: str) -> tuple[str, str, list[str]]:
    text = files.read(path) or ""
    module_match = _MODULE_RE.search(text)
    version_match = _GO_RE.search(text)
    requires: list[str] = []
    in_require = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if line.startswith("require ("):
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        if in_require and line:
            requires.append(line.split()[0])
        elif line.startswith("require "):
            parts = line.split()
            if len(parts) >= 2:
                requires.append(parts[1])
    return (
        module_match.group(1) if module_match else "",
        version_match.group(1) if version_match else "",
        requires,
    )


class GoDetector:
    """Detect Go modules and standard Go commands."""

    name = "go"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Go findings for ``files``."""
        has_go = any(path.endswith(".go") for path in files.paths)
        go_mods = files.by_name("go.mod")
        if not go_mods and not has_go:
            return Detection(ecosystem=self.name)

        manifests: list[str] = [*go_mods, *files.by_name("go.sum")]
        go_version = ""
        requires: list[str] = []
        for path in go_mods:
            _module, version, deps = _go_mod(files, path)
            go_version = go_version or version
            requires.extend(deps)
        frameworks = sorted({_FRAMEWORKS[dep] for dep in requires if dep in _FRAMEWORKS})

        test_commands = [
            DetectedCommand(
                command="go test ./...",
                source="go.mod found",
                confidence=CommandConfidence.HIGH,
            )
        ]
        build_commands = [
            DetectedCommand(
                command="go build ./...",
                source="go.mod found",
                confidence=CommandConfidence.HIGH,
            )
        ]
        lint_commands: list[DetectedCommand] = []
        if files.by_name(".golangci.yml", ".golangci.yaml"):
            lint_commands.append(
                DetectedCommand(
                    command="golangci-lint run",
                    source=".golangci.yml config found",
                    confidence=CommandConfidence.HIGH,
                )
            )
        else:
            lint_commands.append(
                DetectedCommand(
                    command="go vet ./...",
                    source="go.mod found",
                    confidence=CommandConfidence.MEDIUM,
                )
            )

        test_layout: list[str] = []
        if any(path.endswith("_test.go") for path in files.paths):
            test_layout.append("*_test.go")
        for directory in ("tests", "test"):
            if any(path.startswith(f"{directory}/") for path in files.paths):
                test_layout.append(f"{directory}/")

        environment_hints: dict[str, str] = {}
        if go_version:
            environment_hints["go_version"] = go_version

        return Detection(
            ecosystem=self.name,
            package_managers=["go"] if manifests else [],
            manifests=sorted(manifests),
            frameworks=frameworks,
            build_commands=build_commands,
            test_commands=test_commands,
            lint_commands=lint_commands,
            test_layout=sorted(set(test_layout)),
            conventions=[],
            environment_hints=environment_hints,
            key_files=sorted(manifests),
        )


__all__ = ["GoDetector"]
