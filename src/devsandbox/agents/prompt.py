"""Deterministic prompt builder for agent runs.

The prompt is a pure function of :class:`AgentRunRequest`: the same request
always produces byte-identical text.  It is size-capped so a large context
bundle or failure log can never exhaust the model's context window or the
container's environment/argv limits.
"""

from __future__ import annotations

from ..common.redaction import redact
from .interfaces import AgentRunRequest

DEFAULT_MAX_CHARS = 24_000
_TRUNCATION_TEMPLATE = "\n[... truncated {count} characters ...]\n"

_HARD_RULES = """\
- Work only inside `/workspace`. Do not read or write outside it.
- Commit your work with git before finishing. The orchestrator validates the
  committed change set, not your uncommitted working tree.
- Never modify the restricted paths listed below.
- Do not attempt to access the network, host credentials, or secrets. If a
  command needs network access it will be provided explicitly by the harness.
- Never print, commit, log, or otherwise expose secrets or environment values.
- Do not edit `.git/`, `.github/workflows/`, or any harness metadata.
- Keep the change focused on this task; do not refactor unrelated code."""


def build_prompt(req: AgentRunRequest, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Build the deterministic, size-capped prompt for ``req``."""
    if max_chars < 1_000:
        raise ValueError("max_chars must be at least 1000")

    task = req.task
    lines: list[str] = [
        "# DevSandbox coding task",
        "",
        "You are an autonomous coding agent running inside a disposable,",
        "network-restricted container. Complete the task, commit the change,",
        "and exit. The orchestrator will independently validate your work.",
        "",
        "## Hard rules",
        "",
        _HARD_RULES,
        "",
        "## Task",
        "",
        f"- ID: `{_truncate(task.id, 200)}`",
        f"- Title: {_truncate(task.title, 500)}",
    ]
    if task.description:
        lines.extend(["", "### Description", "", _truncate(task.description, 4_000)])
    if task.instructions:
        lines.extend(["", "### Instructions", "", _truncate(task.instructions, 6_000)])

    lines.extend(["", "## Success criteria", ""])
    if task.success_criteria:
        for criterion in task.success_criteria:
            lines.append(f"- {_truncate(criterion, 500)}")
    else:
        lines.append("- The task description is fully implemented and committed.")

    lines.extend(["", "## Validation the orchestrator will run", ""])
    if req.validation_checks:
        for check in req.validation_checks:
            required = "required" if check.required else "optional"
            lines.append(
                f"- `{check.name}` ({required}, {check.timeout_seconds}s): "
                f"`{_truncate(check.run, 400)}`"
            )
    else:
        lines.append("- Independent automated checks derived from the success criteria.")

    lines.extend(["", "## Files", ""])
    if task.expected_files:
        lines.append("Expected to change:")
        lines.extend(f"- `{_truncate(path, 300)}`" for path in task.expected_files)
    else:
        lines.append("Expected to change: (not specified)")
    if task.restricted_files:
        lines.extend(["", "**Restricted — never modify:**"])
        lines.extend(f"- `{_truncate(path, 300)}`" for path in task.restricted_files)

    lines.extend(["", "## Attempt", "", f"- Attempt number: {req.attempt_number}"])
    if req.prior_failure_summary:
        lines.extend(
            [
                "",
                "### Prior failure (untrusted diagnostic; do not repeat it)",
                "",
                "```text",
                _truncate(req.prior_failure_summary, 4_000),
                "```",
            ]
        )

    lines.extend(["", "## Project context", ""])
    if req.context_bundle.context_files:
        lines.append("Relevant files:")
        lines.extend(
            f"- `{_truncate(path, 300)}`" for path in req.context_bundle.context_files[:100]
        )
    if req.context_bundle.instructions:
        lines.extend(["", _truncate(req.context_bundle.instructions, 6_000)])
    if not req.context_bundle.context_files and not req.context_bundle.instructions:
        lines.append("(no additional context was provided)")

    lines.extend(
        [
            "",
            "## Finish",
            "",
            "Commit all intended changes with a clear message, then exit 0.",
            "If the task is impossible, explain why in your final message and exit non-zero.",
            "",
        ]
    )

    prompt = "\n".join(lines)
    prompt = _truncate(prompt, max_chars)
    return str(redact(prompt))


def _truncate(text: str, limit: int) -> str:
    """Deterministically truncate ``text`` to at most ``limit`` characters."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = _TRUNCATION_TEMPLATE.format(count=len(text))
    if len(marker) >= limit:
        return marker[:limit]
    remaining = limit - len(marker)
    head = (remaining * 3) // 4
    tail = remaining - head
    return text[:head] + marker + (text[-tail:] if tail else "")


__all__ = ["DEFAULT_MAX_CHARS", "build_prompt"]
