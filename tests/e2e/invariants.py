"""Reusable invariant checks for crash recovery and randomized soak runs.

The seven invariants mirror plan 11 §3.  Checks are intentionally read-only and
collect *all* violations so a soak iteration reports every problem it found
instead of stopping at the first one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devsandbox.common.types import ProjectStatus, TaskStatus
from devsandbox.db.models import Base
from devsandbox.db.repositories import (
    AttemptRepo,
    EventRepo,
    IntegrationRepo,
    ProjectRepo,
    TaskRepo,
)
from devsandbox.orchestrator.state_machine import is_legal, is_legal_project


def _repo_path(data_root: Path, project_id: str) -> Path:
    return data_root / "repos" / project_id / "repo.git"


def _git_evidence(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


async def _project_tasks(session: AsyncSession, project_id: str) -> list[Any]:
    project = await ProjectRepo(session).get(project_id)
    if project is None or project.active_plan_id is None:
        return []
    return await TaskRepo(session).list_for_plan(project.active_plan_id)


async def check_invariants(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    data_root: Path,
    project_id: str,
    main_before: str,
    sandbox: Any | None = None,
    canary: str | None = None,
    infra_allowance: int = 1,
    expect_all_completed: bool = True,
) -> list[str]:
    """Return human-readable invariant violations for one project."""
    violations: list[str] = []
    repo = _repo_path(data_root, project_id)

    async with session_factory() as session:
        tasks = await _project_tasks(session, project_id)
        integrations = await IntegrationRepo(session).list_for_project(project_id)
        events = await EventRepo(session).list_for_project(project_id, limit=10_000)

        by_task: dict[str, list[Any]] = {}
        for integration in integrations:
            by_task.setdefault(integration.task_id, []).append(integration)

        for task in tasks:
            status = TaskStatus(task.status)
            if expect_all_completed and status is not TaskStatus.COMPLETED:
                violations.append(f"task {task.key} ended in {status.value}, not completed")
            if status is not TaskStatus.COMPLETED:
                continue

            # 1. Exactly one integrated commit, reachable from staging.
            integrated = [row for row in by_task.get(task.id, []) if row.status == "integrated"]
            if len(integrated) != 1:
                violations.append(
                    f"task {task.key} has {len(integrated)} integrated rows (expected 1)"
                )
                continue
            integration = integrated[0]
            commit = integration.staging_commit_after
            if not commit:
                violations.append(f"task {task.key} integration has no staging commit")
            else:
                ancestor = _git_evidence(
                    repo,
                    "merge-base",
                    "--is-ancestor",
                    commit,
                    f"refs/heads/devsandbox/{project_id}/staging",
                )
                if ancestor.returncode != 0:
                    violations.append(
                        f"task {task.key} commit {commit[:12]} is not reachable from staging"
                    )

            # 2. A completed task must have a passed validation report.
            async with session_factory() as attempt_session:
                attempt = await AttemptRepo(attempt_session).get(integration.attempt_id)
            report = (attempt.validation_report_json or {}) if attempt else {}
            if report.get("status") != "passed":
                violations.append(f"task {task.key} completed without a passed validation report")

            # 5. Retry budget (including crash/infra allowance).
            max_attempts = int(task.max_retries) + 1 + infra_allowance
            if int(task.attempt_count) > max_attempts:
                violations.append(
                    f"task {task.key} used {task.attempt_count} attempts > {max_attempts}"
                )

        # 3. main is immutable.
        current_main = _git_evidence(repo, "rev-parse", "refs/heads/main")
        if current_main.returncode != 0:
            violations.append("main ref is missing")
        elif current_main.stdout.strip() != main_before:
            violations.append(
                f"main moved from {main_before[:12]} to {current_main.stdout.strip()[:12]}"
            )

        # 4. No managed containers or orphan workspaces for completed work.
        if sandbox is not None:
            containers = await sandbox.list_managed()
            if containers:
                violations.append(
                    "managed containers remain: "
                    + ", ".join(container.container_id for container in containers)
                )
        if expect_all_completed:
            attempts_all = await AttemptRepo(session).list_for_project(project_id)
            owners = {
                str(attempt.workspace_path): attempt
                for attempt in attempts_all
                if attempt.workspace_path
            }
            for workspace in await _list_workspaces(data_root, project_id):
                owner = owners.get(str(workspace))
                if owner is None:
                    violations.append(f"orphan workspace remains: {workspace}")
                elif owner.status not in {"failed", "abandoned"}:
                    violations.append(
                        f"workspace for {owner.status} attempt {owner.id} remains: {workspace}"
                    )

        # 6. State-machine event paths are legal.
        for event in events:
            if event.type == "task.transition" and event.from_state and event.to_state:
                try:
                    legal = is_legal(TaskStatus(event.from_state), TaskStatus(event.to_state))
                except ValueError:
                    legal = False
                if not legal:
                    violations.append(
                        f"illegal task transition {event.from_state} -> {event.to_state}"
                    )
            elif event.type == "project.transition" and event.from_state and event.to_state:
                try:
                    legal = is_legal_project(
                        ProjectStatus(event.from_state), ProjectStatus(event.to_state)
                    )
                except ValueError:
                    legal = False
                if not legal:
                    violations.append(
                        f"illegal project transition {event.from_state} -> {event.to_state}"
                    )

        # 7. Canary secrets never reach persisted state.
        if canary:
            violations.extend(await _canary_in_db(session_factory, canary))
            violations.extend(_canary_in_logs(data_root, canary))
            violations.extend(_canary_in_git(repo, canary))

    return violations


async def _list_workspaces(data_root: Path, project_id: str) -> list[Path]:
    root = data_root / "workspaces" / project_id
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


async def _canary_in_db(
    session_factory: async_sessionmaker[AsyncSession], canary: str
) -> list[str]:
    violations: list[str] = []
    async with session_factory() as session:
        for table in Base.metadata.sorted_tables:
            result = await session.execute(select(table))
            for row in result.mappings():
                if canary in str(dict(row)):
                    violations.append(f"canary found in table {table.name}")
                    break
    return violations


def _canary_in_logs(data_root: Path, canary: str) -> list[str]:
    violations: list[str] = []
    if not data_root.exists():
        return violations
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if canary in path.read_text(encoding="utf-8", errors="ignore"):
                violations.append(f"canary found in {path}")
        except OSError:
            continue
    return violations


def _canary_in_git(repo: Path, canary: str) -> list[str]:
    if not repo.exists():
        return []
    result = _git_evidence(repo, "log", "-p", "--all")
    if canary in result.stdout:
        return ["canary found in git history"]
    return []


def format_violations(violations: list[str]) -> str:
    """Format a violation list for ``AssertionError`` messages."""
    return "\n".join(f"- {violation}" for violation in violations)


__all__ = ["check_invariants", "format_violations"]
