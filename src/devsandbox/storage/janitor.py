"""Data-root garbage collection and disk-usage accounting.

Deletion is deliberately conservative:

* only the ``workspaces``, ``logs``, and ``validation`` categories are
  collectable; canonical mirrors, integration scratch, and secrets are
  report-only;
* a candidate owned by an active attempt (by attempt id or workspace path) is
  never removed;
* age is measured from the newest file modification time in the tree, so an
  in-progress writer keeps its directory alive;
* every path is resolved and checked to remain below the category root before
  removal, and symlinks are unlinked rather than followed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..common.logging import get_logger
from ..common.time import utcnow
from ..config.settings import StorageSettings
from ..db.repositories import AttemptRepo
from .models import (
    CategoryUsage,
    DiskUsageReport,
    GarbageCollectionReport,
    StorageCategory,
)

_log = get_logger("devsandbox.storage.janitor")

_CATEGORY_ROOTS: tuple[StorageCategory, ...] = (
    StorageCategory.REPOS,
    StorageCategory.WORKSPACES,
    StorageCategory.LOGS,
    StorageCategory.VALIDATION,
    StorageCategory.INTEGRATION,
    StorageCategory.SECRETS,
)


class StorageJanitor:
    """Report disk usage and expire stale workspaces, logs, and scratch trees."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        settings: StorageSettings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._data_root = Path(data_root)
        self.settings = settings or StorageSettings()
        self._session_factory = session_factory
        self._clock = clock

    # ------------------------------------------------------------------ public
    async def disk_usage(self) -> DiskUsageReport:
        """Scan the data root and return a per-category usage report."""
        return await asyncio.to_thread(self._disk_usage_sync)

    async def collect(self, *, dry_run: bool = False) -> GarbageCollectionReport:
        """Remove (or list, when ``dry_run``) expired collectable trees."""
        started = self._clock()
        active_ids, active_paths = await self._active_ownership()
        report = await asyncio.to_thread(
            self._collect_sync,
            started,
            active_ids,
            active_paths,
            dry_run,
        )
        _log.info(
            "storage garbage collection finished",
            dry_run=dry_run,
            removed=len(report.removed_paths),
            bytes_reclaimed=report.bytes_reclaimed,
            errors=len(report.errors),
        )
        return report

    # --------------------------------------------------------------- ownership
    async def _active_ownership(self) -> tuple[set[str], set[str]]:
        """Return (active attempt ids, active workspace paths)."""
        if self._session_factory is None:
            return set(), set()
        async with self._session_factory() as session:
            attempts = await AttemptRepo(session).list_active()
        active_ids = {attempt.id for attempt in attempts}
        active_paths = {
            self._normalise(Path(attempt.workspace_path))
            for attempt in attempts
            if attempt.workspace_path
        }
        return active_ids, active_paths

    @staticmethod
    def _normalise(path: Path) -> str:
        try:
            return str(path.resolve(strict=False))
        except OSError:
            return str(path)

    # ------------------------------------------------------------------ report
    def _disk_usage_sync(self) -> DiskUsageReport:
        now = self._clock()
        categories: dict[str, CategoryUsage] = {}
        for category in _CATEGORY_ROOTS:
            categories[category.value] = self._tree_usage(self._data_root / category.value)
        categories[StorageCategory.OTHER.value] = self._other_usage(
            {category.value for category in _CATEGORY_ROOTS}
        )

        filesystem_total = filesystem_free = filesystem_used = 0
        with contextlib.suppress(OSError):
            usage = shutil.disk_usage(self._data_root)
            filesystem_total = int(usage.total)
            filesystem_free = int(usage.free)
            filesystem_used = int(usage.used)

        return DiskUsageReport(
            data_root=str(self._data_root),
            collected_at=_to_utc(now.timestamp()) or now,
            categories=categories,
            total_bytes=sum(usage.bytes for usage in categories.values()),
            filesystem_total_bytes=filesystem_total,
            filesystem_free_bytes=filesystem_free,
            filesystem_used_bytes=filesystem_used,
        )

    def _other_usage(self, known: set[str]) -> CategoryUsage:
        """Account for root files and unknown top-level directories."""
        total_bytes = 0
        files = 0
        directories = 0
        entries = 0
        oldest: datetime | None = None
        newest: datetime | None = None
        if not self._data_root.is_dir():
            return CategoryUsage()
        with contextlib.suppress(OSError), os.scandir(self._data_root) as scan:
            for entry in scan:
                entries += 1
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                oldest = _min_time(oldest, stat.st_mtime)
                newest = _max_time(newest, stat.st_mtime)
                if entry.is_symlink():
                    total_bytes += stat.st_size
                    files += 1
                elif entry.is_dir(follow_symlinks=False) and entry.name not in known:
                    usage = self._tree_usage(Path(entry.path))
                    total_bytes += usage.bytes
                    files += usage.files
                    directories += usage.directories + 1
                    if usage.oldest_mtime is not None:
                        oldest = _min_time(oldest, usage.oldest_mtime.timestamp())
                    if usage.newest_mtime is not None:
                        newest = _max_time(newest, usage.newest_mtime.timestamp())
                elif entry.is_file(follow_symlinks=False):
                    total_bytes += stat.st_size
                    files += 1
        return CategoryUsage(
            bytes=total_bytes,
            files=files,
            directories=directories,
            entries=entries,
            oldest_mtime=oldest,
            newest_mtime=newest,
        )

    def _tree_usage(self, root: Path) -> CategoryUsage:
        if not root.is_dir() or root.is_symlink():
            return CategoryUsage()
        total_bytes = 0
        files = 0
        directories = 0
        entries = 0
        oldest: datetime | None = None
        newest: datetime | None = None
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as scan:
                    children = list(scan)
            except OSError:
                continue
            if current == root:
                entries = len(children)
            for entry in children:
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                oldest = _min_time(oldest, stat.st_mtime)
                newest = _max_time(newest, stat.st_mtime)
                total_bytes += stat.st_size
                if entry.is_symlink():
                    files += 1
                elif entry.is_dir(follow_symlinks=False):
                    directories += 1
                    stack.append(Path(entry.path))
                else:
                    files += 1
        return CategoryUsage(
            bytes=total_bytes,
            files=files,
            directories=directories,
            entries=entries,
            oldest_mtime=oldest,
            newest_mtime=newest,
        )

    # ------------------------------------------------------------------ collect
    def _collect_sync(
        self,
        started: datetime,
        active_ids: set[str],
        active_paths: set[str],
        dry_run: bool,
    ) -> GarbageCollectionReport:
        removed: list[str] = []
        errors: list[str] = []
        reclaimed = 0
        skipped_active = 0

        if not self.settings.enabled:
            return GarbageCollectionReport(
                started_at=started,
                finished_at=self._clock(),
                dry_run=dry_run,
            )

        now = self._clock()
        try:
            _, workspace_bytes, workspace_active = self._collect_workspaces(
                now, active_ids, active_paths, dry_run, removed, errors
            )
            _, log_bytes, log_active = self._collect_logs(now, active_ids, dry_run, removed, errors)
            _, validation_bytes, validation_active = self._collect_validation(
                now, active_ids, dry_run, removed, errors
            )
            reclaimed = workspace_bytes + log_bytes + validation_bytes
            skipped_active = workspace_active + log_active + validation_active
        except Exception as exc:  # pragma: no cover - defensive around filesystem
            errors.append(f"{type(exc).__name__}: {exc}")

        return GarbageCollectionReport(
            started_at=started,
            finished_at=self._clock(),
            dry_run=dry_run,
            removed_paths=removed,
            bytes_reclaimed=reclaimed,
            skipped_active=skipped_active,
            errors=errors,
        )

    def _collect_workspaces(
        self,
        now: datetime,
        active_ids: set[str],
        active_paths: set[str],
        dry_run: bool,
        removed: list[str],
        errors: list[str],
    ) -> tuple[int, int, int]:
        root = self._data_root / StorageCategory.WORKSPACES.value
        retention = self._retention_seconds(self.settings.workspace_retention_hours)
        candidates = 0
        reclaimed = 0
        skipped_active = 0
        for project_dir in self._safe_children(root, errors):
            if project_dir.is_symlink() or not project_dir.is_dir():
                continue
            for attempt_dir in self._safe_children(project_dir, errors):
                if attempt_dir.is_symlink() or not attempt_dir.is_dir():
                    continue
                candidates += 1
                if attempt_dir.name in active_ids or self._normalise(attempt_dir) in active_paths:
                    skipped_active += 1
                    continue
                if not self._expired(attempt_dir, now, retention):
                    continue
                reclaimed += self._remove(attempt_dir, root, dry_run, removed, errors)
            if not dry_run:
                with contextlib.suppress(OSError):
                    if not any(project_dir.iterdir()):
                        project_dir.rmdir()
        return candidates, reclaimed, skipped_active

    def _collect_logs(
        self,
        now: datetime,
        active_ids: set[str],
        dry_run: bool,
        removed: list[str],
        errors: list[str],
    ) -> tuple[int, int, int]:
        root = self._data_root / StorageCategory.LOGS.value
        retention = self._retention_seconds(self.settings.log_retention_hours)
        candidates = 0
        reclaimed = 0
        skipped_active = 0
        directories = [
            path
            for path in self._safe_children(root, errors)
            if not path.is_symlink() and path.is_dir()
        ]
        keep: set[str] = set()
        if self.settings.keep_latest_log_directories > 0:
            ordered = sorted(
                directories,
                key=lambda path: _latest_mtime(path) or 0.0,
                reverse=True,
            )
            keep = {
                self._normalise(path)
                for path in ordered[: self.settings.keep_latest_log_directories]
            }
        for directory in directories:
            candidates += 1
            if directory.name in active_ids:
                skipped_active += 1
                continue
            if self._normalise(directory) in keep:
                continue
            if not self._expired(directory, now, retention):
                continue
            reclaimed += self._remove(directory, root, dry_run, removed, errors)
        return candidates, reclaimed, skipped_active

    def _collect_validation(
        self,
        now: datetime,
        active_ids: set[str],
        dry_run: bool,
        removed: list[str],
        errors: list[str],
    ) -> tuple[int, int, int]:
        root = self._data_root / StorageCategory.VALIDATION.value
        retention = self._retention_seconds(self.settings.validation_retention_hours)
        candidates = 0
        reclaimed = 0
        skipped_active = 0
        for directory in self._safe_children(root, errors):
            if directory.is_symlink() or not directory.is_dir():
                continue
            candidates += 1
            if directory.name in active_ids:
                skipped_active += 1
                continue
            if not self._expired(directory, now, retention):
                continue
            reclaimed += self._remove(directory, root, dry_run, removed, errors)
        return candidates, reclaimed, skipped_active

    # ------------------------------------------------------------------ helpers
    def _safe_children(self, root: Path, errors: list[str]) -> list[Path]:
        if not root.is_dir() or root.is_symlink():
            return []
        try:
            with os.scandir(root) as scan:
                return sorted(Path(entry.path) for entry in scan)
        except OSError as exc:
            errors.append(f"cannot scan {root}: {exc}")
            return []

    def _expired(self, path: Path, now: datetime, retention_seconds: float) -> bool:
        newest = _latest_mtime(path)
        if newest is None:
            return False
        return now.timestamp() - newest >= retention_seconds

    def _remove(
        self,
        path: Path,
        root: Path,
        dry_run: bool,
        removed: list[str],
        errors: list[str],
    ) -> int:
        if not self._within(path, root):
            errors.append(f"refusing to remove path outside {root}: {path}")
            return 0
        size = _tree_bytes(path)
        if dry_run:
            removed.append(str(path))
            return size
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            errors.append(f"cannot remove {path}: {exc}")
            return 0
        removed.append(str(path))
        return size

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _retention_seconds(hours: float) -> float:
        return max(0.0, hours) * 3600.0


def _latest_mtime(path: Path) -> float | None:
    """Return the newest modification time in a tree without following links."""
    newest: float | None = None
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = list(scan)
        except OSError:
            continue
        if current is path:
            with contextlib.suppress(OSError):
                newest = _max_float(newest, current.stat(follow_symlinks=False).st_mtime)
        for entry in children:
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            newest = _max_float(newest, stat.st_mtime)
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                stack.append(Path(entry.path))
    return newest


def _tree_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = list(scan)
        except OSError:
            continue
        for entry in children:
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            total += stat.st_size
            if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                stack.append(Path(entry.path))
    return total


def _to_utc(timestamp: float | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _min_time(current: datetime | None, timestamp: float | None) -> datetime | None:
    candidate = _to_utc(timestamp)
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if candidate < current else current


def _max_time(current: datetime | None, timestamp: float | None) -> datetime | None:
    candidate = _to_utc(timestamp)
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if candidate > current else current


def _max_float(current: float | None, value: float) -> float:
    return value if current is None or value > current else current


__all__ = ["StorageJanitor"]
