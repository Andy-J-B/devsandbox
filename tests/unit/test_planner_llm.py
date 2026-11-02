"""LLM planner tests with a scripted LLM client (plan 06)."""

from __future__ import annotations

import json

import httpx
import pytest
from tests.planner_helpers import make_spec

from devsandbox.common.errors import LLMError, PlannerError
from devsandbox.common.redaction import register_secret, reset_redactor
from devsandbox.common.types import (
    CheckSpec,
    ProjectContext,
    RepoProfile,
    TokenUsage,
)
from devsandbox.config.policy import SecurityPolicy
from devsandbox.config.settings import DevSandboxSettings, PlannerConfig
from devsandbox.planner.llm import LLMPlanner, OpenAICompatibleClient
from devsandbox.planner.models import (
    FailureRecord,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    Plan,
    ValidationLimits,
)


class ScriptedLLM:
    """LLM client that returns a scripted sequence of responses."""

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.responses:
            raise LLMError("no more scripted responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _task_json(
    key: str = "task-a",
    *,
    depends_on: list[str] | None = None,
    resources: dict[str, object] | None = None,
    checks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "key": key,
        "title": f"Task {key}",
        "description": "Do the work",
        "instructions": "Implement the change and run the tests.",
        "depends_on": depends_on or [],
        "priority": 50,
        "success_criteria": ["The task is complete"],
        "checks": checks
        if checks is not None
        else [{"name": "tests", "run": "pytest -q", "required": True, "timeout_seconds": 60}],
        "expected_files": [],
        "restricted_files": [],
        "resources": resources
        or {"cpu_cores": 1.0, "memory_mb": 2048, "disk_mb": 4096, "pids_limit": 512},
        "timeout_seconds": 1800,
        "max_retries": 1,
    }


def _plan_json(tasks: list[dict[str, object]] | None = None) -> str:
    return json.dumps(
        {
            "tasks": tasks or [_task_json()],
            "rationale": "A focused graph.",
            "assumptions": ["Tests exist"],
            "risks": ["None"],
        }
    )


def _spec():
    return make_spec(
        goals=["Add feature A"],
        commands=[CheckSpec(name="tests", run="pytest -q", required=True)],
    )


def _planner(client: ScriptedLLM, *, fallback: bool = True, policy=None) -> LLMPlanner:
    return LLMPlanner(
        client,
        model="test-model",
        fallback=None
        if not fallback
        else __import__(
            "devsandbox.planner.templates", fromlist=["TemplatePlanner"]
        ).TemplatePlanner(),
        policy=policy,
        planner_config=PlannerConfig(),
        validation_limits=ValidationLimits(max_tasks=100, max_retries=2),
    )


async def test_valid_llm_json_becomes_a_plan() -> None:
    client = ScriptedLLM(
        [LLMResponse(content=_plan_json(), token_usage=TokenUsage(total_tokens=10))]
    )
    plan = await _planner(client).generate_plan(
        _spec(), RepoProfile(name="demo", revision="rev1"), ProjectContext()
    )
    assert plan.generated_by == "llm"
    assert plan.tasks[0].key == "task-a"
    assert plan.source_revision == "rev1"
    assert len(client.requests) == 1


async def test_malformed_json_is_repaired() -> None:
    client = ScriptedLLM(
        [
            LLMResponse(content="not json at all"),
            LLMResponse(content=_plan_json()),
        ]
    )
    plan = await _planner(client).generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "llm"
    assert len(client.requests) == 2
    assert "invalid" in client.requests[1].messages[-1].content.lower()


async def test_invalid_schema_is_repaired_with_feedback() -> None:
    client = ScriptedLLM(
        [
            LLMResponse(content=json.dumps({"tasks": "not-a-list"})),
            LLMResponse(content=_plan_json()),
        ]
    )
    plan = await _planner(client).generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "llm"
    assert len(client.requests) == 2


async def test_persistent_invalid_output_falls_back_to_template() -> None:
    client = ScriptedLLM([LLMResponse(content="still not json") for _ in range(3)])
    planner = _planner(client)
    plan = await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "template-fallback"
    assert planner.last_issues
    assert len(client.requests) == 3


