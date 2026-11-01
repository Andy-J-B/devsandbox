"""Unit tests for the OpenCode adapter using a fake sandbox manager."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.agent_helpers import (
    git_status,
    make_request,
    make_settings,
    make_workspace,
    sandbox_result,
    write_logs,
)
from tests.fakes.sandbox import FakeSandboxManager

from devsandbox.agents.models import UsageLimiter
from devsandbox.agents.opencode import OpenCodeAdapter, parse_token_usage, render_opencode_config
from devsandbox.agents.secrets import SecretProvider
from devsandbox.common.errors import BudgetExceeded, ConfigError, SandboxError
from devsandbox.common.redaction import register_secret, reset_redactor
from devsandbox.common.types import ModelConfig, NetworkPolicy, ResourceProfile
from devsandbox.config.settings import SecretRef


@pytest.fixture(autouse=True)
def _clean_redactor():
    reset_redactor()
    yield
    reset_redactor()


def _adapter(
    tmp_path: Path,
    *,
    settings=None,
    image: str = "agent-test:latest",
    prompt_dir: Path | None = None,
) -> tuple[OpenCodeAdapter, FakeSandboxManager]:
    sandbox = FakeSandboxManager()
    settings = settings or make_settings()
    adapter = OpenCodeAdapter(
        sandbox,
        settings,
        image=image,
        secret_provider=SecretProvider(settings),
        usage_limiter=UsageLimiter(settings),
        prompt_dir=prompt_dir,
    )
    return adapter, sandbox


async def test_run_success_persists_audit_files_outside_repo_and_builds_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_KEY", "canary-model-secret-123456")
    workspace, workspace_path = make_workspace(tmp_path, initialise_git=True)
    settings = make_settings(secrets={"model": SecretRef(env_var="MODEL_KEY")})
    adapter, sandbox = _adapter(tmp_path, settings=settings)
    model = ModelConfig(provider="openrouter", model="free/model", secret_ref="model")
    req = make_request(workspace, model=model)
    stdout, stderr = write_logs(tmp_path, stdout='{"type":"text","text":"done"}\n')
    sandbox.queue_result(sandbox_result(stdout, stderr))

    result = await adapter.run(req)

    assert result.exit_status == "exited_ok"
    call = sandbox.calls[0]
    assert call.attempt_id == "attempt-1"
    assert call.image == "agent-test:latest"
    assert call.workspace_path == workspace_path.resolve()
    assert call.command[:4] == [
        "opencode",
        "run",
        "--model",
        "openrouter/free/model",
    ]
    assert call.command[-1].startswith("# DevSandbox coding task")
    assert "OPENCODE_CONFIG_CONTENT" in call.env
    assert call.env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert call.secret_env["OPENROUTER_API_KEY"].get_secret_value() == (
        "canary-model-secret-123456"
    )
    assert call.labels["devsandbox.project"] == "proj-1"
    assert call.labels["devsandbox.task"] == "task-1"

    prompt_path = workspace_path.parent / "prompts" / "attempt-1.md"
    config_path = workspace_path.parent / "prompts" / "attempt-1.opencode.json"
    assert prompt_path.exists()
    assert config_path.exists()
    assert prompt_path.read_text().startswith("# DevSandbox coding task")
    with pytest.raises(ValueError):
        prompt_path.relative_to(workspace_path)
    with pytest.raises(ValueError):
        config_path.relative_to(workspace_path)
    # Nothing the adapter writes shows up in the repo.
    assert git_status(workspace_path) == ""
    # Secrets never appear in the prompt, config, env, or command.
    assert "canary-model-secret-123456" not in prompt_path.read_text()
    assert "canary-model-secret-123456" not in config_path.read_text()
    assert "canary-model-secret-123456" not in json.dumps(call.env)
    assert "canary-model-secret-123456" not in " ".join(call.command)


async def test_config_is_inline_safe_and_references_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_KEY", "custom-canary-secret")
    workspace, _ = make_workspace(tmp_path)
    settings = make_settings(secrets={"model": SecretRef(env_var="CUSTOM_KEY")})
    adapter, sandbox = _adapter(tmp_path, settings=settings)
    model = ModelConfig(
        provider="my-endpoint",
        model="cheap",
        base_url="http://127.0.0.1:11434/v1",
        secret_ref="model",
    )
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path)))

    await adapter.run(make_request(workspace, model=model))

    config = json.loads(sandbox.calls[0].env["OPENCODE_CONFIG_CONTENT"])
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"
    assert config["snapshot"] is False
    assert config["model"] == "my-endpoint/cheap"
    assert config["permission"]["webfetch"] == "deny"
    assert config["permission"]["edit"] == "allow"
    provider = config["provider"]["my-endpoint"]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    assert provider["options"]["apiKey"] == "{env:DEVSANDBOX_MODEL_API_KEY}"
    assert "custom-canary-secret" not in json.dumps(config)


@pytest.mark.parametrize(
    ("result_kwargs", "expected"),
    [
        ({"exit_code": 0}, "exited_ok"),
        ({"exit_code": 2}, "exited_error"),
        ({"exit_code": None}, "crashed"),
        ({"exit_code": 137}, "crashed"),
        ({"exit_code": 0, "timed_out": True}, "timeout"),
        ({"exit_code": 137, "oom_killed": True}, "oom"),
    ],
)
async def test_exit_status_mapping(
    tmp_path: Path, result_kwargs: dict[str, object], expected: str
) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path)
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path), **result_kwargs))  # type: ignore[arg-type]

    result = await adapter.run(make_request(workspace))

    assert result.exit_status == expected


async def test_sandbox_error_returns_result_instead_of_raising(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path)
    sandbox.fail(SandboxError("docker daemon unavailable"))

    result = await adapter.run(make_request(workspace))

    assert result.exit_status == "sandbox_error"
    assert result.summary is not None
    assert "docker daemon unavailable" in result.summary


async def test_sandbox_error_message_is_redacted(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path)
    canary = "canary-in-sandbox-exception-abc123"
    register_secret(canary)
    sandbox.fail(SandboxError(f"docker failed with {canary}"))

    result = await adapter.run(make_request(workspace))

    assert result.exit_status == "sandbox_error"
    assert result.summary is not None
    assert canary not in result.summary
    assert "[REDACTED]" in result.summary


async def test_cancel_kills_deterministic_container(tmp_path: Path) -> None:
    adapter, sandbox = _adapter(tmp_path)
    await adapter.cancel("attempt-42")
    assert sandbox.killed == ["ds-attempt-42"]


async def test_token_usage_and_summary_are_parsed(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path)
    events = [
        {
            "type": "message.updated",
            "properties": {"info": {"id": "m1", "tokens": {"input": 10, "output": 5}}},
        },
        {
            "type": "message.updated",
            "properties": {"info": {"id": "m1", "tokens": {"input": 10, "output": 6}}},
        },
        {
            "type": "message.part.updated",
            "properties": {"part": {"type": "text", "text": "Implemented and committed."}},
        },
    ]
    stdout_text = "\n".join(json.dumps(event) for event in events) + "\n"
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path, stdout=stdout_text)))

    result = await adapter.run(make_request(workspace))

    assert result.summary == "Implemented and committed."
    assert result.token_usage is not None
    assert result.token_usage.input_tokens == 10
    assert result.token_usage.output_tokens == 6
    assert result.token_usage.total_tokens == 16
    assert result.token_usage.model == "openrouter/free/model"
    assert result.log_path is not None


async def test_logs_and_summary_are_redacted(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path)
    canary = "canary-printed-by-agent-abc123"
    register_secret(canary)
    stdout, stderr = write_logs(
        tmp_path,
        stdout=f"agent printed {canary}\n",
        stderr=f"error with {canary}\n",
    )
    sandbox.queue_result(sandbox_result(stdout, stderr))

    result = await adapter.run(make_request(workspace))

    assert result.summary is not None
    assert canary not in result.summary
    assert canary not in stdout.read_text()
    assert canary not in stderr.read_text()


async def test_budget_blocks_before_side_effects(tmp_path: Path) -> None:
    workspace, workspace_path = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path, settings=make_settings(max_requests=0))

    with pytest.raises(BudgetExceeded):
        await adapter.run(make_request(workspace))

    assert sandbox.calls == []
    assert not (workspace_path.parent / "prompts").exists()


async def test_usage_is_recorded_after_run(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    settings = make_settings(max_requests=5)
    sandbox = FakeSandboxManager()
    limiter = UsageLimiter(settings)
    adapter = OpenCodeAdapter(sandbox, settings, usage_limiter=limiter)
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path)))

    await adapter.run(make_request(workspace))

    assert limiter.snapshot("proj-1", make_request(workspace).model).requests == 1


async def test_policy_clamps_resources_and_network(tmp_path: Path) -> None:
    workspace, _ = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path, settings=make_settings(allow_network_default=False))
    req = make_request(
        workspace,
        resources=ResourceProfile(cpu_cores=99.0, memory_mb=999_999, pids_limit=99_999),
        network=NetworkPolicy(mode="full"),
    )
    sandbox.queue_result(sandbox_result(*write_logs(tmp_path)))

    await adapter.run(req)

    call = sandbox.calls[0]
    assert call.resources.cpu_cores <= 4.0
    assert call.resources.memory_mb <= 8192
    assert call.resources.pids_limit <= 1024
    assert call.network.mode == "none"


async def test_prompt_dir_inside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace, workspace_path = make_workspace(tmp_path)
    adapter, sandbox = _adapter(tmp_path, prompt_dir=workspace_path / "audit")

    with pytest.raises(ConfigError):
        await adapter.run(make_request(workspace))
    assert sandbox.calls == []


def test_render_config_denies_web_tools() -> None:
    config = render_opencode_config(
        ModelConfig(provider="openrouter", model="free/model"),
        NetworkPolicy(mode="none"),
    )
    assert config["permission"]["websearch"] == "deny"
    assert config["permission"]["webfetch"] == "deny"
    assert config["tools"]["webfetch"] is False


def test_parse_token_usage_json_events() -> None:
    text = json.dumps(
        {
            "type": "message.updated",
            "properties": {"info": {"id": "m", "tokens": {"input": 3, "output": 4}}},
        }
    )
    usage = parse_token_usage(text, model_reference="p/m")
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (3, 4, 7)


def test_parse_token_usage_matches_real_opencode_step_finish_event() -> None:
    # Captured from `opencode run --format json` against a local fake
    # OpenAI-compatible endpoint (OpenCode 1.19.5).
    events = [
        {
            "type": "text",
            "part": {"id": "prt_1", "type": "text", "text": "Hello from fake."},
        },
        {
            "type": "step_finish",
            "part": {
                "id": "prt_2",
                "type": "step-finish",
                "tokens": {
                    "total": 15,
                    "input": 10,
                    "output": 5,
                    "reasoning": 0,
                    "cache": {"write": 0, "read": 0},
                },
                "cost": 0,
            },
        },
    ]
    usage = parse_token_usage(
        "\n".join(json.dumps(event) for event in events), model_reference="p/m"
    )
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (10, 5, 15)


def test_parse_token_usage_text_fallback() -> None:
    usage = parse_token_usage("input tokens: 12\noutput tokens: 8\n")
    assert usage is not None
    assert usage.total_tokens == 20


def test_parse_token_usage_returns_none_without_usage() -> None:
    assert parse_token_usage("no numbers here") is None
