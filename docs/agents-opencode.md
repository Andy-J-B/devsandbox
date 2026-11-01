# Agent adapter & OpenCode (plan 04)

The agent layer turns a task + context bundle into a non-interactive run
inside a disposable sandbox container. It is runtime-agnostic:

- `AgentAdapter` Protocol + request/result models:
  `src/devsandbox/agents/interfaces.py`
- Registry for runtime lookup: `src/devsandbox/agents/registry.py`
- First runtime: `OpenCodeAdapter` (`src/devsandbox/agents/opencode.py`)
- Deterministic no-LLM runtime: `FakeScriptAdapter`
  (`src/devsandbox/agents/fake.py`, script in
  `src/devsandbox/agents/fake_agent_script.py`)
- Prompt builder: `src/devsandbox/agents/prompt.py`
- Model budget/usage: `src/devsandbox/agents/models.py`
- Secrets: `src/devsandbox/agents/secrets.py`
- Agent image: `docker/agent-base/`

## Verified OpenCode CLI research

Research performed **2026-10-31** against the installed binary
(`opencode v2.1.4`) and the official CLI/config docs. The image is pinned to
**OpenCode 1.19.5**, the current `opencode-ai` npm `latest` and GitHub
release, because that is the version the npm-based Docker build installs
reproducibly. The invocation below is identical in 1.19.5 and 2.1.4.

### Non-interactive invocation (verified with `--help`)

```bash
opencode --version
opencode run \
  --model provider/model \
  --format json \
  --auto \
  --title "devsandbox <task> attempt <n>" \
  "<prompt>"
```

`--log-level` is intentionally not passed by the adapter: its accepted values
differ between OpenCode 1 (`ERROR`) and 2 (`error`), and the JSON event stream
is enough for the adapter. OpenCode writes its own logs to its data directory.

Verified flags:

| Flag | Meaning |
| --- | --- |
| `opencode run [message..]` | Non-interactive mode; prompt as an argument |
| `--model`, `-m` | `provider/model` (model ids may contain `/`, e.g. OpenRouter) |
| `--format` | `default` or `json` (JSON event stream) |
| `--auto` | Auto-approve permissions not explicitly denied |
| `--log-level` | `error` keeps stdout/stderr focused on the run |
| `--title` | Deterministic session title for audit |
| `--continue` / `--session` | Resume a session (not used here) |

`opencode` with no subcommand starts the TUI, so the adapter always uses
`run` explicitly.

### Configuration

| Method | Value |
| --- | --- |
| Global file | `~/.config/opencode/opencode.json` |
| Project file | `opencode.json` in the repo (not used; the image has a safe global default) |
| Custom path | `OPENCODE_CONFIG=/path/to/config.json` |
| Inline override | `OPENCODE_CONFIG_CONTENT=<json>` (used by the adapter) |
| Config dir | `OPENCODE_CONFIG_DIR=/path` |
| Env substitution | `{env:NAME}` / `{file:path}` inside config values |

The adapter renders the per-attempt config into
`OPENCODE_CONFIG_CONTENT`, so no config file is mounted into the container
and the config is never committed to the repo. A safe baseline config is
also copied to `~/.config/opencode/opencode.json` in the image.

Keys used:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "share": "disabled",
  "snapshot": false,
  "model": "provider/model",
  "small_model": "provider/model",
  "permission": {
    "edit": "allow",
    "bash": "allow",
    "webfetch": "deny",
    "websearch": "deny",
    "external_directory": "deny"
  },
  "tools": {"webfetch": false, "websearch": false},
  "provider": {
    "custom-endpoint": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "custom-endpoint",
      "options": {
        "baseURL": "http://host:port/v1",
        "apiKey": "{env:DEVSANDBOX_MODEL_API_KEY}"
      },
      "models": {"model-id": {"name": "model-id"}}
    }
  }
}
```

No secret value is ever written to the config: the key is referenced as
`{env:NAME}` and the value is injected through `SandboxRequest.secret_env`.

### Disabling update/telemetry/share

- `share: "disabled"` (config) — sessions are never shared.
- `autoupdate: false` (config) plus `OPENCODE_DISABLE_AUTOUPDATE=1`.
- `OPENCODE_DISABLE_PRUNE=1`, `OPENCODE_DISABLE_TERMINAL_TITLE=1`.
- `OPENCODE_DISABLE_CLAUDE_CODE*`, `OPENCODE_DISABLE_LSP_DOWNLOAD=1`,
  `OPENCODE_DISABLE_PROJECT_CONFIG=1`, and
  `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`.
- The sandbox network policy is the real backstop: `network=none` gives the
  container no route at all, so any telemetry/update attempt fails closed.
  OpenCode 1 does not expose a separate documented telemetry switch; the
  container-level network policy is authoritative.

### Output and exit codes

`opencode run --format json` writes one JSON event per line. The adapter:

- reads the last assistant `text` part as the summary (**untrusted**);
- extracts `tokens.input` / `tokens.output` from `step_finish` (or message)
  events when present (best effort, with a text fallback). A real captured
  event looks like
  `{"type":"step_finish","part":{"tokens":{"input":10,"output":5,...}}}`;
- maps container outcomes: `0 → exited_ok`, non-zero → `exited_error`,
  timeout → `timeout`, OOM → `oom`, signal/unknown → `crashed`,
  infrastructure failure → `sandbox_error`.

OpenCode's process exit code is preserved in `AgentRunResult.exit_code`.
Nothing the agent reports is trusted for completion; plan 09 validates the
change set independently.

## Zero-cost model setup

### Option A — local Ollama (fully free, no API key)

```bash
ollama pull qwen2.5-coder:7b
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

