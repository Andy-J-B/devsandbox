"""Subprocess entry point for real process-kill crash tests (E8 and soak).

The parent test starts this module in a child Python process.  The child wires
the *real* orchestrator services (PostgreSQL, Docker, Git) and either runs to
completion or kills itself with ``SIGKILL`` at a named crash checkpoint.  A
second invocation recovers and converges on the same database/repository.

This module is deliberately importable without a pytest context so that the
process really is a standalone orchestrator.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.e2e.agents import Script, ScriptedAgent
from tests.e2e.harness import (
    DEFAULT_IMAGE,
    FixedPlanner,
    build_context,
    make_settings,
    make_spec,
    task_draft,
)

from devsandbox.agents.interfaces import AgentRunRequest
from devsandbox.common.types import ProjectMode, RepoSource
from devsandbox.db.models import Base, ProjectModel
from devsandbox.orchestrator.engine import FaultInjector
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.sandbox.manager import DockerSandboxManager

_CRASH_SIGNAL = signal.SIGKILL


class KillingFaultInjector(FaultInjector):
    """Fault injector that dies by signal instead of raising in-process."""

    async def checkpoint(self, name: str) -> None:
        self.checkpoints.append(name)
        if name in self.crash_at:
            # No Python cleanup, no exception handlers: a real process death.
            os.kill(os.getpid(), _CRASH_SIGNAL)


class MarkerAgent(ScriptedAgent):
    """Scripted agent that writes a sentinel file as soon as it starts."""

    def __init__(self, *args: Any, marker: Path | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._marker = marker

    async def run(self, req: AgentRunRequest) -> Any:
        if self._marker is not None:
            self._marker.write_text("agent-started", encoding="utf-8")
        return await super().run(req)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--task", default="work")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--crash-at", default=None)
    parser.add_argument("--marker", type=Path, default=None)
    parser.add_argument("--lease-holder", default="e2e-worker")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    return parser.parse_args(argv)


async def _project_id(session_factory: Any, name: str) -> str | None:
    async with session_factory() as session:
        result = await session.execute(select(ProjectModel).where(ProjectModel.name == name))
        row = result.scalars().first()
        return row.id if row is not None else None


async def _run(args: argparse.Namespace) -> None:
    settings = make_settings(
        data_root=args.data_root,
        database_url=args.database_url,
        max_retries=args.max_retries,
        max_replans=1,
    )
    engine = create_async_engine(args.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    sandbox = DockerSandboxManager(settings, args.data_root)
    agent = MarkerAgent(
        sandbox,
        settings,
        image=DEFAULT_IMAGE,
        default=Script(file="docs/worker-result.md"),
        delay_seconds=args.delay_seconds,
        marker=args.marker,
    )
    planner = FixedPlanner(
        lambda spec: [task_draft(args.task, max_retries=args.max_retries, timeout_seconds=120)]
    )
    faults = FaultInjector()
    if args.crash_at:
        faults = KillingFaultInjector({args.crash_at})
    orchestrator_config = OrchestratorConfig.from_settings(
        settings,
        idle_sleep_seconds=0.01,
        tick_seconds=0.01,
        shutdown_timeout_seconds=15.0,
        workspace_retention_hours=0.0,
    )
    ctx = build_context(
        settings=settings,
        session_factory=session_factory,
        agent=agent,
        planner=planner,
        data_root=args.data_root,
        sandbox=sandbox,
        orchestrator_config=orchestrator_config,
        faults=faults,
        lease_holder=args.lease_holder,
    )

    project_id = await _project_id(session_factory, args.name)
    if project_id is None:
        spec = make_spec(
            name=args.name,
            repo=RepoSource(path=str(args.repo), ref="main"),
            mode=ProjectMode.SUPERVISED,
            max_retries=args.max_retries,
        )
        project_id, _ = await ctx.submit_analyze_plan(spec)
    print(f"PROJECT_ID={project_id}", flush=True)

    try:
        await ctx.run_engine(timeout=args.timeout)
    finally:
        with contextlib.suppress(Exception):
            sandbox._client.close()  # type: ignore[attr-defined]
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv if argv is not None else sys.argv[1:]))
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
