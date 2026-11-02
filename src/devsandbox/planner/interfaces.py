"""Planner and LLM-client protocols plus shared graph validation export."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.types import ProjectContext, RepoProfile
from ..spec.models import ProjectSpec
from .models import FailureRecord, LLMRequest, LLMResponse, Plan, ProposedPlan
from .validate import validate_graph

PlannerName = str


@runtime_checkable
class Planner(Protocol):
    """Protocol implemented by every task planner."""

    async def generate_plan(
        self, spec: ProjectSpec, profile: RepoProfile, ctx: ProjectContext
    ) -> ProposedPlan: ...

    async def replan(
        self, project_id: str, plan: Plan, failures: list[FailureRecord]
    ) -> ProposedPlan: ...


@runtime_checkable
class LLMClient(Protocol):
    """Minimal OpenAI-compatible chat completion client."""

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


__all__ = ["LLMClient", "Planner", "PlannerName", "validate_graph"]
