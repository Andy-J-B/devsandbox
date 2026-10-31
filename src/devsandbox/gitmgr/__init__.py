"""Git workspace / branch / staging integration abstractions. Owned implementation: plan 03."""

from .command import GitCommandError, GitResult, is_git_repo, run_git
from .interfaces import (
    ChangeSet,
    CommitInfo,
    GitManager,
    IntegrationResult,
    PushResult,
    Workspace,
)
from .manager import GitManagerImpl, IntegrationCandidate

__all__ = [
    "ChangeSet",
    "CommitInfo",
    "GitCommandError",
    "GitManager",
    "GitManagerImpl",
    "GitResult",
    "IntegrationCandidate",
    "IntegrationResult",
    "PushResult",
    "Workspace",
    "is_git_repo",
    "run_git",
]
