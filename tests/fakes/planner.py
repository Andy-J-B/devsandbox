"""Fake planner implementing the Planner protocol with scripted responses."""

from __future__ import annotations

from devsandbox.common.types import CheckSpec
from devsandbox.planner.models import Plan, ProposedPlan, TaskDraft


def _default_plan() -> ProposedPlan:
    return ProposedPlan(
        tasks=[
            TaskDraft(
                key="fake-task",
                title="Fake task",
                description="A scripted fake task.",
                instructions="Make the requested change and run the checks.",
                success_criteria=["The fake task is complete", "Required checks pass"],
                checks=[
                    CheckSpec(name="tests", run="pytest -q", required=True, timeout_seconds=60)
                ],
            )
        ],
        rationale="fake",
        generated_by="fake",
    )


class FakePlanner:
    """Scriptable fake planner.

    Queued plans are returned in order.  When the queue is empty a small valid
    plan is returned so service tests do not need to configure anything.
    """

    name = "fake"

    def __init__(
        self,
        plans: list[ProposedPlan] | None = None,
        replans: list[ProposedPlan] | None = None,
    ) -> None:
        self.plans: list[ProposedPlan] = list(plans or [])
        self.replans: list[ProposedPlan] = list(replans or [])
        self.requests: list[tuple[object, object, object]] = []
        self.replan_requests: list[tuple[str, Plan, list[object]]] = []
        self.last_token_usage = None

    def queue_plan(self, plan: ProposedPlan) -> None:
        """Queue a plan to be returned by the next ``generate_plan`` call."""
        self.plans.append(plan)

    def queue_replan(self, plan: ProposedPlan) -> None:
        """Queue a plan to be returned by the next ``replan`` call."""
        self.replans.append(plan)

    async def generate_plan(self, spec, profile, ctx) -> ProposedPlan:
        """Generate a scripted plan."""
        self.requests.append((spec, profile, ctx))
        if self.plans:
            return self.plans.pop(0)
        return _default_plan()

    async def replan(self, project_id: str, plan: Plan, failures) -> ProposedPlan:
        """Return a scripted replan (or the original tasks when unscripted)."""
        self.replan_requests.append((project_id, plan, list(failures)))
        if self.replans:
            return self.replans.pop(0)
        return ProposedPlan(
            tasks=list(plan.tasks),
            rationale="fake-replan",
            generated_by="fake-replan",
        )
