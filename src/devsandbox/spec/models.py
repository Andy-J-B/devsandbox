"""YAML project spec models and loader."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..common.errors import SpecError
from ..common.types import CheckSpec, ProjectMode, RepoSource


class ConstraintsSpec(BaseModel):
    """Language / path constraints from the spec."""

    model_config = ConfigDict(extra="forbid")

    languages: list[str] = Field(default_factory=list)
    restricted_paths: list[str] = Field(default_factory=list)


class ValidationSpec(BaseModel):
    """Validation commands from the spec."""

    model_config = ConfigDict(extra="forbid")

    commands: list[CheckSpec] = Field(default_factory=list)


class LimitsSpec(BaseModel):
    """Per-project limits (may only narrow global policy)."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = 2
    task_timeout_seconds: int = 1800
    max_concurrency: int = 1

    @field_validator("max_retries")
    @classmethod
    def _max_retries_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_retries must be >= 0")
        return value

    @field_validator("task_timeout_seconds")
    @classmethod
    def _task_timeout_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("task_timeout_seconds must be > 0")
        return value

    @field_validator("max_concurrency")
    @classmethod
    def _max_concurrency_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_concurrency must be >= 1")
        return value


class AgentSpec(BaseModel):
    """Agent runtime / model selection from the spec."""

    model_config = ConfigDict(extra="forbid")

    runtime: str = "opencode"
    model: str | None = None


_SECRET_FIELD_RE = re.compile(
    r"^(api[_\-]?key|apikey|token|tokens|password|pass|passwd|pwd|secret|client[_\-]?secret|auth)$",
    re.IGNORECASE,
)


def _check_no_secret_literals(value: Any, path: str) -> None:
    """Recursively reject fields that look like inline secret values."""
    if isinstance(value, BaseModel):
        for field_name, field_value in value:
            _check_no_secret_literals(field_value, f"{path}{field_name}.")
    elif isinstance(value, dict):
        for key, val in value.items():
            if (
                _SECRET_FIELD_RE.fullmatch(str(key))
                and not str(key).endswith("_ref")
                and val is not None
            ):
                raise ValueError(
                    f"field {path}{key} looks like a secret value; use a secret_ref instead"
                )
            _check_no_secret_literals(val, f"{path}{key}.")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_no_secret_literals(item, path)


class ProjectSpec(BaseModel):
    """A validated YAML DevSandbox project specification."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str
    description: str = ""
    mode: ProjectMode = ProjectMode.SUPERVISED
    repo: RepoSource
    goals: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    constraints: ConstraintsSpec = Field(default_factory=ConstraintsSpec)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    agent: AgentSpec = Field(default_factory=AgentSpec)

    @property
    def canonical_json(self) -> str:
        """Canonical JSON used for the stable spec hash."""
        dumped = self.model_dump(mode="json")
        return json.dumps(dumped, sort_keys=True, separators=(",", ":"), default=str)

    @property
    def spec_hash(self) -> str:
        """Stable sha256 hex digest of the canonical spec JSON."""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def _check_no_secret_literals(self) -> ProjectSpec:
        _check_no_secret_literals(self, "spec.")
        return self


def load_spec(path_or_text: str | Path) -> ProjectSpec:
    """Load and validate a project spec from a file path or raw YAML text."""
    if isinstance(path_or_text, Path):
        text = path_or_text.read_text(encoding="utf-8")
    elif isinstance(path_or_text, str):
        candidate = Path(path_or_text)
        text = candidate.read_text(encoding="utf-8") if candidate.exists() else path_or_text
    else:
        raise SpecError("path_or_text must be a path or YAML text")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"failed to parse project spec YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise SpecError("project spec must be a YAML mapping")

    try:
        return ProjectSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(f"invalid project spec: {exc}") from exc
