"""Unit tests for the data-root janitor and retention policy."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from devsandbox.config.settings import DevSandboxSettings, StorageSettings
from devsandbox.db.repositories import AttemptRepo
from devsandbox.storage import StorageJanitor

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _age(path: Path, hours: float) -> None:
    stamp = (NOW - timedelta(hours=hours)).timestamp()
    for root, directories, files in os.walk(path):
        for name in files:
            os.utime(Path(root) / name, (stamp, stamp))
        for name in directories:
            os.utime(Path(root) / name, (stamp, stamp))
    os.utime(path, (stamp, stamp))


def _write(path: Path, content: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _janitor(
    root: Path,
    *,
    settings: StorageSettings | None = None,
    session_factory: object | None = None,
) -> StorageJanitor:
    return StorageJanitor(
        root,
        settings=settings
        or StorageSettings(
            workspace_retention_hours=1,
            log_retention_hours=1,
            validation_retention_hours=1,
            keep_latest_log_directories=0,
        ),
        session_factory=session_factory,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


async def test_disk_usage_reports_categories_and_filesystem(tmp_path: Path) -> None:
    _write(tmp_path / "repos" / "p1" / "repo.git" / "HEAD", "ref: refs/heads/main")
    _write(tmp_path / "workspaces" / "p1" / "a1" / "file.txt", "abc")
    _write(tmp_path / "logs" / "a1" / "stdout.log", "hello")
    _write(tmp_path / "other.txt", "root")

    report = await _janitor(tmp_path).disk_usage()

    assert Path(report.data_root) == tmp_path
    assert report.categories["repos"].files == 1
    assert report.categories["workspaces"].files == 1
    assert report.categories["workspaces"].bytes >= 3
    assert report.categories["logs"].files == 1
    assert report.categories["other"].files == 1
    assert report.total_bytes == sum(item.bytes for item in report.categories.values())
    assert report.filesystem_total_bytes >= report.filesystem_free_bytes


async def test_collect_removes_only_expired_collectable_trees(tmp_path: Path) -> None:
    old_workspace = _write(tmp_path / "workspaces" / "p1" / "old" / "file.txt")
    new_workspace = _write(tmp_path / "workspaces" / "p1" / "new" / "file.txt")
    old_log = _write(tmp_path / "logs" / "old" / "stdout.log")
    new_log = _write(tmp_path / "logs" / "new" / "stdout.log")
    old_validation = _write(tmp_path / "validation" / "old" / "workspace" / "file.txt")
    new_validation = _write(tmp_path / "validation" / "new" / "workspace" / "file.txt")
    _age(old_workspace.parent, 5)
    _age(new_workspace.parent, 0)
    _age(old_log.parent, 5)
    _age(new_log.parent, 0)
    _age(old_validation.parent.parent, 5)
    _age(new_validation.parent.parent, 0)

    report = await _janitor(tmp_path).collect()

    assert not old_workspace.parent.exists()
    assert new_workspace.parent.exists()
    assert not old_log.parent.exists()
    assert new_log.parent.exists()
    assert not old_validation.parent.parent.exists()
    assert new_validation.parent.parent.exists()
    assert report.removed_count == 3
    assert report.bytes_reclaimed > 0
    assert report.errors == []


async def test_collect_never_touches_repos_integration_or_secrets(tmp_path: Path) -> None:
    repo = _write(tmp_path / "repos" / "p1" / "repo.git" / "HEAD")
    integration = _write(tmp_path / "integration" / "p1" / "worktree" / "file")
    secret = _write(tmp_path / "secrets" / "attempt" / "key")
    _age(repo.parent.parent.parent, 100)
    _age(integration.parent.parent, 100)
    _age(secret.parent, 100)

    report = await _janitor(tmp_path).collect()

    assert repo.exists()
    assert integration.exists()
    assert secret.exists()
    assert report.removed_paths == []


async def test_collect_previews_without_deleting_in_dry_run(tmp_path: Path) -> None:
    old = _write(tmp_path / "workspaces" / "p1" / "old" / "file.txt")
    _age(old.parent.parent, 5)

    report = await _janitor(tmp_path).collect(dry_run=True)

    assert old.exists()
    assert report.dry_run is True
    assert len(report.removed_paths) == 1
    assert report.bytes_reclaimed > 0


async def test_collect_keeps_active_attempt_workspace_logs_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _write(tmp_path / "workspaces" / "p1" / "attempt-1" / "file.txt")
    stale_workspace = _write(tmp_path / "workspaces" / "p1" / "attempt-2" / "file.txt")
    log = _write(tmp_path / "logs" / "attempt-1" / "stdout.log")
    stale_log = _write(tmp_path / "logs" / "attempt-2" / "stdout.log")
    validation = _write(tmp_path / "validation" / "attempt-1" / "workspace" / "file.txt")
    for directory in (
        workspace.parent,
        stale_workspace.parent,
        log.parent,
        stale_log.parent,
        validation.parent.parent,
    ):
        _age(directory, 5)

    async def _active(_self: AttemptRepo) -> list[object]:
        return [
            SimpleNamespace(
                id="attempt-1",
                workspace_path=str(workspace.parent),
            )
        ]

    monkeypatch.setattr(AttemptRepo, "list_active", _active)

    class _Factory:
        def __call__(self) -> object:
            class _Session:
                async def __aenter__(self) -> object:
                    return object()

                async def __aexit__(self, *exc_info: object) -> None:
                    return None

            return _Session()

    report = await _janitor(tmp_path, session_factory=_Factory()).collect()

    assert workspace.exists()
    assert log.parent.exists()
    assert validation.exists()
    assert not stale_workspace.parent.exists()
    assert not stale_log.parent.exists()
    assert report.skipped_active >= 2


async def test_collect_does_not_follow_workspace_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "keep.txt")
    project = tmp_path / "workspaces" / "p1"
    project.mkdir(parents=True)
    (project / "attempt-1").symlink_to(outside, target_is_directory=True)
    _age(project, 5)

    report = await _janitor(tmp_path).collect()

    assert (outside / "keep.txt").exists()
    assert report.removed_paths == []


def test_storage_settings_defaults_and_validation() -> None:
    settings = DevSandboxSettings(_env_file=None)

    assert settings.storage.enabled is True
    assert settings.storage.workspace_retention_hours == 72.0
    assert settings.storage.log_retention_hours == 168.0
    assert settings.storage.validation_retention_hours == 24.0
    with pytest.raises(ValueError, match="retention hours"):
        StorageSettings(workspace_retention_hours=-1)
    with pytest.raises(ValueError, match="storage limits"):
        StorageSettings(gc_interval_seconds=-1)


def test_storage_settings_come_from_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVSANDBOX_STORAGE__WORKSPACE_RETENTION_HOURS", "12")
    monkeypatch.setenv("DEVSANDBOX_STORAGE__LOG_RETENTION_HOURS", "24")
    monkeypatch.setenv("DEVSANDBOX_STORAGE__ENABLED", "false")

    settings = DevSandboxSettings(_env_file=None)

    assert settings.storage.workspace_retention_hours == 12.0
    assert settings.storage.log_retention_hours == 24.0
    assert settings.storage.enabled is False
