"""Rust ecosystem detector."""

from __future__ import annotations

import tomllib
from typing import Any

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_FRAMEWORKS: dict[str, str] = {
    "axum": "Axum",
    "actix-web": "Actix Web",
    "rocket": "Rocket",
    "warp": "Warp",
    "tonic": "gRPC",
    "tokio": "Tokio",
}


def _cargo(files: RepoFiles, path: str) -> dict[str, Any]:
    text = files.read(path)
    if not text:
        return {}
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class RustDetector:
    """Detect Cargo workspaces and standard Cargo commands."""

    name = "rust"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Rust findings for ``files``."""
        has_rust = any(path.endswith(".rs") for path in files.paths)
        cargo_paths = files.by_name("Cargo.toml")
        if not cargo_paths and not has_rust:
            return Detection(ecosystem=self.name)

        manifests: list[str] = [*cargo_paths, *files.by_name("Cargo.lock")]
        dependencies: dict[str, Any] = {}
        for path in cargo_paths:
            data = _cargo(files, path)
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                block = data.get(key)
                if isinstance(block, dict):
                    dependencies.update(block)
            workspace_deps = data.get("workspace", {})
            if isinstance(workspace_deps, dict) and isinstance(
                workspace_deps.get("dependencies"), dict
            ):
                dependencies.update(workspace_deps["dependencies"])
        frameworks = sorted({_FRAMEWORKS[name] for name in dependencies if name in _FRAMEWORKS})

        test_commands = [
            DetectedCommand(
                command="cargo test",
                source="Cargo.toml found",
                confidence=CommandConfidence.HIGH,
            )
        ]
        build_commands = [
            DetectedCommand(
                command="cargo build",
                source="Cargo.toml found",
                confidence=CommandConfidence.HIGH,
            )
        ]
        lint_commands = [
            DetectedCommand(
                command="cargo clippy -- -D warnings",
                source="Cargo.toml found",
                confidence=CommandConfidence.MEDIUM,
            )
        ]

        test_layout: list[str] = []
        if any(path.startswith("tests/") for path in files.paths):
            test_layout.append("tests/")
        if files.glob("**/benches/*.rs"):
            test_layout.append("benches/")

        environment_hints: dict[str, str] = {}
        for name in files.by_name("rust-toolchain", "rust-toolchain.toml"):
            text = files.read(name)
            if text and text.strip():
                environment_hints["rust_version"] = text.strip().splitlines()[0].strip()
                break

        conventions: list[str] = []
        if files.by_name("rustfmt.toml", ".rustfmt.toml"):
            conventions.append("rustfmt")
        if files.by_name("clippy.toml", ".clippy.toml"):
            conventions.append("clippy")

        return Detection(
            ecosystem=self.name,
            package_managers=["cargo"] if manifests else [],
            manifests=sorted(manifests),
            frameworks=frameworks,
            build_commands=build_commands,
            test_commands=test_commands,
            lint_commands=lint_commands,
            test_layout=test_layout,
            conventions=conventions,
            environment_hints=environment_hints,
            key_files=sorted(manifests),
        )


__all__ = ["RustDetector"]
