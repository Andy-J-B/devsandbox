"""Optional Docker/OpenCode integration tests (skipped by default).

Run with::

    DEVSANDBOX_RUN_OPENCODE_INTEGRATION=1 pytest -m opencode

For the live free-model task, also set ``DEVSANDBOX_OPENCODE_LIVE=1`` plus
``DEVSANDBOX_OPENCODE_PROVIDER``, ``DEVSANDBOX_OPENCODE_MODEL``, and
optionally ``DEVSANDBOX_OPENCODE_API_KEY`` / ``DEVSANDBOX_OPENCODE_BASE_URL``.
The live run defaults to ``network=full`` because a hosted model is
unreachable from the default ``network=none`` sandbox; override with
``DEVSANDBOX_OPENCODE_NETWORK``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.opencode]

_ENABLED = os.environ.get("DEVSANDBOX_RUN_OPENCODE_INTEGRATION") == "1"
_IMAGE = os.environ.get("DEVSANDBOX_AGENT_IMAGE", "devsandbox/agent:test")


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


@pytest.fixture(scope="module")
def agent_image() -> str:
    if not _ENABLED:
        pytest.skip("set DEVSANDBOX_RUN_OPENCODE_INTEGRATION=1 to run Docker agent tests")
    if not _docker_available():
        pytest.skip("Docker daemon is not available")
    result = subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            f"OPENCODE_VERSION={os.environ.get('OPENCODE_VERSION', '1.19.5')}",
            "-f",
            "docker/agent-base/Dockerfile",
            "-t",
            _IMAGE,
            ".",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"agent image build failed:\n{result.stdout}\n{result.stderr}")
    return _IMAGE


def test_opencode_version_in_image(agent_image: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "opencode",
            agent_image,
            "--version",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = os.environ.get("OPENCODE_VERSION", "1.19.5")
    assert result.stdout.strip().lstrip("v") == expected


def test_agent_image_has_expected_tools_and_no_docker(agent_image: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            agent_image,
            "-c",
            "id -u; command -v git; command -v python3; command -v node; command -v docker || true",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0].strip() == "1000"
    assert any("git" in line for line in lines)
    assert any("python3" in line for line in lines)
    assert any("node" in line for line in lines)
    assert not any("docker" in line for line in lines)


@pytest.mark.skipif(
    os.environ.get("DEVSANDBOX_OPENCODE_LIVE") != "1",
    reason="set DEVSANDBOX_OPENCODE_LIVE=1 (and provider/model/key env) for a live LLM run",
)
def test_live_free_model_small_task(agent_image: str, tmp_path) -> None:
    """Run one tiny real OpenCode task through OpenCodeAdapter."""
    from tests.agent_helpers import make_request, make_settings, make_workspace

    from devsandbox.agents.opencode import OpenCodeAdapter
    from devsandbox.common.types import ModelConfig, NetworkPolicy, TaskSpec
    from devsandbox.config.settings import SecretRef
    from devsandbox.sandbox.manager import DockerSandboxManager

    provider = os.environ.get("DEVSANDBOX_OPENCODE_PROVIDER", "opencode")
    model_id = os.environ.get("DEVSANDBOX_OPENCODE_MODEL")
    if not model_id:
        pytest.skip("DEVSANDBOX_OPENCODE_MODEL is required for the live test")
    settings = make_settings()
    if os.environ.get("DEVSANDBOX_OPENCODE_API_KEY"):
        settings.secrets["model"] = SecretRef(env_var="DEVSANDBOX_OPENCODE_API_KEY")
        secret_ref = "model"
    else:
        secret_ref = None
    model = ModelConfig(
        provider=provider,
        model=model_id,
        base_url=os.environ.get("DEVSANDBOX_OPENCODE_BASE_URL") or None,
        secret_ref=secret_ref,
    )
    network = NetworkPolicy(mode=os.environ.get("DEVSANDBOX_OPENCODE_NETWORK", "full"))
    task = TaskSpec(
        id="live-smoke",
        title="Create a greeting file",
        description="Create a file named hello.txt in the repository root.",
        instructions=(
            "Write exactly 'hello from devsandbox' followed by a newline to "
            "hello.txt, then commit the change with git."
        ),
        success_criteria=["hello.txt exists and contains the greeting"],
        expected_files=["hello.txt"],
    )
    workspace, workspace_path = make_workspace(tmp_path / "live", initialise_git=True)
    sandbox = DockerSandboxManager(settings, data_root=tmp_path / "data")
    adapter = OpenCodeAdapter(sandbox, settings, image=agent_image)

    import asyncio

    result = asyncio.run(
        adapter.run(
            make_request(
                workspace,
                task=task,
                model=model,
                network=network,
                timeout_seconds=300,
            )
        )
    )
    usage = result.token_usage
    record = {
        "provider": provider,
        "model": model_id,
        "base_url": model.base_url,
        "network": network.mode,
        "exit_status": result.exit_status,
        "exit_code": result.exit_code,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
        "cost_usd": 0.0,
        "duration_seconds": round(result.duration_seconds or 0.0, 2),
        "log_path": str(result.log_path) if result.log_path else None,
    }
    print("LIVE_OPENCODE_SMOKE=" + json.dumps(record, sort_keys=True))

    assert result.log_path is not None
    assert result.exit_status == "exited_ok", result.summary
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8").strip() == (
        "hello from devsandbox"
    )
    assert usage is not None
    assert usage.total_tokens > 0
