"""Deterministic template planner.

The template planner is the zero-cost default: it needs no model access and
turns the spec goals into a linear task graph with project-level validation
checks.  It is also the fallback for the optional LLM planner.
"""

from __future__ import annotations

import re
from typing import cast

from ..common.redaction import redact
from ..common.types import (
    CheckSpec,
    ProjectContext,
    RepoProfile,
    ResourceProfile,
)
from ..spec.models import ProjectSpec
from .models import PLANNER_VERSION, FailureRecord, Plan, ProposedPlan, TaskDraft

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 60) -> str:
    """Turn arbitrary text into a stable task-key slug."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_length].strip("-") or "task"


def _safe_text(text: str) -> str:
    return cast(str, redact(text))


class TemplatePlanner:
    """Build a plan from spec goals without any model access."""

    name = "template"

    def __init__(
        self,
        *,
        include_baseline: bool = False,
        include_final_verification: bool = True,
        planner_version: str = PLANNER_VERSION,
    ) -> None:
        self.include_baseline = include_baseline
        self.include_final_verification = include_final_verification
        self.planner_version = planner_version
        self._last_inputs: tuple[ProjectSpec, RepoProfile, ProjectContext] | None = None

    async def generate_plan(
        self, spec: ProjectSpec, profile: RepoProfile, ctx: ProjectContext
    ) -> ProposedPlan:
        """Generate a deterministic plan from the spec goals."""
        self._last_inputs = (spec, profile, ctx)
        checks = self._project_checks(spec, profile)
        goals = self._goals(spec)
        tasks: list[TaskDraft] = []
        previous: str | None = None

        if self.include_baseline:
            tasks.append(self._baseline_task(spec, profile, checks))
            previous = tasks[-1].key

        goal_keys: list[str] = []
        for index, goal in enumerate(goals, start=1):
            key = f"goal-{index}-{slugify(goal)}"
            depends_on = [previous] if previous else []
            tasks.append(
                TaskDraft(
                    key=key,
                    title=goal,
                    description=f"Deliver project goal: {goal}",
                    instructions=self._goal_instructions(spec, profile, ctx, goal),
                    depends_on=depends_on,
                    priority=50,
                    success_criteria=[
                        f"Goal is implemented: {goal}",
                        "All required validation checks pass",
                    ],
                    checks=[check.model_copy() for check in checks],
                    restricted_files=list(spec.constraints.restricted_paths),
                    resources=self._default_resources(),
                    timeout_seconds=spec.limits.task_timeout_seconds,
                    max_retries=spec.limits.max_retries,
                )
            )
            goal_keys.append(key)
            previous = key

        if self.include_final_verification:
            tasks.append(
                TaskDraft(
                    key="final-verification",
                    title="Final integration verification",
                    description="Verify the integrated result against the project requirements.",
                    instructions=self._final_instructions(spec, profile, ctx),
                    depends_on=goal_keys,
                    priority=10,
                    success_criteria=[
                        "All required validation checks pass on the integrated result",
                        "Project goals and requirements are satisfied",
                    ],
                    checks=[check.model_copy() for check in checks],
                    restricted_files=list(spec.constraints.restricted_paths),
                    resources=self._default_resources(),
                    timeout_seconds=spec.limits.task_timeout_seconds,
                    max_retries=spec.limits.max_retries,
                )
            )

        return ProposedPlan(
            tasks=tasks,
            rationale=(
                f"Generated one task per spec goal ({len(goals)}) using the deterministic "
                "template planner."
            ),
            assumptions=[
                "Goals are ordered and should be implemented sequentially.",
                "Detected validation commands are sufficient to verify the work.",
            ],
            risks=[
                "The template planner does not infer hidden dependencies between goals.",
            ],
            generated_by=self.name,
            planner_version=self.planner_version,
            source_revision=profile.revision,
        )

    async def replan(
        self, project_id: str, plan: Plan, failures: list[FailureRecord]
    ) -> ProposedPlan:
        """Regenerate the plan and enrich failed tasks with failure context."""
        if self._last_inputs is not None:
            spec, profile, ctx = self._last_inputs
            fresh = await self.generate_plan(spec, profile, ctx)
            by_key = {failure.task_key or failure.task_id: failure for failure in failures}
            tasks: list[TaskDraft] = []
            for task in fresh.tasks:
                failure = by_key.get(task.key)
                if failure is None:
                    tasks.append(task)
                    continue
                note = self._failure_note(failure)
                tasks.append(
                    task.model_copy(
                        update={
                            "instructions": f"{task.instructions}\n\n{note}",
                            "success_criteria": [
                                *task.success_criteria,
                                "Address the previous failure before completing the task",
                            ],
                        }
                    )
                )
            return fresh.model_copy(
                update={
                    "tasks": tasks,
                    "rationale": (
                        f"Template replan for project {project_id}: previous plan version "
                        f"{plan.version} had {len(failures)} failed task(s)."
                    ),
                    "generated_by": f"{self.name}-replan",
                }
            )

        tasks = [self._failure_task(failure) for failure in failures]
        return ProposedPlan(
            tasks=tasks,
            rationale=f"Template replan from {len(failures)} failure record(s).",
            generated_by=f"{self.name}-replan",
            planner_version=self.planner_version,
        )

    def _goals(self, spec: ProjectSpec) -> list[str]:
        if spec.goals:
            return list(spec.goals)
        if spec.requirements:
            return ["Implement the project requirements"]
        if spec.description:
            return [spec.description]
        return ["Complete the project"]

    def _project_checks(self, spec: ProjectSpec, profile: RepoProfile) -> list[CheckSpec]:
        checks: list[CheckSpec]
        if spec.validation.commands:
            checks = [check.model_copy() for check in spec.validation.commands]
            if not any(check.required for check in checks):
                checks.append(self._sanity_check(spec))
            return checks

        commands = profile.effective_test_commands or profile.test_commands
        checks = []
        for index, command in enumerate(commands, start=1):
            name = "tests" if index == 1 else f"tests-{index}"
            checks.append(
                CheckSpec(
                    name=name,
                    run=command,
                    required=True,
                    timeout_seconds=min(300, spec.limits.task_timeout_seconds),
                )
            )
        for index, command in enumerate(profile.build_commands, start=1):
            name = "build" if index == 1 else f"build-{index}"
            checks.append(
                CheckSpec(
                    name=name,
                    run=command,
                    required=False,
                    timeout_seconds=min(300, spec.limits.task_timeout_seconds),
                )
            )
        if not checks:
            checks.append(self._sanity_check(spec))
        return checks

    def _sanity_check(self, spec: ProjectSpec) -> CheckSpec:
        return CheckSpec(
            name="repo-sanity",
            run="git diff --check",
            required=True,
            timeout_seconds=min(300, spec.limits.task_timeout_seconds),
        )

    def _default_resources(self) -> ResourceProfile:
        return ResourceProfile()

    def _baseline_task(
        self, spec: ProjectSpec, profile: RepoProfile, checks: list[CheckSpec]
    ) -> TaskDraft:
        return TaskDraft(
            key="baseline",
            title="Baseline environment sanity check",
            description="Confirm the repository can be built and tested before changes.",
            instructions=(
                "Inspect the repository and run the required validation commands. "
                "Do not change project behaviour; report any pre-existing failures.\n\n"
                + self._context_block(spec, profile)
            ),
            priority=90,
            success_criteria=[
                "The baseline validation commands were executed",
                "Any pre-existing failures are documented",
            ],
            checks=[check.model_copy() for check in checks],
            restricted_files=list(spec.constraints.restricted_paths),
            resources=self._default_resources(),
            timeout_seconds=spec.limits.task_timeout_seconds,
            max_retries=spec.limits.max_retries,
        )

    def _goal_instructions(
        self,
        spec: ProjectSpec,
        profile: RepoProfile,
        ctx: ProjectContext,
        goal: str,
    ) -> str:
        requirements = "\n".join(f"- {item}" for item in ctx.requirements or spec.requirements)
        constraints = "\n".join(f"- {item}" for item in ctx.constraints)
        body = [
            f"Goal: {goal}",
            f"Project: {spec.name}",
            self._context_block(spec, profile),
        ]
        if requirements:
            body.append(f"Requirements:\n{requirements}")
        if constraints:
            body.append(f"Constraints:\n{constraints}")
        body.append(
            "Keep changes focused on this goal. Do not modify restricted paths. "
            "Commit your work in the workspace when finished."
        )
        return _safe_text("\n\n".join(body))

    def _final_instructions(
        self, spec: ProjectSpec, profile: RepoProfile, ctx: ProjectContext
    ) -> str:
        requirements = "\n".join(f"- {item}" for item in ctx.requirements or spec.requirements)
        body = [
            "Verify the integrated result of all completed tasks.",
            f"Project: {spec.name}",
            self._context_block(spec, profile),
        ]
        if requirements:
            body.append(f"Requirements:\n{requirements}")
        body.append(
            "Run every required validation command and fix only issues introduced by the "
            "plan. Do not modify restricted paths."
        )
        return _safe_text("\n\n".join(body))

    def _context_block(self, spec: ProjectSpec, profile: RepoProfile) -> str:
        lines = [f"Repository: {profile.name}"]
        if profile.languages:
            lines.append("Languages: " + ", ".join(profile.languages))
        if profile.frameworks:
            lines.append("Frameworks: " + ", ".join(profile.frameworks))
        if profile.conventions:
            lines.append("Conventions:\n" + "\n".join(f"- {item}" for item in profile.conventions))
        commands = profile.effective_test_commands or profile.test_commands
        if commands:
            lines.append("Test commands:\n" + "\n".join(f"- {item}" for item in commands))
        if spec.constraints.languages:
            lines.append("Allowed languages: " + ", ".join(spec.constraints.languages))
        if spec.constraints.restricted_paths:
            lines.append("Restricted paths: " + ", ".join(spec.constraints.restricted_paths))
        return _safe_text("\n".join(lines))

    def _failure_note(self, failure: FailureRecord) -> str:
        parts = ["Previous attempt failed."]
        if failure.summary:
            parts.append(f"Summary: {failure.summary}")
        if failure.error:
            parts.append(f"Error: {failure.error}")
        if failure.validation_summary:
            parts.append(f"Validation: {failure.validation_summary}")
        return _safe_text("\n".join(parts))

    def _failure_task(self, failure: FailureRecord) -> TaskDraft:
        key = slugify(failure.task_key or failure.task_id)
        return TaskDraft(
            key=key,
            title=f"Retry failed task {failure.task_key or failure.task_id}",
            description="Recover a failed task using the recorded failure context.",
            instructions=self._failure_note(failure),
            success_criteria=["The previous failure is resolved", "Required checks pass"],
            checks=[CheckSpec(name="repo-sanity", run="git diff --check", required=True)],
            timeout_seconds=1800,
            max_retries=1,
        )


__all__ = ["TemplatePlanner", "slugify"]
