"""Plan lifecycle service: create, approve, edit, reject, replan, diff.

The service persists plans/tasks/dependencies through the plan-01 repositories.
Callers own the surrounding transaction; every method flushes before returning.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..common.errors import (
    ConfigError,
    PlanLimitError,
    PlanNotFoundError,
    PlanStateError,
    PlanValidationError,
    PolicyViolation,
    ProjectNotFoundError,
)
from ..common.ids import new_plan_id, new_task_id
from ..common.time import utcnow
from ..common.types import (
    CheckSpec,
    ProjectContext,
    ProjectMode,
    RepoHandle,
    RepoProfile,
    RepoSource,
    ResourceProfile,
    TaskStatus,
)
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings, get_settings
from ..context.interfaces import ContextManager
from ..db.models import CostEntryModel, EventModel, PlanModel, ProjectModel, TaskModel
from ..db.repositories import CostRepo, EventRepo, PlanRepo, ProjectRepo, TaskRepo
from ..spec.models import ProjectSpec, load_spec
from .interfaces import Planner
from .models import (
    FailureRecord,
    GraphIssue,
    Plan,
    PlanDiff,
    PlanPatch,
    PlanStatus,
    ProposedPlan,
    TaskDiff,
    TaskDraft,
    ValidationLimits,
)
from .normalize import normalize_tasks
from .validate import (
    GraphCycleError,
    topological_order,
    validate_graph,
    validation_limits_from_settings,
)

_COMPLETED = TaskStatus.COMPLETED.value
_PENDING = TaskStatus.PENDING.value
_PROPOSED = PlanStatus.PROPOSED.value
_ACTIVE = PlanStatus.ACTIVE.value
_REJECTED = PlanStatus.REJECTED.value
_SUPERSEDED = PlanStatus.SUPERSEDED.value


class PlanService:
    """Create, approve, edit, reject, and replan versioned task graphs."""

    def __init__(
        self,
        session: AsyncSession,
        planner: Planner,
        policy: SecurityPolicy,
        *,
        context_manager: ContextManager | None = None,
        repo_lookup: Callable[[str], RepoHandle] | None = None,
        settings: DevSandboxSettings | None = None,
        cost_repo: CostRepo | None = None,
        event_repo: EventRepo | None = None,
        auto_approve: bool = True,
    ) -> None:
        self.session = session
        self.planner = planner
        self.policy = policy
        self.context_manager = context_manager
        self.repo_lookup = repo_lookup
        self.settings = settings or get_settings()
        self.plans = PlanRepo(session)
        self.tasks = TaskRepo(session)
        self.projects = ProjectRepo(session)
        self.events = event_repo or EventRepo(session)
        self.costs = cost_repo or CostRepo(session)
        self.auto_approve = auto_approve

    # ------------------------------------------------------------------ create
    async def create_plan(self, project_id: str) -> Plan:
        """Run the planner and persist a proposed (or rejected) plan version."""
        project = await self._get_project(project_id)
        spec = self._spec_from_project(project)
        profile, ctx = await self._analysis(project, spec)
        proposed = await self.planner.generate_plan(spec, profile, ctx)
        tasks, issues = self._normalise(proposed.tasks, spec)
        errors = [issue for issue in issues if issue.is_error]

        version = await self.plans.next_version(project.id)
        plan_id = new_plan_id()
        plan_model = PlanModel(
            id=plan_id,
            project_id=project.id,
            version=version,
            status=_REJECTED if errors else _PROPOSED,
            generated_by=proposed.generated_by,
            planner_version=proposed.planner_version,
            source_revision=proposed.source_revision or profile.revision,
            rationale=proposed.rationale,
            assumptions_json=list(proposed.assumptions),
            risks_json=list(proposed.risks),
            issues_json=[issue.model_dump(mode="json") for issue in issues],
        )
        await self.plans.create(plan_model)
        await self._persist_tasks(project.id, plan_id, tasks)
        await self._record_inference_cost(project.id, plan_id)
        await self._event(
            project.id,
            "plan.created",
            payload={
                "plan_id": plan_id,
                "version": version,
                "errors": len(errors),
                "warnings": len(issues) - len(errors),
            },
        )
        plan = self._to_plan(plan_model, tasks)

        if errors:
            await self._event(
                project.id,
                "plan.rejected",
                payload={"plan_id": plan_id, "errors": len(errors)},
            )
            return plan
        if project.mode == ProjectMode.AUTONOMOUS.value and self.auto_approve:
            return await self.approve_plan(plan_id, approver="autonomous")
        return plan

    # ----------------------------------------------------------------- approve
    async def approve_plan(self, plan_id: str, approver: str) -> Plan:
        """Activate a proposed plan after a final validation and policy check."""
        plan_model = await self._get_plan(plan_id)
        if plan_model.status != _PROPOSED:
            raise PlanStateError(
                f"plan {plan_id} is {plan_model.status}, only proposed plans can be approved"
            )
        project = await self._get_project(plan_model.project_id)
        spec = self._spec_from_project(project)
        tasks = await self._load_plan_drafts(plan_id)
        issues = validate_graph(tasks, limits=self._limits(spec))
        errors = [issue for issue in issues if issue.is_error]
        if errors:
            plan_model.status = _REJECTED
            plan_model.issues_json = [issue.model_dump(mode="json") for issue in issues]
            await self.session.flush()
            raise PlanValidationError(
                f"plan {plan_id} has {len(errors)} validation error(s) and cannot be activated"
            )
        self._assert_policy_compliant(tasks)

        previous = await self.plans.get_active_for_project(project.id)
        if previous is not None and previous.id != plan_model.id:
            previous.status = _SUPERSEDED
            await self._event(
                project.id,
                "plan.superseded",
                payload={"plan_id": previous.id, "replaced_by": plan_id},
            )

        plan_model.status = _ACTIVE
        plan_model.approved_by = approver
        plan_model.approved_at = utcnow()
        plan_model.issues_json = [issue.model_dump(mode="json") for issue in issues]
        project.active_plan_id = plan_model.id
        await self.session.flush()
        await self._event(
            project.id,
            "plan.approved",
            payload={"plan_id": plan_id, "approver": approver, "version": plan_model.version},
        )
        return self._to_plan(plan_model, tasks)

    # ------------------------------------------------------------------ reject
    async def reject_plan(self, plan_id: str, reason: str) -> Plan:
        """Reject a proposed plan with a human-readable reason."""
        plan_model = await self._get_plan(plan_id)
        if plan_model.status != _PROPOSED:
            raise PlanStateError(
                f"plan {plan_id} is {plan_model.status}, only proposed plans can be rejected"
            )
        plan_model.status = _REJECTED
        plan_model.rejection_reason = reason
        await self.session.flush()
        await self._event(
            plan_model.project_id,
            "plan.rejected",
            payload={"plan_id": plan_id, "reason": reason},
        )
        return self._to_plan(plan_model, await self._load_plan_drafts(plan_id))

    # -------------------------------------------------------------------- edit
    async def edit_plan(self, plan_id: str, patch: PlanPatch) -> Plan:
        """Apply a pre-approval edit and re-validate the graph."""
        plan_model = await self._get_plan(plan_id)
        if plan_model.status not in {_PROPOSED, _REJECTED}:
            raise PlanStateError(
                f"plan {plan_id} is {plan_model.status}, only pre-approval plans can be edited"
            )
        project = await self._get_project(plan_model.project_id)
        spec = self._spec_from_project(project)
        current = await self._load_plan_drafts(plan_id)
        edited = self._apply_patch(current, patch)
        tasks, issues = self._normalise(edited, spec)
        errors = [issue for issue in issues if issue.is_error]

        plan_model.rationale = patch.rationale or plan_model.rationale
        if patch.assumptions is not None:
            plan_model.assumptions_json = list(patch.assumptions)
        if patch.risks is not None:
            plan_model.risks_json = list(patch.risks)
        plan_model.issues_json = [issue.model_dump(mode="json") for issue in issues]
        plan_model.status = _REJECTED if errors else _PROPOSED
        if not errors:
            plan_model.rejection_reason = None

        await self.tasks.delete_for_plan(plan_id)
        await self._persist_tasks(project.id, plan_id, tasks)
        await self.session.flush()
        await self._event(
            project.id,
            "plan.edited",
            payload={
                "plan_id": plan_id,
                "errors": len(errors),
                "tasks": len(tasks),
            },
        )
        return self._to_plan(plan_model, tasks)

    # ------------------------------------------------------------------ replan
    async def replan(self, project_id: str, failures: list[FailureRecord]) -> Plan:
        """Create a new plan version that preserves COMPLETED tasks."""
        project = await self._get_project(project_id)
        spec = self._spec_from_project(project)
        active = await self.plans.get_active_for_project(project_id)
        if active is None:
            active = await self.plans.latest_for_project(project_id)
        if active is None:
            raise PlanStateError(f"project {project_id} has no plan to replan")

        if project.mode == ProjectMode.AUTONOMOUS.value:
            replans = await self.plans.count_replans(project_id)
            if replans >= self.settings.planner.max_replans:
                raise PlanLimitError(
                    f"project {project_id} already used {replans} replans "
                    f"(limit {self.settings.planner.max_replans})"
                )

        old_drafts = await self._load_plan_drafts(active.id)
        old_models = await self.tasks.list_for_plan(active.id)
        completed_models = {model.key: model for model in old_models if model.status == _COMPLETED}
        completed_drafts = [draft for draft in old_drafts if draft.key in completed_models]
        old_plan = self._to_plan(active, old_drafts)
        proposed = await self.planner.replan(project_id, old_plan, failures)
        new_tasks = [task for task in proposed.tasks if task.key not in completed_models]
        combined = [*completed_drafts, *new_tasks]
        combined_keys = {task.key for task in combined}
        # Completed tasks no longer need edges to tasks that were replaced.
        combined = [
            task.model_copy(
                update={"depends_on": [dep for dep in task.depends_on if dep in combined_keys]}
            )
            if task.key in completed_models
            else task
            for task in combined
        ]

        tasks, issues = self._normalise(combined, spec)
        errors = [issue for issue in issues if issue.is_error]
        version = await self.plans.next_version(project_id)
        plan_id = new_plan_id()
        plan_model = PlanModel(
            id=plan_id,
            project_id=project_id,
            version=version,
            status=_REJECTED if errors else _PROPOSED,
            generated_by=f"replan:{proposed.generated_by}",
            planner_version=proposed.planner_version,
            source_revision=proposed.source_revision,
            rationale=proposed.rationale,
            assumptions_json=list(proposed.assumptions),
            risks_json=list(proposed.risks),
            issues_json=[issue.model_dump(mode="json") for issue in issues],
        )
        await self.plans.create(plan_model)
        await self._persist_tasks(project_id, plan_id, tasks, completed=completed_models)
        await self._record_inference_cost(project_id, plan_id)
        await self._event(
            project_id,
            "plan.replanned",
            payload={
                "plan_id": plan_id,
                "version": version,
                "from_plan": active.id,
                "preserved": len(completed_models),
                "errors": len(errors),
            },
        )
        plan = self._to_plan(plan_model, tasks)
        if errors:
            return plan
        if project.mode == ProjectMode.AUTONOMOUS.value and self.auto_approve:
            return await self.approve_plan(plan_id, approver="autonomous")
        return plan

    # -------------------------------------------------------------------- diff
    @staticmethod
    def plan_diff(old: Plan | ProposedPlan, new: Plan | ProposedPlan) -> PlanDiff:
        """Return a structured diff between two plans."""
        old_tasks = {task.key: task for task in old.tasks}
        new_tasks = {task.key: task for task in new.tasks}
        added = sorted(new_tasks.keys() - old_tasks.keys())
        removed = sorted(old_tasks.keys() - new_tasks.keys())
        common = sorted(old_tasks.keys() & new_tasks.keys())
        changed: list[str] = []
        dependency_changes: list[str] = []
        diffs: list[TaskDiff] = [
            *[TaskDiff(key=key, change="added") for key in added],
            *[TaskDiff(key=key, change="removed") for key in removed],
        ]
        for key in common:
            old_dump = old_tasks[key].model_dump(mode="json")
            new_dump = new_tasks[key].model_dump(mode="json")
            fields = sorted(
                field
                for field in old_dump.keys() | new_dump.keys()
                if old_dump.get(field) != new_dump.get(field)
            )
            if fields:
                changed.append(key)
                diffs.append(TaskDiff(key=key, change="changed", fields=fields))
            if old_tasks[key].depends_on != new_tasks[key].depends_on:
                dependency_changes.append(key)
        return PlanDiff(
            added=added,
            removed=removed,
            changed=changed,
            dependency_changes=dependency_changes,
            task_diffs=diffs,
            rationale_changed=old.rationale != new.rationale,
            version_from=old.version if isinstance(old, Plan) else None,
            version_to=new.version if isinstance(new, Plan) else None,
        )

    # --------------------------------------------------------------- internals
    async def _get_project(self, project_id: str) -> ProjectModel:
        project = await self.projects.get(project_id)
        if project is None:
            raise ProjectNotFoundError(f"project {project_id} not found")
        return project

    async def _get_plan(self, plan_id: str) -> PlanModel:
        plan = await self.plans.get(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan {plan_id} not found")
        return plan

    def _spec_from_project(self, project: ProjectModel) -> ProjectSpec:
        try:
            if project.spec_json:
                return ProjectSpec.model_validate_json(project.spec_json)
            if project.spec_yaml:
                return load_spec(project.spec_yaml)
        except (ValidationError, ValueError) as exc:
            raise ConfigError(f"project {project.id} has an invalid spec: {exc}") from exc
        raise ConfigError(f"project {project.id} has no stored spec")

    def _repo_handle(self, project: ProjectModel) -> RepoHandle:
        if self.repo_lookup is not None:
            return self.repo_lookup(project.id)
        source: RepoSource | None = None
        if project.repo_source_json:
            source = RepoSource.model_validate_json(project.repo_source_json)
        path = project.repo_path or (source.path if source is not None else None)
        url = source.url if source is not None else None
        if path is None and url is None:
            raise ConfigError(f"project {project.id} has no repository source")
        return RepoHandle(
            project_id=project.id,
            path=path or "",
            url=url,
            ref=source.ref if source is not None else project.main_branch,
            main_branch=project.main_branch,
            staging_branch=project.staging_branch,
        )

    async def _analysis(
        self, project: ProjectModel, spec: ProjectSpec
    ) -> tuple[RepoProfile, ProjectContext]:
        repo = self._repo_handle(project)
        revision = repo.base_commit or repo.ref or project.main_branch
        if self.context_manager is None:
            constraints = [
                *spec.constraints.languages,
                *spec.constraints.restricted_paths,
            ]
            return (
                RepoProfile(name=spec.name, revision=revision),
                ProjectContext(
                    goals=list(spec.goals),
                    requirements=list(spec.requirements),
                    constraints=constraints,
                ),
            )
        profile = await self.context_manager.analyze_repo(repo, revision)
        ctx = await self.context_manager.get_project_context(project.id, revision)
        return profile, ctx

    def _limits(self, spec: ProjectSpec) -> ValidationLimits:
        return validation_limits_from_settings(
            self.settings,
            baseline_restricted_paths=spec.constraints.restricted_paths,
            policy_max_resources=self.policy.max_resources(),
        )

    def _normalise(
        self, tasks: list[TaskDraft], spec: ProjectSpec
    ) -> tuple[list[TaskDraft], list[GraphIssue]]:
        result = normalize_tasks(
            tasks,
            policy=self.policy,
            planner_config=self.settings.planner,
            project_checks=spec.validation.commands,
            baseline_restricted_paths=spec.constraints.restricted_paths,
            max_retries=spec.limits.max_retries,
            max_timeout_seconds=spec.limits.task_timeout_seconds,
        )
        issues = [*result.issues, *validate_graph(result.tasks, limits=self._limits(spec))]
        return result.tasks, issues

    def _assert_policy_compliant(self, tasks: list[TaskDraft]) -> None:
        maximum = self.policy.max_resources()
        for task in tasks:
            resources = task.resources
            if (
                resources.cpu_cores > maximum.cpu_cores
                or resources.memory_mb > maximum.memory_mb
                or resources.disk_mb > maximum.disk_mb
                or resources.pids_limit > maximum.pids_limit
            ):
                raise PolicyViolation(
                    f"task {task.key} requests resources above the security policy"
                )
            if task.timeout_seconds > self.settings.planner.max_task_timeout_seconds:
                raise PolicyViolation(f"task {task.key} timeout exceeds the security policy")
            if task.max_retries > self.settings.retries.default_max_retries:
                raise PolicyViolation(f"task {task.key} retries exceed the security policy")

    async def _persist_tasks(
        self,
        project_id: str,
        plan_id: str,
        tasks: list[TaskDraft],
        *,
        completed: dict[str, TaskModel] | None = None,
    ) -> None:
        ids: dict[str, str] = {}
        for draft in tasks:
            old = completed.get(draft.key) if completed is not None else None
            task_id = new_task_id()
            ids[draft.key] = task_id
            await self.tasks.create(
                TaskModel(
                    id=task_id,
                    project_id=project_id,
                    plan_id=plan_id,
                    key=draft.key,
                    title=draft.title,
                    description=draft.description,
                    instructions=draft.instructions,
                    priority=draft.priority,
                    status=_COMPLETED if old is not None else _PENDING,
                    success_criteria_json=list(draft.success_criteria),
                    checks_json=[check.model_dump(mode="json") for check in draft.checks],
                    expected_files_json=list(draft.expected_files),
                    restricted_files_json=list(draft.restricted_files),
                    resource_profile_json=draft.resources.model_dump(mode="json"),
                    timeout_seconds=draft.timeout_seconds,
                    max_retries=draft.max_retries,
                    attempt_count=old.attempt_count if old is not None else 0,
                    result_json=old.result_json if old is not None else None,
                    version=1,
                )
            )
        for draft in tasks:
            for dependency in draft.depends_on:
                dependency_id = ids.get(dependency)
                if dependency_id is not None:
                    await self.tasks.add_dependency(ids[draft.key], dependency_id)

    async def _load_plan_drafts(self, plan_id: str) -> list[TaskDraft]:
        models = await self.tasks.list_for_plan(plan_id)
        id_to_key = {model.id: model.key for model in models}
        drafts: list[TaskDraft] = []
        for model in models:
            dependency_ids = await self.tasks.dependencies_for_task(model.id)
            dependencies = [
                id_to_key[dependency_id]
                for dependency_id in dependency_ids
                if dependency_id in id_to_key
            ]
            drafts.append(self._draft_from_model(model, dependencies))
        try:
            order = topological_order(drafts)
        except GraphCycleError:
            return drafts
        by_key = {draft.key: draft for draft in drafts}
        return [by_key[key] for key in order]

    def _draft_from_model(self, model: TaskModel, dependencies: list[str]) -> TaskDraft:
        checks = [CheckSpec.model_validate(item) for item in (model.checks_json or [])]
        resources = ResourceProfile.model_validate(model.resource_profile_json or {})
        return TaskDraft(
            key=model.key,
            title=model.title,
            description=model.description or "",
            instructions=model.instructions or "",
            depends_on=dependencies,
            priority=model.priority,
            success_criteria=list(model.success_criteria_json or []),
            checks=checks,
            expected_files=list(model.expected_files_json or []),
            restricted_files=list(model.restricted_files_json or []),
            resources=resources,
            timeout_seconds=model.timeout_seconds,
            max_retries=model.max_retries,
        )

    def _to_plan(self, model: PlanModel, tasks: list[TaskDraft]) -> Plan:
        issues = [GraphIssue.model_validate(item) for item in (model.issues_json or [])]
        return Plan(
            id=model.id,
            project_id=model.project_id,
            version=model.version,
            status=PlanStatus(model.status),
            tasks=tasks,
            rationale=model.rationale or "",
            assumptions=list(model.assumptions_json or []),
            risks=list(model.risks_json or []),
            generated_by=model.generated_by or "",
            planner_version=model.planner_version or "",
            source_revision=model.source_revision or "",
            issues=issues,
            approved_by=model.approved_by,
            approved_at=model.approved_at,
            rejection_reason=model.rejection_reason,
        )

    def _apply_patch(self, current: list[TaskDraft], patch: PlanPatch) -> list[TaskDraft]:
        if patch.tasks is not None:
            return list(patch.tasks)
        removed = set(patch.remove_keys)
        result = [task for task in current if task.key not in removed]
        index = {task.key: position for position, task in enumerate(result)}
        for task in patch.upsert_tasks:
            position = index.get(task.key)
            if position is None:
                index[task.key] = len(result)
                result.append(task)
            else:
                result[position] = task
        return result

    async def _record_inference_cost(self, project_id: str, plan_id: str) -> None:
        usage = getattr(self.planner, "last_token_usage", None)
        if usage is None:
            return
        await self.costs.add_cost(
            CostEntryModel(
                project_id=project_id,
                kind="inference",
                amount_usd=0.0,
                units_json=usage.model_dump(mode="json"),
                note=f"planning:{plan_id}",
            )
        )

    async def _event(
        self,
        project_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.events.add(
            EventModel(
                project_id=project_id,
                task_id=task_id,
                type=event_type,
                payload_json=payload or {},
                at=utcnow(),
            )
        )


__all__ = ["PlanService"]
