"""Independent validation and integration checks (plan 09).

Public entry points:

- :class:`~devsandbox.validation.validator.ValidatorImpl` — implements the
  plan-01 ``Validator`` protocol.
- :class:`~devsandbox.validation.runner.CheckRunner` — sandboxed check runner.
- :func:`~devsandbox.validation.validator.ValidatorImpl.run_baseline` — records
  pre-existing failures before tasks start.
"""

from __future__ import annotations

from .config import ValidationConfig
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
from .runner import CheckRunner
from .validator import ValidatorImpl

__all__ = [
    "BaselineComparison",
    "BaselineReport",
    "CheckResult",
    "CheckRunner",
    "Evidence",
    "FailureKind",
    "TamperingFinding",
    "TamperingSignal",
    "ValidationConfig",
    "ValidationFailure",
    "ValidationReport",
    "ValidatorImpl",
]
