"""OpenCode adapter (plan 04).

Runs OpenCode non-interactively inside a sandbox container.  The adapter
renders a per-attempt OpenCode config, persists a redacted prompt outside the
repository working tree, injects only the provider secret named by the model
config, and maps the container result to an :class:`AgentRunResult`.

Verified invocation (see ``docs/agents-opencode.md``)::

    opencode run --model provider/model --format json --auto "<prompt>"

Configuration is supplied inline via ``OPENCODE_CONFIG_CONTENT`` so no config
file needs to be mounted into the container and no secret is ever written to
disk in the config.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..common.errors import ConfigError, SandboxError
from ..common.time import utcnow
from ..common.types import AttemptId, ModelConfig, NetworkPolicy, TokenUsage
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings
from ..sandbox.interfaces import SandboxManager, SandboxRequest
from .interfaces import AgentRunRequest, AgentRunResult
from .models import UsageLimiter
from .prompt import build_prompt
from .sandbox_result import (
    build_labels,
    clean_summary,
    map_exit_status,
    read_log_text,
    resolve_project_id,
    sanitize_log_file,
)
from .secrets import SecretProvider, provider_secret_env

DEFAULT_AGENT_IMAGE = "devsandbox/agent:latest"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TOKEN_INPUT_RE = re.compile(
    r"(?i)(?:(?:input|prompt)[ \t]*tokens?[ \t]*[:=][ \t]*(\d+)"
    r"|(\d+)[ \t]*(?:input|prompt)[ \t]*tokens?)"
)
_TOKEN_OUTPUT_RE = re.compile(
    r"(?i)(?:(?:output|completion)[ \t]*tokens?[ \t]*[:=][ \t]*(\d+)"
    r"|(\d+)[ \t]*(?:output|completion)[ \t]*tokens?)"
)


def render_opencode_config(model: ModelConfig, network: NetworkPolicy) -> dict[str, Any]:
    """Render the per-attempt OpenCode config.

    The config never contains secret values: the API key is referenced as
    ``{env:NAME}`` and injected through ``SandboxRequest.secret_env``.
    """
    provider = model.provider_id
    model_id = model.model_id
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "model": model.reference,
        "small_model": model.reference,
        "permission": {
            "edit": "allow",
            "bash": "allow",
            "glob": "allow",
            "grep": "allow",
            "read": "allow",
            "list": "allow",
            "task": "allow",
            "webfetch": "deny",
            "websearch": "deny",
            "external_directory": "deny",
        },
        "tools": {
            "webfetch": False,
            "websearch": False,
        },
    }
    if model.base_url:
        provider_config: dict[str, Any] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": provider,
            "options": {"baseURL": model.base_url},
            "models": {model_id: {"name": model_id}},
        }
        if model.secret_ref:
            provider_config["options"]["apiKey"] = "{env:" + provider_secret_env(provider) + "}"
        config["provider"] = {provider: provider_config}
    if network.mode == "none":
        # Defence in depth: even if a tool slips through permissions, there is
        # no container network for it to reach.
        config["permission"]["websearch"] = "deny"
    return config


class OpenCodeAdapter:
    """Agent adapter that runs OpenCode in a disposable sandbox."""

    name = "opencode"

    def __init__(
        self,
        sandbox: SandboxManager,
        settings: DevSandboxSettings,
        *,
        image: str = DEFAULT_AGENT_IMAGE,
        secret_provider: SecretProvider | None = None,
        usage_limiter: UsageLimiter | None = None,
        prompt_dir: str | Path | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._settings = settings
        self._policy = SecurityPolicy(settings)
        self._image = image
        self._secrets = secret_provider or SecretProvider(settings)
        self._usage = usage_limiter or UsageLimiter(settings)
        self._prompt_dir = Path(prompt_dir) if prompt_dir is not None else None

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        """Run OpenCode for ``req`` and return an untrusted result."""
        project_id = resolve_project_id(req)
        budget_key = project_id or req.task.id
        # Budget is checked before any side effect (including writing files).
        self._usage.check_budget(budget_key, req.model)

        workspace_path = Path(req.workspace.path).resolve()
        prompt = build_prompt(req)
        self._write_prompt(req, workspace_path, prompt)
        config = render_opencode_config(req.model, req.network)
        self._write_config(req, workspace_path, config)
        secret_env = self._secrets.secret_env_for(req.model)

        env = {
            "OPENCODE_CONFIG_CONTENT": json.dumps(config, sort_keys=True, separators=(",", ":")),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_PRUNE": "1",
            "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "CI": "1",
        }
        command = [
            "opencode",
            "run",
            "--model",
            req.model.reference,
            "--format",
            "json",
            "--auto",
            "--title",
            f"devsandbox {req.task.id} attempt {req.attempt_number}",
            prompt,
        ]
        sandbox_request = SandboxRequest(
            attempt_id=req.workspace.attempt_id,
            image=self._image,
            workspace_path=workspace_path,
            command=command,
            env=env,
            secret_env=secret_env,
            resources=self._policy.effective_resources(req.resources),
            network=self._policy.effective_network(req.network),
            timeout_seconds=req.timeout_seconds,
            labels=build_labels(project_id, req.task.id, req.workspace.attempt_id),
        )

        started_at = utcnow()
        try:
            result = await self._sandbox.run(sandbox_request)
        except SandboxError as exc:
            duration = (utcnow() - started_at).total_seconds()
            return AgentRunResult(
                exit_status="sandbox_error",
                exit_code=None,
                log_path=None,
                summary=clean_summary(str(exc)),
                duration_seconds=duration,
            )

        duration = (utcnow() - started_at).total_seconds()
        stdout = read_log_text(result.stdout_path)
        stderr = read_log_text(result.stderr_path)
        sanitize_log_file(result.stdout_path)
        sanitize_log_file(result.stderr_path)

        summary = _extract_summary(stdout, stderr)
        token_usage = parse_token_usage(stdout, model_reference=req.model.reference)
        self._usage.record_usage(budget_key, req.model, token_usage)

        return AgentRunResult(
            exit_status=map_exit_status(result),
            exit_code=result.exit_code,
            log_path=result.stdout_path,
            summary=summary,
            token_usage=token_usage,
            duration_seconds=duration,
        )

    async def cancel(self, attempt_id: AttemptId) -> None:
        """Kill the deterministic container for ``attempt_id``."""
        await self._sandbox.kill(f"ds-{attempt_id}")

    # -- persistence ------------------------------------------------------

    def _prompt_root(self, workspace_path: Path) -> Path:
        return self._prompt_dir or (workspace_path.parent / "prompts")

    def _write_prompt(self, req: AgentRunRequest, workspace_path: Path, prompt: str) -> Path:
        path = self._prompt_root(workspace_path) / f"{req.workspace.attempt_id}.md"
        self._assert_outside_workspace(path, workspace_path)
        self._write_private(path, prompt)
        return path

    def _write_config(
        self, req: AgentRunRequest, workspace_path: Path, config: dict[str, Any]
    ) -> Path:
        path = self._prompt_root(workspace_path) / f"{req.workspace.attempt_id}.opencode.json"
        self._assert_outside_workspace(path, workspace_path)
        self._write_private(path, json.dumps(config, indent=2, sort_keys=True) + "\n")
        return path

    @staticmethod
    def _assert_outside_workspace(path: Path, workspace_path: Path) -> None:
        try:
            path.resolve().relative_to(workspace_path)
        except ValueError:
            return
        raise ConfigError(f"prompt/config path must be outside the repository: {path}")

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
        except OSError as exc:
            raise ConfigError(f"failed to write agent audit file {path}: {exc}") from exc


def parse_token_usage(text: str, *, model_reference: str = "opencode") -> TokenUsage | None:
    """Best-effort extraction of token usage from OpenCode JSON events."""
    per_message: dict[str, tuple[int, int]] = {}
    anonymous = (0, 0)
    found = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        for mapping in _walk_dicts(obj):
            tokens = mapping.get("tokens")
            if not isinstance(tokens, dict):
                continue
            input_tokens = _as_int(tokens.get("input"))
            output_tokens = _as_int(tokens.get("output"))
            if input_tokens == 0 and output_tokens == 0:
                continue
            found = True
            key = mapping.get("id") or mapping.get("messageID") or mapping.get("messageId")
            if isinstance(key, str) and key:
                previous = per_message.get(key, (0, 0))
                per_message[key] = (
                    max(previous[0], input_tokens),
                    max(previous[1], output_tokens),
                )
            else:
                anonymous = (max(anonymous[0], input_tokens), max(anonymous[1], output_tokens))

    if not found:
        input_tokens = _match_token_count(_TOKEN_INPUT_RE.search(text))
        output_tokens = _match_token_count(_TOKEN_OUTPUT_RE.search(text))
        if input_tokens or output_tokens:
            return TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                model=model_reference,
            )
        return None

    total_input = anonymous[0] + sum(value[0] for value in per_message.values())
    total_output = anonymous[1] + sum(value[1] for value in per_message.values())
    return TokenUsage(
        input_tokens=total_input,
        output_tokens=total_output,
        total_tokens=total_input + total_output,
        model=model_reference,
    )


def _extract_summary(stdout: str, stderr: str) -> str | None:
    texts: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        for mapping in _walk_dicts(obj):
            if mapping.get("type") == "text" and isinstance(mapping.get("text"), str):
                texts.append(mapping["text"])
    if texts:
        return clean_summary("\n".join(texts[-3:]))
    fallback = _ANSI_RE.sub("", stdout or stderr).strip()
    return clean_summary(fallback)


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    """Return every dict nested inside ``value`` (deterministic order)."""
    found: list[dict[str, Any]] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.append(item)
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return found


def _match_token_count(match: re.Match[str] | None) -> int:
    if match is None:
        return 0
    for group in match.groups():
        if group is not None:
            return int(group)
    return 0


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


__all__ = ["DEFAULT_AGENT_IMAGE", "OpenCodeAdapter", "parse_token_usage", "render_opencode_config"]
