"""Fake planner implementing the Planner protocol."""

from __future__ import annotations

from devsandbox.planner.types import Plan, ProposedPlan


class FakePlanner:
    """Scriptable fake planner."""

    def __init__(self) -> None:
        self.plans: list[ProposedPlan] = []
        self.replans: list[ProposedPlan] = []

    async def generate_plan(self, spec, profile, ctx) -> ProposedPlan:
        """Generate a plan."""
        plan = ProposedPlan(tasks=[], rationale="fake")
        self.plans.append(plan)
        return plan

    async def replan(self, project_id: str, plan: Plan, failures) -> ProposedPlan:
        """Replan from failures."""
        new_plan = ProposedPlan(tasks=[], rationale="fake-replan")
        self.replans.append(new_plan)
        return new_plan
