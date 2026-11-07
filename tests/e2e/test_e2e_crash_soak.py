"""E8: real subprocess SIGKILL crash recovery.

Unlike the in-process recovery tests, these tests start the orchestrator in a
child Python process and kill that process with ``SIGKILL``.  Docker containers,
PostgreSQL connections, and Git worktrees therefore survive exactly as they do
after a real crash.
"""

from __future__ import annotations

import contextlib
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.e2e.harness import materialize_fixture_repo, run_git
from tests.e2e.invariants import check_invariants, format_violations

from devsandbox.db.models import ProjectModel
from devsandbox.sandbox.manager import DockerSandboxManager

REPO_ROOT = Path(__file__).resolve().parents[2]


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    return env


def _worker_args(
    env: Any,
    *,
    name: str,
    crash_at: str | None = None,
    marker: Path | None = None,
    delay_seconds: float = 0.0,
    data_root: Path | None = None,
    repo: Path | None = None,
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "tests.e2e.worker",
        "--database-url",
        env.settings.database_url,
        "--data-root",
        str(data_root or env.data_root),
        "--repo",
        str(repo or env.repo),
        "--name",
        name,
        "--lease-holder",
        "e2e-crash-worker",
        "--timeout",
        "240",
        "--delay-seconds",
        str(delay_seconds),
    ]
    if crash_at:
        args.extend(["--crash-at", crash_at])
    if marker is not None:
        args.extend(["--marker", str(marker)])
    return args


def _start_worker(
    env: Any, args: list[str], tmp_path: Path, label: str
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    stdout_path = tmp_path / f"{label}.out"
    stderr_path = tmp_path / f"{label}.err"
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    proc = subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        env=_worker_env(),
        stdout=stdout,
        stderr=stderr,
    )
    # Attach the files so callers can close them after the process dies.
    proc._e2e_stdout = stdout  # type: ignore[attr-defined]
    proc._e2e_stderr = stderr  # type: ignore[attr-defined]
    proc._e2e_stdout_path = stdout_path  # type: ignore[attr-defined]
    proc._e2e_stderr_path = stderr_path  # type: ignore[attr-defined]
    return proc, stdout_path, stderr_path


def _finish_worker(proc: subprocess.Popen[bytes], timeout: float = 300.0) -> int:
    try:
        return proc.wait(timeout=timeout)
    finally:
        for handle in (proc._e2e_stdout, proc._e2e_stderr):  # type: ignore[attr-defined]
            handle.close()


def _run_worker(env: Any, args: list[str], tmp_path: Path, label: str) -> int:
    proc, _stdout, _stderr = _start_worker(env, args, tmp_path, label)
    return _finish_worker(proc)