In the project spec / orchestrator model config:

```yaml
provider: ollama
model: qwen2.5-coder:7b
base_url: http://host.docker.internal:11434/v1   # Docker Desktop
# Linux bridge alternative: http://172.17.0.1:11434/v1
```

The adapter renders an OpenAI-compatible provider using `base_url`. Ollama
does not require a key, so `secret_ref` can be omitted. The container needs
network access to the host endpoint; the current sandbox manager does not
implement `allowlist` network mode, so use `full` (egress still restricted by
the host/container network setup) or add allowlist support in plan 02.

### Option B — free hosted tier (OpenRouter)

```bash
export OPENROUTER_API_KEY=...        # free key from openrouter.ai
```

```yaml
provider: openrouter
model: "<free-model-id>:free"
secret_ref: model
```

```toml
# devsandbox.toml
[secrets.model]
env_var = "OPENROUTER_API_KEY"
```

The adapter maps known providers to their standard env names
(`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...), and uses
`DEVSANDBOX_MODEL_API_KEY` for custom OpenAI-compatible endpoints. Only the
secret named by the model config is placed in `secret_env`; the rest of the
host environment is never forwarded.

### Option C — paid provider

Use the provider's normal `provider/model` and `secret_ref`. Billing is
tracked as inference usage (tokens); the adapter records amount 0 for free
models and leaves price lookup to the reporting layer.

## Model config and budget

`ModelConfig` (`common/types.py`) fields: `provider`, `model`, `base_url`,
`secret_ref`, `max_tokens`, `max_requests`. `name` is a backwards-compatible
fallback for the model id. `ModelConfig.reference` returns
`provider/model`.

`UsageLimiter` (`agents/models.py`) checks
`settings.agent.model_budget` before every run and records usage afterwards.
`BudgetExceeded` propagates to the caller; plan 07 treats it as "do not
launch". Usage/cost persistence is injected (`read`, `record`,
`cost_recorder`) so plan 08 can wire `usage_counters` and
`cost_entries kind="inference"`.

## Secrets

`SecretProvider.resolve` accepts:

- a `SecretRef` (`env_var` or `path`),
- a name in `settings.secrets`,
- an existing file path (must be mode `0600`; group/other-readable files are
  rejected),
- a host env var name.

Resolved values are registered with `common.redaction` and returned as
`SecretStr`. Prompt, config, command, labels, and result objects are redacted.

## Fake agent (E2E without an LLM)

`FakeScriptAdapter` runs the deterministic script through the real sandbox
manager. Modes:

| Mode | Behaviour |
| --- | --- |
| `success` (default) | writes `AGENT_OUTPUT.md`, commits, exits 0 |
| `fail` | exits 1 without changes |
| `hang` | sleeps until the sandbox times out |
| `no-commit` | edits but leaves the tree dirty |
| `touch-restricted` | commits `.github/workflows/devsandbox-fake.yml` |
| `lie` | claims success, makes no changes |
| `leak-secret` | prints a secret env value to stdout |

The script is self-contained and can also be run on the host:

```bash
python -m devsandbox.agents.fake_agent_script --fail
```

## Adding another runtime

Implement `name`, `run`, `cancel` and register it:

```python
class EchoAdapter:
    name = "echo"

    async def run(self, req: AgentRunRequest) -> AgentRunResult:
        return AgentRunResult(exit_status="exited_ok", summary=req.task.title)

    async def cancel(self, attempt_id: AttemptId) -> None:
        return None


registry.register("echo", EchoAdapter())
```

`tests/unit/test_agent_registry.py` uses exactly this ~20-line adapter and
asserts it satisfies the Protocol and registry lookup.

## Agent image

`docker/agent-base/Dockerfile` builds on `node:22-bookworm-slim` and adds
Python 3, pip, git, build-essential, ripgrep, curl, and
`opencode-ai@${OPENCODE_VERSION}` (default `1.19.5`). It runs as non-root
`agent` uid 1000, has no Docker CLI/socket, sudo, SSH keys, or cloud
credentials, and its root filesystem is read-only under the sandbox manager.

```bash
docker/agent-base/build.sh             # build + version/tool smoke tests
OPENCODE_VERSION=1.19.5 docker/agent-base/build.sh
```

## Testing

```bash
pytest tests/unit -q                    # no Docker/LLM required
pytest -q                               # full suite (Docker tests skip if unavailable)

# Opt-in image build + OpenCode smoke test:
DEVSANDBOX_RUN_OPENCODE_INTEGRATION=1 pytest -m opencode

# Optional live free-model task:
DEVSANDBOX_OPENCODE_LIVE=1 \
DEVSANDBOX_OPENCODE_PROVIDER=openrouter \
DEVSANDBOX_OPENCODE_MODEL='<free-model>:free' \
DEVSANDBOX_OPENCODE_API_KEY=... \
pytest -m opencode tests/integration/test_agent_image.py
```

Unit coverage includes prompt determinism/truncation/redaction, every
sandbox exit mapping, cancel, prompt/config placement outside the repo,
secret-env-only injection, budget blocking/recording, fake-agent modes, and
the second-runtime registry example.

## Out of scope

Container lifecycle (plan 02), retry decisions (plan 08), validation
(plan 09), and reporting/API (plan 10).
