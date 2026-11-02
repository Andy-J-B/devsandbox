"""Python ecosystem detector (pyproject/setup/requirements/pytest)."""

from __future__ import annotations

import tomllib
from typing import Any

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_FRAMEWORKS: dict[str, str] = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "starlette": "Starlette",
    "tornado": "Tornado",
    "aiohttp": "aiohttp",
    "pydantic": "Pydantic",
    "sqlalchemy": "SQLAlchemy",
    "celery": "Celery",
    "pytest": "pytest",
}

_TEST_CONFIG_NAMES = ("pytest.ini", "tox.ini", "setup.cfg")


def _normalise_requirement(value: str) -> str:
    name = value.strip().split(";", 1)[0].split("=", 1)[0]
    for marker in ("[", "<", ">", "~", "!", " "):
        name = name.split(marker, 1)[0]
    return name.strip().lower().replace("_", "-")


def _read_requirements(files: RepoFiles) -> list[str]:
    names: list[str] = []
    for path in files.glob("**/requirements*.txt"):
        text = files.read(path) or ""
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.append(_normalise_requirement(line))
    return names


def _pyproject_data(files: RepoFiles, path: str) -> dict[str, Any]:
    text = files.read(path)
    if not text:
        return {}
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pyproject_dependencies(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            names.extend(_normalise_requirement(str(item)) for item in dependencies)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    names.extend(_normalise_requirement(str(item)) for item in group)
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        for section in ("dependencies", "dev-dependencies"):
            deps = poetry.get(section)
            if isinstance(deps, dict):
                names.extend(str(key).lower().replace("_", "-") for key in deps)
    return names


def _has_pytest_config(files: RepoFiles, pyprojects: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    for path in sorted(pyprojects):
        data = pyprojects[path]
        tool = data.get("tool")
        if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
            return True, f"{path} [tool.pytest.ini_options]"
    for path in files.by_name(*_TEST_CONFIG_NAMES):
        text = files.read(path)
        if text and ("[tool:pytest]" in text or "[pytest]" in text or "[tool.pytest" in text):
            return True, path
    return False, ""


def _python_version(files: RepoFiles, pyprojects: dict[str, dict[str, Any]]) -> str | None:
    for path in files.by_name(".python-version"):
        text = files.read(path)
        if text and text.strip():
            return text.strip().splitlines()[0].strip()
    for data in pyprojects.values():
        project = data.get("project")
        if isinstance(project, dict):
            requires = project.get("requires-python")
            if isinstance(requires, str) and requires:
                return requires
    return None


def _tool_table(data: dict[str, Any]) -> dict[str, Any]:
    tool = data.get("tool")
    return tool if isinstance(tool, dict) else {}


class PythonDetector:
    """Detect Python projects, package managers, frameworks and commands."""

    name = "python"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Python findings for ``files``."""
        pyproject_paths = files.by_name("pyproject.toml")
        pyprojects = {path: _pyproject_data(files, path) for path in pyproject_paths}
        manifests: list[str] = [*pyproject_paths]
        manifests.extend(files.by_name("setup.py", "setup.cfg", "Pipfile"))
        manifests.extend(files.by_name("pyproject.toml"))
        for path in files.glob("**/requirements*.txt"):
            manifests.append(path)
        has_python = any(path.endswith((".py", ".pyi")) for path in files.paths)
        manifests = sorted(set(manifests))
        if not manifests and not has_python:
            return Detection(ecosystem=self.name)

        dependencies: list[str] = []
        for data in pyprojects.values():
            dependencies.extend(_pyproject_dependencies(data))
        dependencies.extend(_read_requirements(files))
        frameworks = sorted({_FRAMEWORKS[name] for name in dependencies if name in _FRAMEWORKS})

        package_managers: list[str] = []
        tool_tables = [_tool_table(data) for data in pyprojects.values()]
        if files.by_name("poetry.lock") or any("poetry" in table for table in tool_tables):
            package_managers.append("poetry")
        elif files.by_name("uv.lock") or any("uv" in table for table in tool_tables):
            package_managers.append("uv")
        elif files.by_name("Pipfile", "Pipfile.lock"):
            package_managers.append("pipenv")
        elif manifests:
            package_managers.append("pip")

        build_commands: list[DetectedCommand] = []
        for path in pyproject_paths:
            if isinstance(pyprojects[path].get("build-system"), dict):
                build_commands.append(
                    DetectedCommand(
                        command="python -m build",
                        source=f"{path} [build-system]",
                        confidence=CommandConfidence.HIGH,
                    )
                )
                break
        else:
            setup_paths = files.by_name("setup.py")
            if setup_paths:
                build_commands.append(
                    DetectedCommand(
                        command="python -m build",
                        source=f"{setup_paths[0]} found",
                        confidence=CommandConfidence.MEDIUM,
                    )
                )

        test_commands: list[DetectedCommand] = []
        pytest_config, pytest_source = _has_pytest_config(files, pyprojects)
        has_tests = bool(
            files.glob("**/tests/*.py")
            or files.glob("**/test/*.py")
            or files.glob("**/test_*.py")
            or files.glob("**/*_test.py")
            or files.by_name("conftest.py")
        )
        if pytest_config:
            test_commands.append(
                DetectedCommand(
                    command="pytest",
                    source=pytest_source,
                    confidence=CommandConfidence.HIGH,
                )
            )
        elif has_tests:
            test_commands.append(
                DetectedCommand(
                    command="pytest",
                    source="tests directory found",
                    confidence=CommandConfidence.MEDIUM,
                )
            )
        elif "pytest" in dependencies:
            test_commands.append(
                DetectedCommand(
                    command="pytest",
                    source="pytest dependency",
                    confidence=CommandConfidence.MEDIUM,
                )
            )

        lint_commands: list[DetectedCommand] = []
        ruff_path = next(
            (path for path, data in pyprojects.items() if "ruff" in _tool_table(data)), ""
        )
        mypy_path = next(
            (path for path, data in pyprojects.items() if "mypy" in _tool_table(data)), ""
        )
        if ruff_path or "ruff" in dependencies or files.by_name("ruff.toml", ".ruff.toml"):
            source = f"{ruff_path} [tool.ruff]" if ruff_path else "ruff config/dependency found"
            lint_commands.append(
                DetectedCommand(
                    command="ruff check .",
                    source=source,
                    confidence=CommandConfidence.HIGH,
                )
            )
        if mypy_path or "mypy" in dependencies or files.by_name("mypy.ini"):
            source = f"{mypy_path} [tool.mypy]" if mypy_path else "mypy config/dependency found"
            lint_commands.append(
                DetectedCommand(
                    command="mypy .",
                    source=source,
                    confidence=CommandConfidence.HIGH,
                )
            )
        if files.by_name(".flake8") or "flake8" in dependencies:
            lint_commands.append(
                DetectedCommand(
                    command="flake8",
                    source=".flake8 config found",
                    confidence=CommandConfidence.MEDIUM,
                )
            )

        test_layout: list[str] = []
        for directory in ("tests", "test"):
            if any(f"/{directory}/" in f"/{path}" for path in files.paths):
                test_layout.append(f"{directory}/")
        if files.by_name("conftest.py"):
            test_layout.append("conftest.py")
        if files.glob("**/test_*.py"):
            test_layout.append("test_*.py")
        if files.glob("**/*_test.py"):
            test_layout.append("*_test.py")

        conventions: list[str] = []
        if ruff_path or files.by_name("ruff.toml", ".ruff.toml"):
            conventions.append("ruff")
        if mypy_path or files.by_name("mypy.ini"):
            conventions.append("mypy")
        if files.by_name(".flake8"):
            conventions.append("flake8")
        if any("black" in table for table in tool_tables) or "black" in dependencies:
            conventions.append("black")

        environment_hints: dict[str, str] = {}
        version = _python_version(files, pyprojects)
        if version:
            environment_hints["python_version"] = version
        if "poetry" in package_managers:
            environment_hints["package_manager"] = "poetry"
        elif package_managers:
            environment_hints["package_manager"] = package_managers[0]

        key_files = [*manifests]
        key_files.extend(files.by_name("pytest.ini", "tox.ini", "conftest.py"))

        return Detection(
            ecosystem=self.name,
            package_managers=sorted(set(package_managers)),
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


__all__ = ["PythonDetector"]
