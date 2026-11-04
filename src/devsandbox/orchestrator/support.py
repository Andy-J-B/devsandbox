"""Shared conversion helpers for orchestrator services."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from ..common.errors import ConfigError
from ..common.types import (
    CheckSpec,
    RepoHandle,
    RepoSource,
    ResourceProfile,
    TaskSpec,
)
from ..db.models import ProjectModel, TaskModel
from ..spec.models import ProjectSpec, load_spec


def spec_from_project(project: ProjectModel) -> ProjectSpec:
    """Return the validated spec stored on a project row."""
    try:
        if project.spec_json:
            return ProjectSpec.model_validate_json(project.spec_json)
        if project.spec_yaml:
            return load_spec(project.spec_yaml)
    except (ValidationError, ValueError) as exc:
        raise ConfigError(f"project {project.id} has an invalid spec: {exc}") from exc
    raise ConfigError(f"project {project.id} has no stored spec")


def repo_handle_for_project(project: ProjectModel) -> RepoHandle:
    """Build a :class:`RepoHandle` from a persisted project row."""
    source: RepoSource | None = None
    if project.repo_source_json:
        source = RepoSource.model_validate_json(project.repo_source_json)
    path = project.repo_path or (source.path if source is not None else None)
    url = source.url if source is not None else None
    if path is None and url is None:
        raise ConfigError(f"project {project.id} has no repository source")
    return RepoHandle(
        project_id=project.id,
        path=path or "",
        url=url,
        ref=source.ref if source is not None else project.main_branch,
        main_branch=project.main_branch,
        staging_branch=project.staging_branch,
    )


def task_spec_from_model(task: TaskModel) -> TaskSpec:
    """Convert a persisted task row into the shared :class:`TaskSpec`."""
    resources = ResourceProfile.model_validate(task.resource_profile_json or {})
    return TaskSpec(
        id=task.id,
        title=task.title,
        description=task.description or "",
        instructions=task.instructions or "",
        success_criteria=list(task.success_criteria_json or []),
        expected_files=list(task.expected_files_json or []),
        restricted_files=list(task.restricted_files_json or []),
        checks=checks_from_task(task),
        resources=resources,
        timeout_seconds=task.timeout_seconds,
        max_retries=task.max_retries,
    )


def checks_from_task(task: TaskModel) -> list[CheckSpec]:
    """Return the validation checks persisted on a task."""
    return [CheckSpec.model_validate(item) for item in (task.checks_json or [])]


def project_checks(spec: ProjectSpec) -> list[CheckSpec]:
    """Return the project-level validation checks from a spec."""
    return list(spec.validation.commands)


def usage_window_start(now: datetime, window_seconds: int) -> datetime:
    """Floor ``now`` to a fixed window boundary (UTC epoch based)."""
    epoch = int(now.timestamp())
    floored = epoch - (epoch % max(1, window_seconds))
    return datetime.fromtimestamp(floored, tz=now.tzinfo)


__all__ = [
    "checks_from_task",
    "project_checks",
    "repo_handle_for_project",
    "spec_from_project",
    "task_spec_from_model",
    "usage_window_start",
]
