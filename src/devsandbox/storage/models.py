"""Storage usage and garbage-collection data models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StorageCategory(StrEnum):
    """Known top-level categories under ``data_root``."""

    REPOS = "repos"
    WORKSPACES = "workspaces"
    LOGS = "logs"
    VALIDATION = "validation"
    INTEGRATION = "integration"
    SECRETS = "secrets"
    OTHER = "other"


# Categories whose contents the janitor may delete.  Repositories, integration
# scratch, and secrets are deliberately report-only: the canonical mirror must
# never be garbage-collected, an interrupted integration needs the recovery
# decision table, and secret files are managed by the secret lifecycle.
COLLECTABLE_CATEGORIES: frozenset[StorageCategory] = frozenset(
    {
        StorageCategory.WORKSPACES,
        StorageCategory.LOGS,
        StorageCategory.VALIDATION,
    }
)


class CategoryUsage(BaseModel):
    """Byte/file accounting for one data-root category."""

    model_config = ConfigDict(frozen=True)

    bytes: int = 0
    files: int = 0
    directories: int = 0
    entries: int = 0
    oldest_mtime: datetime | None = None
    newest_mtime: datetime | None = None


class DiskUsageReport(BaseModel):
    """Point-in-time disk usage report for the DevSandbox data root."""

    model_config = ConfigDict(frozen=True)

    data_root: str
    collected_at: datetime
    categories: dict[str, CategoryUsage] = Field(default_factory=dict)
    total_bytes: int = 0
    filesystem_total_bytes: int = 0
    filesystem_free_bytes: int = 0
    filesystem_used_bytes: int = 0


class GarbageCollectionReport(BaseModel):
    """Outcome of one garbage-collection pass."""

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = False
    removed_paths: list[str] = Field(default_factory=list)
    bytes_reclaimed: int = 0
    skipped_active: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def removed_count(self) -> int:
        """Return how many paths were removed (or would be removed)."""
        return len(self.removed_paths)


__all__ = [
    "COLLECTABLE_CATEGORIES",
    "CategoryUsage",
    "DiskUsageReport",
    "GarbageCollectionReport",
    "StorageCategory",
]
