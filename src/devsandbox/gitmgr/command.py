"""Hardened git CLI wrapper used by the git manager.

Every ``git`` invocation is launched through :func:`run_git`, which applies
security defaults (no interactive prompts, fixed identity, hooks disabled on
the host, dubious-ownership tolerance, timeouts, redacted error output) so no
untrusted repo content can execute hooks or leak secrets through logs.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..common.errors import GitError
from ..common.redaction import redact

# Fixed identity for all commits made by the orchestrator (never the agent's).
_GIT_IDENTITY_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "DevSandbox",
    "GIT_AUTHOR_EMAIL": "devsandbox@localhost",
    "GIT_COMMITTER_NAME": "DevSandbox",
    "GIT_COMMITTER_EMAIL": "devsandbox@localhost",
    # Never prompt for credentials interactively.
    "GIT_TERMINAL_PROMPT": "0",
}


@dataclass(frozen=True)
class GitResult:
    """Outcome of a single :func:`run_git` invocation.

    ``stdout`` is raw (callers parse it programmatically and must not log it
    unredacted). ``stderr`` and ``command`` are redacted and safe to log.
    """

    returncode: int
    stdout: str
    stderr: str
    command: str


class GitCommandError(GitError):
    """Raised when a git command exits non-zero."""


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a sanitized subprocess environment for git invocations."""
    env = dict(os.environ)
    env.update(_GIT_IDENTITY_ENV)
    if extra:
        env.update(extra)
    return env


def _argv(args: list[str], cwd: str | Path | None) -> list[str]:
    """Build the argv for a hardened git invocation.

    Adds ``-c core.hooksPath=/dev/null`` so hooks found inside an untrusted
    workspace are never executed on the host, plus ``safe.directory`` for the
    working directory to tolerate dubious-ownership checks.
    """
    argv = ["git", "-c", "core.hooksPath=/dev/null"]
    if cwd is not None:
        argv.extend(["-c", f"safe.directory={cwd}"])
    argv.extend(args)
    return argv


async def run_git(
    args: list[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
    input_text: str | None = None,
    check: bool = True,
) -> GitResult:
    """Run a git command with hardened defaults.

    Args:
        args: git subcommand and arguments (without the leading ``git``).
        cwd: working directory for the invocation.
        env: extra environment variables (merged over the sanitized base).
        timeout: hard timeout in seconds; exceeded commands are killed.
        input_text: optional stdin fed to the command.
        check: when True (default), raise :class:`GitCommandError` on a
            non-zero exit; when False, return the :class:`GitResult` so the
            caller can inspect the exit code (e.g. to detect merge conflicts).

    Returns:
        A :class:`GitResult`. ``stderr`` and ``command`` are redacted.

    Raises:
        GitCommandError: when ``check`` is True and the command exits
            non-zero or times out.
    """
    argv = _argv(args, cwd)
    cwd_str = str(cwd) if cwd is not None else None

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=cwd_str,
            env=_git_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        cmd = " ".join(shlex.quote(a) for a in argv)
        raise GitCommandError(f"git command timed out after {timeout}s: {cmd}") from exc

    result = GitResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=redact(proc.stderr),
        command=" ".join(shlex.quote(a) for a in argv),
    )

    if check and proc.returncode != 0:
        raise GitCommandError(
            f"git command failed (exit {proc.returncode}): {result.command}\n"
            f"stderr: {result.stderr}"
        )
    return result


def is_git_repo(path: str | Path) -> bool:
    """Return True if ``path`` is the root of a git repository.

    Handles both regular worktrees (``.git/`` subdir) and bare repos (the
    directory itself is the git directory).
    """
    p = Path(path)
    if (p / ".git").exists():
        return True
    return (p / "HEAD").is_file() and (p / "objects").is_dir()
