"""Node.js / TypeScript ecosystem detector."""

from __future__ import annotations

import json
from typing import Any

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_FRAMEWORKS: dict[str, str] = {
    "react": "React",
    "react-dom": "React",
    "next": "Next.js",
    "vue": "Vue",
    "nuxt": "Nuxt",
    "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit",
    "express": "Express",
    "fastify": "Fastify",
    "koa": "Koa",
    "@nestjs/core": "NestJS",
    "angular": "Angular",
    "@angular/core": "Angular",
    "jest": "Jest",
    "vitest": "Vitest",
    "mocha": "Mocha",
}

_LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")


def _package_json(files: RepoFiles, path: str) -> dict[str, Any]:
    text = files.read(path)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dependencies(data: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            merged.update({str(name): str(version) for name, version in block.items()})
    return merged


def _manager(files: RepoFiles, data: dict[str, Any]) -> str:
    declared = data.get("packageManager")
    if isinstance(declared, str):
        prefix = declared.split("@", 1)[0].strip()
        if prefix:
            return prefix
    for lock, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("package-lock.json", "npm"),
    ):
        if files.by_name(lock):
            return manager
    return "npm"


def _script_command(manager: str, script: str) -> str:
    if script == "test":
        return f"{manager} test" if manager != "npm" else "npm test"
    return f"{manager} run {script}"


def _first_scripts(packages: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    if "package.json" in packages:
        data = packages["package.json"]
    elif packages:
        data = packages[sorted(packages)[0]]
    else:
        data = {}
    scripts = data.get("scripts")
    return (
        "package.json"
        if "package.json" in packages
        else (sorted(packages)[0] if packages else "package.json"),
        scripts if isinstance(scripts, dict) else {},
    )


class NodeDetector:
    """Detect Node/TypeScript projects and package.json scripts."""

    name = "node"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Node findings for ``files``."""
        package_paths = files.by_name("package.json")
        packages = {path: _package_json(files, path) for path in package_paths}
        manifests: list[str] = [*package_paths]
        manifests.extend(files.by_name(*_LOCKFILES))
        has_js = any(
            path.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"))
            for path in files.paths
        )
        manifests = sorted(set(manifests))
        if not manifests and not has_js:
            return Detection(ecosystem=self.name)

        root_data = packages.get("package.json", {})
        script_path, scripts = _first_scripts(packages)
        dependencies: dict[str, str] = {}
        for data in packages.values():
            dependencies.update(_dependencies(data))
        manager = _manager(files, root_data)
        frameworks = sorted({_FRAMEWORKS[name] for name in dependencies if name in _FRAMEWORKS})

        build_commands: list[DetectedCommand] = []
        if "build" in scripts:
            build_commands.append(
                DetectedCommand(
                    command=_script_command(manager, "build"),
                    source=f"{script_path} scripts.build",
                    confidence=CommandConfidence.HIGH,
                )
            )

        test_commands: list[DetectedCommand] = []
        if "test" in scripts:
            test_commands.append(
                DetectedCommand(
                    command=_script_command(manager, "test"),
                    source=f"{script_path} scripts.test",
                    confidence=CommandConfidence.HIGH,
                )
            )
        elif (
            files.glob("**/*.test.*")
            or files.glob("**/*.spec.*")
            or files.by_name("jest.config.js", "jest.config.ts", "vitest.config.ts")
        ):
            test_commands.append(
                DetectedCommand(
                    command=_script_command(manager, "test"),
                    source="test files found",
                    confidence=CommandConfidence.LOW,
                )
            )

        lint_commands: list[DetectedCommand] = []
        if "lint" in scripts:
            lint_commands.append(
                DetectedCommand(
                    command=_script_command(manager, "lint"),
                    source=f"{script_path} scripts.lint",
                    confidence=CommandConfidence.HIGH,
                )
            )
        elif files.by_name(".eslintrc", ".eslintrc.js", ".eslintrc.json") or files.glob(
            "**/eslint.config.*"
        ):
            lint_commands.append(
                DetectedCommand(
                    command="npx eslint .",
                    source="eslint config found",
                    confidence=CommandConfidence.MEDIUM,
                )
            )
        if "format" in scripts:
            lint_commands.append(
                DetectedCommand(
                    command=_script_command(manager, "format"),
                    source=f"{script_path} scripts.format",
                    confidence=CommandConfidence.MEDIUM,
                )
            )

        test_layout: list[str] = []
        for directory in ("__tests__", "tests", "test", "spec"):
            if any(f"/{directory}/" in f"/{path}" for path in files.paths):
                test_layout.append(f"{directory}/")
        if files.glob("**/*.test.*"):
            test_layout.append("*.test.*")
        if files.glob("**/*.spec.*"):
            test_layout.append("*.spec.*")

        conventions: list[str] = []
        if files.by_name(".eslintrc", ".eslintrc.js", ".eslintrc.json") or files.glob(
            "**/eslint.config.*"
        ):
            conventions.append("eslint")
        if files.by_name(".prettierrc", ".prettierrc.json", ".prettierrc.js") or files.glob(
            "**/prettier.config.*"
        ):
            conventions.append("prettier")
        if files.by_name("tsconfig.json"):
            conventions.append("typescript")
        if files.by_name("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
            conventions.append("lockfile")

        environment_hints: dict[str, str] = {}
        engines = root_data.get("engines")
        if isinstance(engines, dict) and isinstance(engines.get("node"), str):
            environment_hints["node_version"] = str(engines["node"])
        for version_file in files.by_name(".nvmrc", ".node-version"):
            text = files.read(version_file)
            if text and text.strip():
                environment_hints["node_version"] = text.strip().splitlines()[0].strip()
                break
        environment_hints["package_manager"] = manager

        key_files = [*manifests]
        key_files.extend(
            files.by_name(
                "tsconfig.json",
                "jest.config.js",
                "jest.config.ts",
                "vitest.config.ts",
                "eslint.config.js",
            )
        )

        return Detection(
            ecosystem=self.name,
            package_managers=[manager],
            manifests=manifests,
            frameworks=frameworks,
            build_commands=build_commands,
            test_commands=test_commands,
            lint_commands=lint_commands,
            test_layout=sorted(set(test_layout)),
            conventions=sorted(set(conventions)),
            environment_hints=environment_hints,
            key_files=sorted(set(key_files)),
        )


__all__ = ["NodeDetector"]
