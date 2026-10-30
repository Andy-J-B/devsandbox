"""Validator protocol and validation report types."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..common.types import CheckSpec, RepoHandle, TaskSpec
from ..gitmgr.interfaces import ChangeSet, Workspace


class CheckResult(BaseModel):
    """Outcome of a single validation check."""

    check: str
    passed: bool
    output: str = ""


class ValidationReport(BaseModel):
    """Result of a validation pass."""

    status: Literal["passed", "failed"]
    checks: list[CheckResult] = []
    summary: str = ""


@runtime_checkable
class Validator(Protocol):
    """Protocol implemented by the validator / integration runner."""

    async def validate_task(
        self, task: TaskSpec, ws: Workspace, cs: ChangeSet
    ) -> ValidationReport: ...

    async def validate_integration(
        self, repo: RepoHandle, staging_commit: str, checks: list[CheckSpec]
    ) -> ValidationReport: ...
