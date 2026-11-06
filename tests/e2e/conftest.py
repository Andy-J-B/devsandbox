"""Fixtures for the real end-to-end acceptance suite.

The suite is opt-in (``DEVSANDBOX_E2E=1``) because it needs a Docker daemon,
the ``devsandbox/validator:latest`` image, and PostgreSQL.  Every other run
collects the tests but skips them without touching Docker or the database.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.e2e.agents import Script, ScriptedAgent
from tests.e2e.harness import (
    DEFAULT_IMAGE,
    E2EContext,
    build_context,
    make_bare_remote,
    make_settings,
    make_spec,
    materialize_fixture_repo,
)

from devsandbox.common.types import ProjectMode, RepoSource
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.manager import DockerSandboxManager
from devsandbox.scheduler.config import SchedulerConfig


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not found"
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        return False, "Docker daemon is not reachable"
    image = subprocess.run(
        ["docker", "image", "inspect", DEFAULT_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if image.returncode != 0:
        return False, f"{DEFAULT_IMAGE} is not built"
    return True, ""


@pytest.fixture
def e2e_enabled() -> None:
    """Skip the test unless the E2E suite was explicitly enabled."""
    if os.environ.get("DEVSANDBOX_E2E") != "1":
        pytest.skip("set DEVSANDBOX_E2E=1 to run the end-to-end acceptance suite")
    ready, reason = _docker_ready()
    if not ready:
        pytest.skip(reason)


@dataclass
class E2EEnvironment:
    """Per-test E2E environment sharing one database and data root."""

    settings: DevSandboxSettings
    data_root: Path
    session_factory: async_sessionmaker[AsyncSession]
    repo: Path
    remote_url: str
    _sandboxes: list[DockerSandboxManager] = field(default_factory=list)

    def agent(
        self,
        *,
        scripts: dict[str, Script | list[Script]] | None = None,
        default: Script | None = None,
        secret_env: dict[str, Any] | None = None,
        faults: Any | None = None,
    ) -> ScriptedAgent:
        """Create a scripted agent backed by a fresh real sandbox manager."""
        sandbox = DockerSandboxManager(self.settings, self.data_root)
        self._sandboxes.append(sandbox)
        return ScriptedAgent(
            sandbox,
            self.settings,
            image=DEFAULT_IMAGE,
            scripts=scripts,
            default=default,
            secret_env=secret_env,
            faults=faults,
        )

    def context(
        self,
        agent: ScriptedAgent,
        *,
        planner: Any | None = None,
        scheduler_config: SchedulerConfig | None = None,
    ) -> E2EContext:
        """Wire the real orchestrator services around ``agent``."""
        return build_context(
            settings=self.settings,
            session_factory=self.session_factory,
            agent=agent,
            planner=planner,
            data_root=self.data_root,
            sandbox=agent.sandbox,  # type: ignore[arg-type]
            scheduler_config=scheduler_config,
        )

    def spec(
        self,
        *,
        name: str = "e2e-project",
        repo_url: bool = False,
        goals: tuple[str, ...] = ("Implement the requested change",),
        mode: ProjectMode = ProjectMode.SUPERVISED,
        restricted_paths: tuple[str, ...] = (),
        max_retries: int = 2,
        task_timeout_seconds: int = 300,
        requirements: tuple[str, ...] = (),
    ) -> Any:
        """Build a spec pointing at the local fixture repo or its bare remote."""
        source = (
            RepoSource(url=self.remote_url, ref="main")
            if repo_url
            else RepoSource(path=str(self.repo), ref="main")
        )
        return make_spec(
            name=name,
            repo=source,
            goals=goals,
            mode=mode,
            restricted_paths=restricted_paths,
            max_retries=max_retries,
            task_timeout_seconds=task_timeout_seconds,
            requirements=requirements,
        )


@pytest.fixture
async def e2e_env(
    e2e_enabled: None,
    db_engine: Any,
    postgres_url: str,
    tmp_path: Path,
) -> Any:
    """Provide an isolated data root, database, fixture repo, and services."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    repo = materialize_fixture_repo(tmp_path / "fixture-repo")
    remote_url = make_bare_remote(repo, tmp_path / "remotes" / "e2e.git")
    settings = make_settings(data_root=data_root, database_url=postgres_url)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    env = E2EEnvironment(
        settings=settings,
        data_root=data_root,
        session_factory=maker,
        repo=repo,
        remote_url=remote_url,
    )
    try:
        yield env
    finally:
        for sandbox in env._sandboxes:
            with contextlib.suppress(Exception):
                for container in await sandbox.list_managed():
                    await sandbox.destroy(container.container_id)
            with contextlib.suppress(Exception):
                sandbox._client.close()  # type: ignore[attr-defined]


@pytest.fixture
def fixture_repo(e2e_env: E2EEnvironment) -> Path:
    """Return the materialized local git fixture repository."""
    return e2e_env.repo