async def test_hostile_task_count_falls_back() -> None:
    tasks = [_task_json(f"task-{index}") for index in range(150)]
    client = ScriptedLLM([LLMResponse(content=_plan_json(tasks)) for _ in range(3)])
    planner = _planner(client)
    plan = await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "template-fallback"
    assert any(issue.code == "graph.max_tasks" for issue in planner.last_issues)


async def test_hostile_cycle_falls_back() -> None:
    tasks = [
        _task_json("task-a", depends_on=["task-b"]),
        _task_json("task-b", depends_on=["task-a"]),
    ]
    client = ScriptedLLM([LLMResponse(content=_plan_json(tasks)) for _ in range(3)])
    planner = _planner(client)
    plan = await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "template-fallback"
    assert any(issue.code == "graph.cycle" for issue in planner.last_issues)


async def test_hostile_resources_are_clamped_by_policy() -> None:
    settings = DevSandboxSettings()
    policy = SecurityPolicy(settings)
    hostile = _task_json(
        resources={
            "cpu_cores": 999.0,
            "memory_mb": 999999,
            "disk_mb": 999999,
            "pids_limit": 999999,
        }
    )
    client = ScriptedLLM([LLMResponse(content=_plan_json([hostile]))])
    planner = _planner(client, policy=policy)
    plan = await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "llm"
    assert plan.tasks[0].resources.cpu_cores == policy.MAX_CPU_CORES
    assert plan.tasks[0].resources.memory_mb == policy.MAX_MEMORY_MB
    assert any(issue.code == "policy.resources_clamped" for issue in planner.last_issues)


async def test_unknown_network_field_is_rejected_then_falls_back() -> None:
    task = _task_json()
    task["network"] = {"mode": "full"}
    client = ScriptedLLM([LLMResponse(content=_plan_json([task])) for _ in range(3)])
    planner = _planner(client)
    plan = await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert plan.generated_by == "template-fallback"


async def test_no_fallback_raises_planner_error() -> None:
    client = ScriptedLLM([LLMResponse(content="nope") for _ in range(3)])
    planner = _planner(client, fallback=False)
    with pytest.raises(PlannerError):
        await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())


async def test_token_usage_is_accumulated() -> None:
    client = ScriptedLLM(
        [
            LLMResponse(content="bad", token_usage=TokenUsage(input_tokens=3, output_tokens=4)),
            LLMResponse(
                content=_plan_json(),
                token_usage=TokenUsage(input_tokens=5, output_tokens=6),
            ),
        ]
    )
    planner = _planner(client)
    await planner.generate_plan(_spec(), RepoProfile(name="demo"), ProjectContext())
    assert planner.last_token_usage is not None
    assert planner.last_token_usage.input_tokens == 8
    assert planner.last_token_usage.output_tokens == 10
    assert planner.last_token_usage.total_tokens == 18


async def test_prompt_redacts_registered_secrets() -> None:
    reset_redactor()
    register_secret("canary-secret-value")
    profile = RepoProfile(
        name="demo",
        conventions=["Never print canary-secret-value"],
    )
    client = ScriptedLLM([LLMResponse(content=_plan_json())])
    planner = _planner(client)
    await planner.generate_plan(_spec(), profile, ProjectContext())
    prompt = client.requests[0].messages[1].content
    assert "canary-secret-value" not in prompt
    assert "[REDACTED]" in prompt
    reset_redactor()


async def test_replan_includes_prior_failures() -> None:
    client = ScriptedLLM([LLMResponse(content=_plan_json()), LLMResponse(content=_plan_json())])
    planner = _planner(client)
    spec = _spec()
    first = await planner.generate_plan(spec, RepoProfile(name="demo"), ProjectContext())
    plan = Plan(id="plan-1", project_id="proj-1", version=1, tasks=first.tasks)
    failures = [FailureRecord(task_id="task-a", task_key="task-a", error="boom")]
    replanned = await planner.replan("proj-1", plan, failures)
    assert replanned.generated_by == "llm"
    assert "prior_failures" in client.requests[-1].messages[1].content


async def test_openai_compatible_client_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenAICompatibleClient(
            base_url="http://example.test/v1",
            model="test",
            api_key=__import__("pydantic").SecretStr("test-key"),
            client=http,
        )
        response = await client.complete(
            LLMRequest(model="test", messages=[LLMMessage(role="user", content="hi")])
        )
    assert response.content == "hello"
    assert response.token_usage is not None
    assert response.token_usage.total_tokens == 5
