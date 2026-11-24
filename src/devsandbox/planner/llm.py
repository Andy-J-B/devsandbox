"""Optional LLM-backed planner with bounded repair and deterministic fallback."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx
from pydantic import SecretStr, ValidationError

from ..common.errors import LLMError, PlannerError
from ..common.rate_limit import NoopRateLimiter, TokenBucketRateLimiter
from ..common.redaction import redact
from ..common.types import (
    ProjectContext,
    RepoProfile,
    TokenUsage,
)
from ..config.policy import SecurityPolicy
from ..config.settings import PlannerConfig
from ..spec.models import ProjectSpec
from .interfaces import LLMClient
from .models import (
    PLANNER_VERSION,
    FailureRecord,
    GraphIssue,
    GraphSeverity,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    Plan,
    ProposedPlan,
    TaskDraft,
    ValidationLimits,
)
from .normalize import apply_baseline_restrictions, apply_default_checks, normalize_tasks
from .templates import TemplatePlanner
from .validate import validate_graph

_SYSTEM_PROMPT = """You are the DevSandbox planner.
Return ONLY a JSON object with this shape:
{
  "tasks": [
    {
      "key": "stable-slug",
      "title": "short title",
      "description": "what to do",
      "instructions": "detailed instructions for a coding agent",
      "depends_on": ["other-task-key"],
      "priority": 50,
      "success_criteria": ["observable criterion"],
      "checks": [
        {"name": "tests", "run": "pytest -q", "required": true, "timeout_seconds": 300}
      ],
      "expected_files": ["src/example.py"],
      "restricted_files": ["LICENSE"],
      "resources": {"cpu_cores": 1.0, "memory_mb": 2048, "disk_mb": 4096, "pids_limit": 512},
      "timeout_seconds": 1800,
      "max_retries": 1
    }
  ],
  "rationale": "why this graph",
  "assumptions": ["..."],
  "risks": ["..."]
}
Rules:
- Every task needs non-empty instructions, at least one success criterion, and at
  least one required check.
