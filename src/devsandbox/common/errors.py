"""Typed error hierarchy for DevSandbox."""

from __future__ import annotations


class DevSandboxError(Exception):
    """Base class for all DevSandbox errors."""


class ConfigError(DevSandboxError):
    """Raised when configuration is invalid."""


class PolicyViolation(DevSandboxError):
    """Raised when an operation violates security policy."""


class SandboxError(DevSandboxError):
    """Raised when sandbox container operations fail."""


class GitError(DevSandboxError):
    """Raised when git workspace / integration operations fail."""


class ValidationError(DevSandboxError):
    """Raised when validation checks fail or report an error."""


class ContextError(DevSandboxError):
    """Raised when context analysis or artifact generation fails."""


class SpecError(DevSandboxError):
    """Raised when a project spec cannot be loaded or is invalid."""


class InvalidTransition(DevSandboxError):
    """Raised when a task status transition is illegal or loses a CAS race."""


class TaskNotFoundError(DevSandboxError):
    """Raised when a task record cannot be found."""


class BudgetExceeded(DevSandboxError):
    """Raised when a model inference budget would be exceeded."""


class PlannerError(DevSandboxError):
    """Raised when planning fails or produces an unusable plan."""


class PlanNotFoundError(DevSandboxError):
    """Raised when a plan record cannot be found."""


class ProjectNotFoundError(DevSandboxError):
    """Raised when a project record cannot be found."""


class PlanStateError(DevSandboxError):
    """Raised when a plan lifecycle operation is illegal for the current state."""


class PlanValidationError(PlannerError):
    """Raised when a plan fails graph validation."""


class PlanLimitError(PlannerError):
    """Raised when a plan would exceed a configured planning limit."""


class LLMError(PlannerError):
    """Raised when an LLM call fails or returns unusable output."""
