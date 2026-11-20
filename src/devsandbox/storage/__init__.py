"""Data-root storage accounting and garbage collection.

The storage package owns the on-disk retention policy for DevSandbox's data
root: workspaces, agent logs, and validation scratch trees.  It never touches
the canonical Git mirrors, integration scratch, or secret files, and it never
deletes anything owned by an active attempt.
"""

from .janitor import StorageJanitor
from .models import (
    CategoryUsage,
    DiskUsageReport,
    GarbageCollectionReport,
    StorageCategory,
)

__all__ = [
    "CategoryUsage",
    "DiskUsageReport",
    "GarbageCollectionReport",
    "StorageCategory",
    "StorageJanitor",
]