- Dependencies must reference existing task keys and must be acyclic.
- Never request network access or elevated permissions.
- Do not include secrets or credentials.
- Keep the graph small and focused on the spec goals.
"""


def _safe(value: str) -> str:
    return cast(str, redact(value))


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("LLM response did not contain a JSON object")
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("LLM response JSON must be an object")
    return data


class LLMPlanner:
    """Planner that asks an OpenAI-compatible model for a structured task graph.

    Invalid output is repaired up to ``max_repairs`` times; after that the
    deterministic :class:`TemplatePlanner` is used and ``generated_by`` records
    the fallback honestly.
    """

    name = "llm"

    def __init__(
        self,
        client: LLMClient,
        *,
        model: str,
        fallback: TemplatePlanner | None = None,
        policy: SecurityPolicy | None = None,
        planner_config: PlannerConfig | None = None,
        validation_limits: ValidationLimits | None = None,
        max_repairs: int = 2,
        max_output_tokens: int | None = None,
        planner_version: str = PLANNER_VERSION,
    ) -> None:
        self._client = client
        self._model = model
        self._fallback = fallback
        self._policy = policy
        self._planner_config = planner_config or PlannerConfig()
        self._limits = validation_limits or ValidationLimits(
            max_tasks=self._planner_config.max_tasks,
            max_depth=self._planner_config.max_depth,
            max_timeout_seconds=self._planner_config.max_task_timeout_seconds,
            max_retries=2,
            min_priority=self._planner_config.min_priority,
            max_priority=self._planner_config.max_priority,
            max_expected_files=self._planner_config.max_expected_files,
        )
        self._max_repairs = max(0, max_repairs)
        self._max_output_tokens = max_output_tokens
        self._planner_version = planner_version

        self.last_token_usage: TokenUsage | None = None
        self.last_issues: list[GraphIssue] = []
        self.last_generated_by: str = self.name
        self._last_inputs: tuple[ProjectSpec, RepoProfile, ProjectContext] | None = None

    async def generate_plan(
        self, spec: ProjectSpec, profile: RepoProfile, ctx: ProjectContext
    ) -> ProposedPlan:
        """Generate a plan using the LLM, repairing invalid output when possible."""
        self._last_inputs = (spec, profile, ctx)
        return await self._generate(spec, profile, ctx, failures=[])

    async def replan(
        self, project_id: str, plan: Plan, failures: list[FailureRecord]
    ) -> ProposedPlan:
        """Replan with failure context, falling back to the template planner."""
        if self._last_inputs is None:
            if self._fallback is None:
                raise PlannerError(f"cannot replan project {project_id}: no prior planning inputs")
            return await self._fallback.replan(project_id, plan, failures)
        spec, profile, ctx = self._last_inputs
        return await self._generate(spec, profile, ctx, failures=failures)

    async def _generate(
        self,
        spec: ProjectSpec,
        profile: RepoProfile,
        ctx: ProjectContext,
        *,
        failures: list[FailureRecord],
    ) -> ProposedPlan:
        messages = [
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=self._user_prompt(spec, profile, ctx, failures=failures),
            ),
        ]
        total_usage = TokenUsage(model=self._model)
        last_issues: list[GraphIssue] = []

        for attempt in range(self._max_repairs + 1):
            response = await self._client.complete(
                LLMRequest(
                    model=self._model,
                    messages=messages,
                    max_tokens=self._max_output_tokens,
                )
            )
            if response.token_usage is not None:
                total_usage = self._add_usage(total_usage, response.token_usage)

            try:
                proposed = self._parse_plan(response.content, spec, profile)
                tasks, normalisation_issues = self._normalise(tasks=proposed.tasks, spec=spec)
                limits = self._limits_for(spec)
                issues = [
                    *normalisation_issues,
                    *validate_graph(tasks, limits=limits),
                ]
                last_issues = issues
                errors = [issue for issue in issues if issue.is_error]
                if not errors:
                    self.last_token_usage = total_usage
                    self.last_issues = issues
                    self.last_generated_by = self.name
                    return proposed.model_copy(
                        update={
                            "tasks": tasks,
                            "generated_by": self.name,
                            "planner_version": self._planner_version,
                            "source_revision": profile.revision,
                        }
                    )
                feedback = self._feedback_from_issues(errors)
            except LLMError as exc:
                feedback = f"Your previous response was invalid: {exc}"
                last_issues = [
                    GraphIssue(
                        code="llm.invalid_output",
                        severity=GraphSeverity.ERROR,
                        issue=str(exc),
                    )
                ]

            if attempt >= self._max_repairs:
                break
            messages = [
                *messages,
                LLMMessage(role="assistant", content=response.content[:8000]),
                LLMMessage(role="user", content=feedback),
            ]

        self.last_token_usage = total_usage
        self.last_issues = last_issues
        if self._fallback is None:
            raise PlannerError(
                "LLM planner produced no valid plan and no fallback is configured: "
                + "; ".join(issue.issue for issue in last_issues[:5])
            )

        fallback_plan = await self._fallback.generate_plan(spec, profile, ctx)
        self.last_generated_by = f"{self._fallback.name}-fallback"
        return fallback_plan.model_copy(
            update={
                "generated_by": self.last_generated_by,
                "rationale": (
                    "LLM output remained invalid after repair attempts; used the "
                    "deterministic template planner. " + fallback_plan.rationale
                ),
            }
        )

    def _parse_plan(self, content: str, spec: ProjectSpec, profile: RepoProfile) -> ProposedPlan:
        data = _extract_json(content)
        data.setdefault("generated_by", self.name)
        data.setdefault("planner_version", self._planner_version)
        data.setdefault("source_revision", profile.revision)
        try:
            return ProposedPlan.model_validate(data)
        except ValidationError as exc:
            raise LLMError(f"plan JSON failed schema validation: {exc}") from exc

    def _normalise(
        self, *, tasks: list[TaskDraft], spec: ProjectSpec
    ) -> tuple[list[TaskDraft], list[GraphIssue]]:
        if self._policy is None:
            normalised = apply_default_checks(tasks, spec.validation.commands)
            normalised = apply_baseline_restrictions(normalised, spec.constraints.restricted_paths)
            return normalised, []
        result = normalize_tasks(
            tasks,
            policy=self._policy,
            planner_config=self._planner_config,
            project_checks=spec.validation.commands,
            baseline_restricted_paths=spec.constraints.restricted_paths,
            max_retries=spec.limits.max_retries,
            max_timeout_seconds=spec.limits.task_timeout_seconds,
        )
        return result.tasks, result.issues

    def _limits_for(self, spec: ProjectSpec) -> ValidationLimits:
        update: dict[str, object] = {
            "baseline_restricted_paths": list(spec.constraints.restricted_paths)
        }
        if self._policy is not None:
            update["policy_max_resources"] = self._policy.max_resources()
        return self._limits.model_copy(update=update)

    def _user_prompt(
        self,
        spec: ProjectSpec,
        profile: RepoProfile,
        ctx: ProjectContext,
        *,
        failures: list[FailureRecord],
    ) -> str:
        profile_payload = {
            "name": profile.name,
            "languages": profile.languages,
            "frameworks": profile.frameworks,
            "package_managers": profile.package_managers,
            "manifests": profile.manifests,
            "test_commands": profile.effective_test_commands or profile.test_commands,
            "build_commands": profile.build_commands,
            "lint_commands": profile.lint_commands,
            "conventions": profile.conventions,
            "tree_summary": profile.tree_summary[:200],
            "warnings": profile.warnings,
        }
        payload: dict[str, Any] = {
            "spec": spec.model_dump(mode="json"),
            "repo_profile": profile_payload,
            "project_context": ctx.model_dump(mode="json"),
        }
        if failures:
            payload["prior_failures"] = [failure.model_dump(mode="json") for failure in failures]
        return _safe(
            "Create a task graph for this project.\n\n"
            + json.dumps(payload, indent=2, sort_keys=True)
        )

    def _feedback_from_issues(self, issues: list[GraphIssue]) -> str:
        lines = ["Your plan has validation errors. Fix them and return the full JSON again."]
        for issue in issues:
            location = f" task={issue.task_key}" if issue.task_key else ""
            lines.append(f"- [{issue.code}]{location}: {issue.issue}")
        return "\n".join(lines)

    def _add_usage(self, total: TokenUsage, usage: TokenUsage) -> TokenUsage:
        input_tokens = total.input_tokens + usage.input_tokens
        output_tokens = total.output_tokens + usage.output_tokens
        call_total = usage.total_tokens or usage.input_tokens + usage.output_tokens
        total_tokens = total.total_tokens + call_total
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=usage.model or total.model,
        )


class OpenAICompatibleClient:
    """Small async client for an OpenAI-compatible ``/chat/completions`` API.

    Calls are gated by an optional provider token bucket and retried with
    exponential backoff on transient failures (timeouts, transport errors,
    HTTP 408/429/5xx).  ``Retry-After`` is honoured when present.  Other 4xx
    responses fail immediately because retrying a malformed request or a bad
    key only wastes the provider budget.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucketRateLimiter | NoopRateLimiter | None = None,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        jitter: Callable[[], float] = random.random,
        jitter_ratio: float = 0.1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if backoff_base_seconds < 0 or backoff_max_seconds < 0:
            raise ValueError("backoff values must be >= 0")
        if not 0.0 <= jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0 and 1")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._rate_limiter = rate_limiter or NoopRateLimiter()
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._jitter = jitter
        self._jitter_ratio = jitter_ratio
        self._sleep = sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Call the chat completions endpoint and normalise the response."""
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        data = await self._post_with_retries(headers=headers, payload=payload)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM response did not contain any choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise LLMError("LLM response did not contain message content")

        usage_data = data.get("usage")
        usage: TokenUsage | None = None
        if isinstance(usage_data, dict):
            input_tokens = int(usage_data.get("prompt_tokens", 0) or 0)
            output_tokens = int(usage_data.get("completion_tokens", 0) or 0)
            total_tokens = int(usage_data.get("total_tokens", 0) or 0)
            usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens or input_tokens + output_tokens,
                model=request.model or self._model,
            )
        return LLMResponse(content=content, token_usage=usage)

    async def _post_with_retries(
        self, *, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        last_error = "LLM request failed"
        for attempt in range(self._max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"LLM request failed: {exc}"
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt))
                    continue
                raise LLMError(last_error) from exc
            except ValueError as exc:
                raise LLMError(f"LLM request failed: {exc}") from exc

            if self._retryable_status(response.status_code):
                last_error = (
                    f"LLM request failed with HTTP {response.status_code}: "
                    f"{redact(response.text[:500])}"
                )
                if attempt < self._max_retries:
                    await self._sleep(
                        self._retry_delay(attempt, response.headers.get("Retry-After"))
                    )
                    continue
                raise LLMError(last_error)

            try:
                response.raise_for_status()
                parsed = response.json()
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    f"LLM request failed with HTTP {exc.response.status_code}: "
                    f"{redact(exc.response.text[:500])}"
                ) from exc
            except ValueError as exc:
                raise LLMError(f"LLM response was not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise LLMError("LLM response JSON must be an object")
            return parsed
        raise LLMError(last_error)

    @staticmethod
    def _retryable_status(status: int) -> bool:
        return status in {408, 425, 429} or 500 <= status < 600

    def _backoff_delay(self, attempt: int) -> float:
        delay = min(self._backoff_max_seconds, self._backoff_base_seconds * (2**attempt))
        if delay <= 0:
            return 0.0
        return float(delay + delay * self._jitter() * self._jitter_ratio)

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        server_delay = _parse_retry_after(retry_after)
        if server_delay is not None:
            return min(self._backoff_max_seconds, max(0.0, server_delay))
        return self._backoff_delay(attempt)

    async def aclose(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_client:
            await self._client.aclose()


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP date)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


__all__ = ["LLMPlanner", "OpenAICompatibleClient"]
