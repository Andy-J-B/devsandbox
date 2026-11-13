"""Fixtures for the opt-in operational drill suite.

Drills are deliberately *not* part of the normal unit/contract/integration
runs.  They simulate or perform disruptive operations (Docker daemon outage,
PostgreSQL restart, disk exhaustion, a long idle observation) and are enabled
with::

    DEVSANDBOX_DRILLS=1 python3 -m pytest -m drill

See ``docs/drills.md`` for the automatic and manual procedures.
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
    build_context,
    make_bare_remote,
    make_settings,
    make_spec,
    materialize_fixture_repo,
)

from devsandbox.common.types import ProjectMode, RepoSource
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.orchestrator.models import OrchestratorConfig
from devsandbox.sandbox.manager import DockerSandboxManager


def docker_ready() -> tuple[bool, str]:
    """Return whether a real Docker daemon and validator image are available."""
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


@pytest.fixture(autouse=True)
def drill_enabled() -> None:
    """Skip every drill unless the operator opted in."""
    if os.environ.get("DEVSANDBOX_DRILLS") != "1":
        pytest.skip("set DEVSANDBOX_DRILLS=1 to run the operational drills")


@pytest.fixture
def require_docker() -> None:
    """Skip a drill when Docker or the validator image is unavailable."""
    ready, reason = docker_ready()
    if not ready:
        pytest.skip(reason)


@dataclass
class DrillEnvironment:
    """Per-drill data root, database, fixture repo, and real services."""

    settings: DevSandboxSettings
    data_root: Path
    session_factory: async_sessionmaker[AsyncSession]
    repo: Path
    remote_url: str
    _sandboxes: list[DockerSandboxManager] = field(default_factory=list)

    def sandbox(self) -> DockerSandboxManager:
        """Create a real sandbox manager owned by this drill."""
        sandbox = DockerSandboxManager(self.settings, self.data_root)
        self._sandboxes.append(sandbox)
        return sandbox

    def agent(
        self,
        sandbox: Any | None = None,
        *,
        scripts: dict[str, Script | list[Script]] | None = None,
        default: Script | None = None,
    ) -> ScriptedAgent:
        """Create the deterministic scripted agent (real container execution)."""
        resolved = sandbox or self.sandbox()
        return ScriptedAgent(
            resolved,
            self.settings,
            image=DEFAULT_IMAGE,
            scripts=scripts,
            default=default,
        )

    def context(
        self,
        agent: ScriptedAgent,
        *,
        sandbox: Any | None = None,
        orchestrator_config: OrchestratorConfig | None = None,
    ) -> Any:
        """Wire the real orchestrator services around ``agent``."""
        return build_context(
            settings=self.settings,
            session_factory=self.session_factory,
            agent=agent,
            data_root=self.data_root,
            sandbox=sandbox or agent.sandbox,  # type: ignore[arg-type]
            orchestrator_config=orchestrator_config,
        )

    def spec(self, *, name: str = "drill-project", max_retries: int = 2) -> Any:
        """Build a spec pointing at the local fixture repository."""
        return make_spec(
            name=name,
            repo=RepoSource(path=str(self.repo), ref="main"),
            mode=ProjectMode.SUPERVISED,
            max_retries=max_retries,
        )


@pytest.fixture
async def drill_env(
    require_docker: None,
    postgres_url: str,
    db_engine: Any,
    tmp_path: Path,
) -> Any:
    """Provide an isolated database, data root, fixture repo, and services."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    repo = materialize_fixture_repo(tmp_path / "fixture-repo")
    remote_url = make_bare_remote(repo, tmp_path / "remotes" / "drill.git")
    settings = make_settings(data_root=data_root, database_url=postgres_url)
    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    env = DrillEnvironment(
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
