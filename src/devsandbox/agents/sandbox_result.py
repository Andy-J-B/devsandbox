"""Shared helpers for turning sandbox executions into agent results.

These helpers are used by the OpenCode adapter and by the deterministic fake
agent adapter, so both map container outcomes identically.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from ..common.redaction import redact
from ..common.types import AttemptId
from ..sandbox.interfaces import SandboxResult
from .interfaces import AgentExitStatus, AgentRunRequest

_MAX_LOG_READ_BYTES = 4 * 1024 * 1024
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SIGNAL_EXIT_CODES = {129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 143}


def map_exit_status(result: SandboxResult) -> AgentExitStatus:
    """Map a :class:`SandboxResult` to an :class:`AgentExitStatus`.

    Ordering matters: a timeout or OOM kill is reported as such even when
    Docker also reports a signal exit code.
    """
    if result.timed_out:
        return "timeout"
    if result.oom_killed:
        return "oom"
    exit_code = result.exit_code
    if exit_code is None:
        return "crashed"
    if exit_code == 0:
        return "exited_ok"
    if exit_code < 0 or exit_code in _SIGNAL_EXIT_CODES:
        return "crashed"
    return "exited_error"


def read_log_text(path: Path | None, *, max_bytes: int = _MAX_LOG_READ_BYTES) -> str:
    """Read at most ``max_bytes`` of ``path`` (best effort, redacted)."""
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return ""
    return str(redact(raw.decode("utf-8", errors="replace")))


def sanitize_log_file(path: Path | None, *, max_bytes: int = _MAX_LOG_READ_BYTES) -> None:
    """Redact a log file in place (defence in depth over the sandbox streamer)."""
    if path is None:
        return
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except OSError:
        return
    text = raw.decode("utf-8", errors="replace")
    cleaned = str(redact(text))
    if cleaned != text:
        with contextlib.suppress(OSError):
            path.write_text(cleaned, encoding="utf-8")


def clean_summary(text: str | None, *, max_chars: int = 4_000) -> str | None:
    """Strip ANSI escapes, redact, and trim an untrusted summary."""
    if not text:
        return None
    cleaned = _ANSI_RE.sub("", text).strip()
    cleaned = str(redact(cleaned))
    if not cleaned:
        return None
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 20] + "\n[... truncated ...]"
    return cleaned


def build_labels(project_id: str | None, task_id: str, attempt_id: AttemptId) -> dict[str, str]:
    """Build the deterministic container labels required by 00 §8.5."""
    labels = {
        "devsandbox.managed": "true",
        "devsandbox.task": task_id,
        "devsandbox.attempt": str(attempt_id),
    }
    if project_id:
        labels["devsandbox.project"] = project_id
    return labels


def resolve_project_id(req: AgentRunRequest) -> str | None:
    """Return the project id, deriving it from the branch when omitted."""
    if req.project_id:
        return req.project_id
    parts = req.workspace.branch.split("/")
    if len(parts) >= 2 and parts[0] == "devsandbox":
        return parts[1]
    return None


__all__ = [
    "build_labels",
    "clean_summary",
    "map_exit_status",
    "read_log_text",
    "resolve_project_id",
    "sanitize_log_file",
]
