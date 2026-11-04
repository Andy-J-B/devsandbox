"""Validator protocol and validation report types.

The concrete report/check models live in :mod:`devsandbox.validation.models`
(plan 09); they are re-exported here so existing imports such as
``from devsandbox.validation.interfaces import ValidationReport`` keep working.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.types import CheckSpec, RepoHandle, TaskSpec
from ..gitmgr.interfaces import ChangeSet, Workspace
from .models import (
    BaselineComparison,
    BaselineReport,
    CheckResult,
    Evidence,
    FailureKind,
    TamperingFinding,
    TamperingSignal,
    ValidationFailure,
    ValidationReport,
)

__all__ = [
    "BaselineComparison",
    "BaselineReport",
    "CheckResult",
    "Evidence",
    "FailureKind",
    "TamperingFinding",
    "TamperingSignal",
    "ValidationFailure",
    "ValidationReport",
    "Validator",
]


@runtime_checkable
class Validator(Protocol):
    """Protocol implemented by the validator / integration runner."""

    async def validate_task(
        self, task: TaskSpec, ws: Workspace, cs: ChangeSet
    ) -> ValidationReport: ...

    async def validate_integration(
        self, repo: RepoHandle, staging_commit: str, checks: list[CheckSpec]
    ) -> ValidationReport: ...
