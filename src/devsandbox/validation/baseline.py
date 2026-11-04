"""Baseline validation helper (plan 09).

The implementation lives on :class:`~devsandbox.validation.validator.ValidatorImpl`
so it can reuse the same sandbox runner, checkout logic, and policy.  This
module provides the small function-shaped entry point described in the plan.
"""

from __future__ import annotations

from ..common.types import CheckSpec, RepoHandle
from .models import BaselineReport
from .validator import ValidatorImpl


async def run_baseline(
    validator: ValidatorImpl,
    repo: RepoHandle,
    revision: str,
    checks: list[CheckSpec] | None = None,
) -> BaselineReport:
    """Run configured checks on ``revision`` and record pre-existing failures."""
    return await validator.run_baseline(repo, revision, checks)


__all__ = ["run_baseline"]
