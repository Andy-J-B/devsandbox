"""Diff inspection for host-side validation pre-checks (plan 09).

The validator must be able to reason about a change set without executing the
agent's code.  This module runs read-only ``git diff`` commands through plan
03's hardened wrapper and returns a small, size-capped summary that pre-checks
and the test-tampering heuristic can inspect.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..common.redaction import redact
from ..gitmgr.command import GitResult, run_git
from ..gitmgr.interfaces import ChangeSet, Workspace


@dataclass(frozen=True)
class DiffFile:
    """Per-file diff statistics."""

    path: str
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    status: str = "M"


@dataclass(frozen=True)
class DiffSummary:
    """A bounded, read-only summary of a change set."""

    available: bool = True
    error: str = ""
    files: list[DiffFile] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    binary_files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    text: str = ""
    truncated: bool = False

    @property
    def changed_lines(self) -> int:
        """Return additions + deletions (the diff-size metric)."""
        return self.additions + self.deletions


@runtime_checkable
class DiffProvider(Protocol):
    """Protocol for obtaining a diff summary from an attempt workspace."""

    async def diff(self, ws: Workspace, cs: ChangeSet) -> DiffSummary: ...


def _parse_numstat(output: str) -> tuple[list[DiffFile], int, int, list[str]]:
    files: list[DiffFile] = []
    additions = deletions = 0
    binary: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw, del_raw, path = parts[0], parts[1], parts[-1]
        if add_raw == "-" or del_raw == "-":
            files.append(DiffFile(path=path, binary=True))
            binary.append(path)
            continue
        try:
            add = int(add_raw)
            delete = int(del_raw)
        except ValueError:
            continue
        additions += add
        deletions += delete
        files.append(DiffFile(path=path, additions=add, deletions=delete))
    return files, additions, deletions, binary


def _parse_name_status(output: str) -> tuple[dict[str, str], list[str]]:
    statuses: dict[str, str] = {}
    deleted: list[str] = []
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1] or "M"
        path = parts[-1]
        statuses[path] = status
        if status == "D":
            deleted.append(path)
    return statuses, deleted


class GitDiffProvider:
    """Default :class:`DiffProvider` backed by read-only git commands."""

    def __init__(self, *, max_diff_bytes: int = 2_000_000, timeout_seconds: float = 60.0) -> None:
        self._max_diff_bytes = max_diff_bytes
        self._timeout = timeout_seconds

    async def diff(self, ws: Workspace, cs: ChangeSet) -> DiffSummary:
        """Return a diff summary for ``cs`` in ``ws.path``."""
        workspace = Path(ws.path)
        if not workspace.is_dir():
            return DiffSummary(available=False, error=f"workspace not found: {workspace}")

        base = cs.base_commit or ""
        head = cs.head_commit or "HEAD"
        if base and base == head:
            return DiffSummary(available=True)

        numstat_args = self._diff_args("--numstat", base, head)
        status_args = self._diff_args("--name-status", base, head)
        text_args = self._diff_args("--unified=0", base, head)
        text_args.insert(1, "--no-color")

        numstat, status_res, text_res = await asyncio.gather(
            run_git(numstat_args, cwd=workspace, check=False, timeout=self._timeout),
            run_git(status_args, cwd=workspace, check=False, timeout=self._timeout),
            run_git(text_args, cwd=workspace, check=False, timeout=self._timeout),
        )
        if numstat.returncode != 0:
            return DiffSummary(
                available=False,
                error=str(redact(f"git diff failed: {numstat.stderr.strip()}")),
            )

        files, additions, deletions, binary = _parse_numstat(numstat.stdout)
        statuses: dict[str, str] = {}
        deleted: list[str] = []
        if status_res.returncode == 0:
            statuses, deleted = _parse_name_status(status_res.stdout)
        for index, item in enumerate(files):
            item_status = statuses.get(item.path, "M")
            if item_status != "M":
                files[index] = DiffFile(
                    path=item.path,
                    additions=item.additions,
                    deletions=item.deletions,
                    binary=item.binary,
                    status=item_status,
                )
        files_changed = [item.path for item in files] or list(statuses)
        text = text_res.stdout if text_res.returncode == 0 else ""
        truncated = len(text.encode("utf-8")) > self._max_diff_bytes
        if truncated:
            text = text.encode("utf-8")[: self._max_diff_bytes].decode("utf-8", errors="ignore")
        return DiffSummary(
            available=True,
            files=files,
            files_changed=files_changed,
            deleted_files=deleted,
            binary_files=binary,
            additions=additions,
            deletions=deletions,
            text=text,
            truncated=truncated,
        )

    @staticmethod
    def _diff_args(option: str, base: str, head: str) -> list[str]:
        if base and head:
            return ["diff", option, base, head]
        if head and head != "HEAD":
            return ["show", option, "--format=", head]
        return ["diff", option, "HEAD"]


def _result_ok(result: GitResult) -> bool:
    """Small helper kept for tests / future diff backends."""
    return result.returncode == 0


__all__ = [
    "DiffFile",
    "DiffProvider",
    "DiffSummary",
    "GitDiffProvider",
]