def _kill_after_marker(proc: subprocess.Popen[bytes], marker: Path, timeout: float = 60.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            proc.send_signal(signal.SIGKILL)
            return _finish_worker(proc)
        if proc.poll() is not None:
            return _finish_worker(proc)
        time.sleep(0.02)
    proc.kill()
    return _finish_worker(proc)


def _read_output(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


async def _project_id_for(env: Any, name: str) -> str:
    async with env.session_factory() as session:
        row = (
            (await session.execute(select(ProjectModel).where(ProjectModel.name == name)))
            .scalars()
            .first()
        )
        assert row is not None, f"project {name!r} was never created"
        return row.id


async def _assert_recovered(e2e_env: Any, *, name: str, main_before: str) -> None:
    project_id = await _project_id_for(e2e_env, name)
    agent = e2e_env.agent()
    sandbox: DockerSandboxManager = agent.sandbox  # type: ignore[assignment]
    violations = await check_invariants(
        session_factory=e2e_env.session_factory,
        data_root=e2e_env.data_root,
        project_id=project_id,
        main_before=main_before,
        sandbox=sandbox,
        infra_allowance=1,
        expect_all_completed=True,
    )
    if violations:
        pytest.fail("post-crash invariants violated:\n" + format_violations(violations))


@pytest.mark.e2e
async def test_e8_sigkill_mid_run_recovers_without_duplicates(e2e_env, tmp_path: Path) -> None:
    """Kill the orchestrator while its agent container is running."""
    env = e2e_env
    name = "e8-mid-run"
    marker = tmp_path / "agent-started"
    args = _worker_args(env, name=name, marker=marker, delay_seconds=0.75)
    proc, stdout_path, stderr_path = _start_worker(env, args, tmp_path, "mid-run")

    returncode = _kill_after_marker(proc, marker)
    output = _read_output(stdout_path)
    assert returncode == -signal.SIGKILL, (
        f"worker did not die by SIGKILL (rc={returncode})\n"
        f"stdout:\n{output}\nstderr:\n{_read_output(stderr_path)}"
    )
    assert marker.exists(), "agent never started"
    project_id = await _project_id_for(env, name)
    main_before = run_git(
        ["rev-parse", "refs/heads/main"],
        env.data_root / "repos" / project_id / "repo.git",
    )

    # Recovery run must converge the interrupted attempt.
    recovery_rc = _run_worker(env, _worker_args(env, name=name), tmp_path, "mid-run-recovery")
    assert recovery_rc == 0, _read_output(tmp_path / "mid-run-recovery.err")

    await _assert_recovered(e2e_env=env, name=name, main_before=main_before)


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("checkpoint", "label"),
    [
        ("after_validation_report", "mid-validation"),
        ("after_staging_advance_before_db", "mid-integration"),
    ],
)
async def test_e8_sigkill_checkpoint_recovers(
    e2e_env, tmp_path: Path, checkpoint: str, label: str
) -> None:
    """Kill the process at a named validation or integration checkpoint."""
    env = e2e_env
    name = f"e8-{label}"
    args = _worker_args(env, name=name, crash_at=checkpoint)
    returncode = _run_worker(env, args, tmp_path, label)
    assert returncode == -signal.SIGKILL, (
        f"checkpoint {checkpoint} did not kill the worker (rc={returncode})\n"
        f"stdout:\n{_read_output(tmp_path / f'{label}.out')}\n"
        f"stderr:\n{_read_output(tmp_path / f'{label}.err')}"
    )

    project_id = await _project_id_for(env, name)
    main_before = run_git(
        ["rev-parse", "refs/heads/main"],
        env.data_root / "repos" / project_id / "repo.git",
    )
    recovery_rc = _run_worker(env, _worker_args(env, name=name), tmp_path, f"{label}-recovery")
    assert recovery_rc == 0, _read_output(tmp_path / f"{label}-recovery.err")

    await _assert_recovered(e2e_env=env, name=name, main_before=main_before)


CRASH_CHECKPOINTS = [
    "after_attempt_created",
    "after_attempt_row",
    "after_workspace",
    "before_agent",
    "after_agent",
    "after_collect",
    "before_validation",
    "after_validation",
    "after_validation_report",
    "before_integration",
    "after_prepare_integration",
    "after_staging_advance_before_db",
    "after_integration_commit",
]


async def _destroy_managed(sandbox: DockerSandboxManager) -> None:
    for container in await sandbox.list_managed():
        with contextlib.suppress(Exception):
            await sandbox.destroy(container.container_id)


@pytest.mark.e2e
@pytest.mark.slow
async def test_soak_random_crash_points(e2e_env, tmp_path: Path) -> None:
    """Run at least 50 randomized crash/restart cycles and check invariants."""
    env = e2e_env
    runs = int(os.environ.get("DEVSANDBOX_SOAK_RUNS", "50"))
    seed = int(os.environ.get("DEVSANDBOX_SOAK_SEED", "20261106"))
    rng = random.Random(seed)
    sandbox = DockerSandboxManager(env.settings, tmp_path)
    failures: list[str] = []

    try:
        for index in range(runs):
            checkpoint = rng.choice(CRASH_CHECKPOINTS)
            run_root = tmp_path / f"soak-{index:03d}"
            data_root = run_root / "data"
            repo = materialize_fixture_repo(run_root / "repo")
            name = f"soak-{index:03d}-{checkpoint}"

            first_rc = _run_worker(
                env,
                _worker_args(
                    env,
                    name=name,
                    crash_at=checkpoint,
                    data_root=data_root,
                    repo=repo,
                ),
                tmp_path,
                f"soak-{index:03d}-crash",
            )
            if first_rc != -signal.SIGKILL:
                failures.append(f"iteration {index} ({checkpoint}): first worker rc={first_rc}")
                await _destroy_managed(sandbox)
                continue

            project_id = await _project_id_for(env, name)
            main_before = run_git(
                ["rev-parse", "refs/heads/main"],
                data_root / "repos" / project_id / "repo.git",
            )
            recovery_rc = _run_worker(
                env,
                _worker_args(env, name=name, data_root=data_root, repo=repo),
                tmp_path,
                f"soak-{index:03d}-recovery",
            )
            if recovery_rc != 0:
                failures.append(
                    f"iteration {index} ({checkpoint}): recovery rc={recovery_rc}\n"
                    + _read_output(tmp_path / f"soak-{index:03d}-recovery.err")
                )
                await _destroy_managed(sandbox)
                continue

            violations = await check_invariants(
                session_factory=env.session_factory,
                data_root=data_root,
                project_id=project_id,
                main_before=main_before,
                sandbox=sandbox,
                infra_allowance=1,
                expect_all_completed=True,
            )
            if violations:
                failures.append(
                    f"iteration {index} ({checkpoint}):\n" + format_violations(violations)
                )
            await _destroy_managed(sandbox)
    finally:
        await _destroy_managed(sandbox)
        with contextlib.suppress(Exception):
            sandbox._client.close()  # type: ignore[attr-defined]

    assert not failures, (
        f"soak found {len(failures)} violating iteration(s) out of {runs}:\n\n"
        + "\n\n".join(failures)
    )
