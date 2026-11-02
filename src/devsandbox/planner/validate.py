"""Pure task-graph validation and graph algorithms.

Everything in this module is side-effect free and independent of the database,
the filesystem, and any model provider.  This makes the rules easy to unit test
and safe to run before a plan is persisted.
"""

from __future__ import annotations

import fnmatch
import heapq
import re
from collections.abc import Iterable

from ..common.types import ResourceProfile
from ..config.settings import DevSandboxSettings
from .models import GraphIssue, GraphSeverity, TaskDraft, ValidationLimits

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_KEY_LENGTH = 128


class GraphCycleError(ValueError):
    """Raised when a graph algorithm requires an acyclic graph but finds a cycle."""

    def __init__(self, path: list[str]) -> None:
        self.path = path
        super().__init__(f"task graph contains a cycle: {' -> '.join(path)}")


def _error(
    code: str,
    issue: str,
    *,
    task_key: str | None = None,
    path: Iterable[str] = (),
) -> GraphIssue:
    return GraphIssue(
        code=code,
        severity=GraphSeverity.ERROR,
        task_key=task_key,
        issue=issue,
        path=list(path),
    )


def _warning(
    code: str,
    issue: str,
    *,
    task_key: str | None = None,
    path: Iterable[str] = (),
) -> GraphIssue:
    return GraphIssue(
        code=code,
        severity=GraphSeverity.WARNING,
        task_key=task_key,
        issue=issue,
        path=list(path),
    )


def _task_map(tasks: list[TaskDraft]) -> dict[str, TaskDraft]:
    """Return the first task for each key (duplicates are reported separately)."""
    mapping: dict[str, TaskDraft] = {}
    for task in tasks:
        mapping.setdefault(task.key, task)
    return mapping


def _find_cycles(tasks_by_key: dict[str, TaskDraft]) -> list[list[str]]:
    """Return dependency cycles as paths (each cycle once)."""
    colour: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(key: str) -> None:
        colour[key] = 1
        stack.append(key)
        for dep in tasks_by_key[key].depends_on:
            if dep == key or dep not in tasks_by_key:
                continue
            state = colour.get(dep, 0)
            if state == 1:
                start = stack.index(dep)
                cycle = [*stack[start:], dep]
                signature = tuple(sorted(cycle[:-1]))
                if signature not in seen:
                    seen.add(signature)
                    cycles.append(cycle)
            elif state == 0:
                visit(dep)
        stack.pop()
        colour[key] = 2

    for key in tasks_by_key:
        if colour.get(key, 0) == 0:
            visit(key)
    return cycles


def _longest_chain(tasks_by_key: dict[str, TaskDraft]) -> list[str]:
    """Return the longest dependency chain (root first)."""
    memo: dict[str, list[str]] = {}
    visiting: set[str] = set()

    def chain(key: str) -> list[str]:
        if key in memo:
            return memo[key]
        if key in visiting:
            return []
        visiting.add(key)
        best: list[str] = []
        for dep in tasks_by_key[key].depends_on:
            if dep == key or dep not in tasks_by_key:
                continue
            candidate = chain(dep)
            if len(candidate) > len(best):
                best = candidate
        visiting.discard(key)
        result = [*best, key]
        memo[key] = result
        return result

    longest: list[str] = []
    for key in tasks_by_key:
        candidate = chain(key)
        if len(candidate) > len(longest):
            longest = candidate
    return longest


def _depends_transitively(key: str, target: str, tasks_by_key: dict[str, TaskDraft]) -> bool:
    """Return whether ``key`` depends on ``target`` (directly or transitively)."""
    stack = list(tasks_by_key[key].depends_on)
    seen: set[str] = set()
    while stack:
        dep = stack.pop()
        if dep == target:
            return True
        if dep in seen or dep not in tasks_by_key:
            continue
        seen.add(dep)
        stack.extend(tasks_by_key[dep].depends_on)
    return False


def _normalise_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def _paths_overlap(left: str, right: str) -> bool:
    """Return whether two expected-file patterns could touch the same file."""
    a = _normalise_path(left)
    b = _normalise_path(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if any(char in a for char in "*?[") or any(char in b for char in "*?["):
        return fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)
    return a.startswith(f"{b}/") or b.startswith(f"{a}/")


def topological_order(tasks: list[TaskDraft]) -> list[str]:
    """Return task keys in dependency order.

    Raises :class:`GraphCycleError` when the graph contains a cycle.
    """
    tasks_by_key = _task_map(tasks)
    index = {key: position for position, key in enumerate(tasks_by_key)}
    indegree: dict[str, int] = {}
    dependents: dict[str, list[str]] = {key: [] for key in tasks_by_key}

    for key, task in tasks_by_key.items():
        deps = {dep for dep in task.depends_on if dep in tasks_by_key and dep != key}
        indegree[key] = len(deps)
        for dep in deps:
            dependents[dep].append(key)

    heap: list[tuple[int, str]] = [
        (index[key], key) for key, degree in indegree.items() if degree == 0
    ]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        _, key = heapq.heappop(heap)
        order.append(key)
        for dependent in sorted(dependents[key], key=index.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, (index[dependent], dependent))

    if len(order) != len(tasks_by_key):
        cycles = _find_cycles(tasks_by_key)
        raise GraphCycleError(cycles[0] if cycles else [])
    return order


def critical_path(tasks: list[TaskDraft]) -> list[str]:
    """Return the dependency path with the largest total timeout.

    Task ``timeout_seconds`` is used as the duration estimate.  The returned
    path is ordered from the earliest dependency to the final task.
    """
    tasks_by_key = _task_map(tasks)
    order = topological_order(tasks)
    best: dict[str, tuple[int, list[str]]] = {}
    for key in order:
        task = tasks_by_key[key]
        predecessor_duration = 0
        predecessor_path: list[str] = []
        for dep in task.depends_on:
            if dep in best and best[dep][0] > predecessor_duration:
                predecessor_duration, predecessor_path = best[dep]
        best[key] = (
            predecessor_duration + max(task.timeout_seconds, 0),
            [*predecessor_path, key],
        )
    if not best:
        return []
    return max(best.values(), key=lambda item: item[0])[1]


def ready_set(tasks: list[TaskDraft], completed: Iterable[str]) -> list[str]:
    """Return keys whose dependencies are all in ``completed``."""
    done = set(completed)
    return [
        task.key
        for task in tasks
        if task.key not in done and all(dep in done for dep in task.depends_on)
    ]


def validate_graph(
    tasks: list[TaskDraft],
    *,
    limits: ValidationLimits | None = None,
) -> list[GraphIssue]:
    """Validate a task graph and return all errors and warnings.

    The function is pure: callers that want project-level defaults (for example
    injecting the spec's validation commands into tasks that omitted checks)
    should normalise the tasks first via
    :func:`devsandbox.planner.normalize.normalize_tasks`.
    """
    effective = limits or ValidationLimits()
    issues: list[GraphIssue] = []
    tasks_by_key = _task_map(tasks)

    if len(tasks) > effective.max_tasks:
        issues.append(
            _error(
                "graph.max_tasks",
                f"graph has {len(tasks)} tasks, exceeding the limit of {effective.max_tasks}",
            )
        )

    # Per-task field checks.
    seen_keys: set[str] = set()
    for task in tasks:
        if not task.key:
            issues.append(_error("task.empty_key", "task key must not be empty", task_key=""))
        elif len(task.key) > _MAX_KEY_LENGTH or not _KEY_RE.fullmatch(task.key):
            issues.append(
                _error(
                    "task.invalid_key",
                    f"task key '{task.key}' must be a slug of at most {_MAX_KEY_LENGTH} characters",
                    task_key=task.key,
                )
            )
        if task.key in seen_keys:
            issues.append(
                _error("task.duplicate_key", f"duplicate task key '{task.key}'", task_key=task.key)
            )
        seen_keys.add(task.key)

        if not task.title.strip():
            issues.append(
                _error("task.empty_title", "task title must not be empty", task_key=task.key)
            )
        if effective.require_instructions and not task.instructions.strip():
            issues.append(
                _error(
                    "task.empty_instructions",
                    "task instructions must not be empty",
                    task_key=task.key,
                )
            )
        criteria = [item for item in task.success_criteria if item.strip()]
        if effective.require_success_criteria and not criteria:
            issues.append(
                _error(
                    "task.no_success_criteria",
                    "task must define at least one success criterion",
                    task_key=task.key,
                )
            )
        if len(task.expected_files) > effective.max_expected_files:
            issues.append(
                _warning(
                    "task.too_many_expected_files",
                    f"task lists {len(task.expected_files)} expected files "
                    f"(limit {effective.max_expected_files})",
                    task_key=task.key,
                )
            )

        required_checks = [check for check in task.checks if check.required]
        if effective.require_required_check and not required_checks:
            issues.append(
                _error(
                    "task.no_required_check",
                    "task must define at least one required validation check",
                    task_key=task.key,
                )
            )
        for check in task.checks:
            if not check.name.strip():
                issues.append(
                    _error("check.empty_name", "check name must not be empty", task_key=task.key)
                )
            if not check.run.strip():
                issues.append(
                    _error(
                        "check.empty_command",
                        f"check '{check.name}' has an empty command",
                        task_key=task.key,
                    )
                )
            if check.timeout_seconds <= 0:
                issues.append(
                    _error(
                        "check.invalid_timeout",
                        f"check '{check.name}' timeout must be > 0",
                        task_key=task.key,
                    )
                )

        if not effective.min_priority <= task.priority <= effective.max_priority:
            issues.append(
                _error(
                    "task.priority_out_of_range",
                    f"priority {task.priority} is outside "
                    f"[{effective.min_priority}, {effective.max_priority}]",
                    task_key=task.key,
                )
            )
        if task.timeout_seconds <= 0:
            issues.append(
                _error("task.invalid_timeout", "timeout_seconds must be > 0", task_key=task.key)
            )
        elif task.timeout_seconds > effective.max_timeout_seconds:
            issues.append(
                _error(
                    "task.timeout_too_large",
                    f"timeout {task.timeout_seconds}s exceeds the limit of "
                    f"{effective.max_timeout_seconds}s",
                    task_key=task.key,
                )
            )
        if task.max_retries < 0:
            issues.append(
                _error("task.invalid_retries", "max_retries must be >= 0", task_key=task.key)
            )
        elif task.max_retries > effective.max_retries:
            issues.append(
                _error(
                    "task.retries_too_large",
                    f"max_retries {task.max_retries} exceeds the limit of {effective.max_retries}",
                    task_key=task.key,
                )
            )

        issues.extend(_resource_issues(task, effective.policy_max_resources))

        restricted = set(task.restricted_files)
        overlap = sorted(expected for expected in task.expected_files if expected in restricted)
        if overlap:
            issues.append(
                _error(
                    "task.expected_is_restricted",
                    "expected files overlap restricted files: " + ", ".join(overlap),
                    task_key=task.key,
                )
            )
        missing_baseline = [
            path for path in effective.baseline_restricted_paths if path not in restricted
        ]
        if missing_baseline:
            issues.append(
                _error(
                    "task.missing_baseline_restrictions",
                    "task restricted_files must include project baseline paths: "
                    + ", ".join(missing_baseline),
                    task_key=task.key,
                )
            )

        duplicates = {dep for dep in task.depends_on if task.depends_on.count(dep) > 1}
        for dep in sorted(duplicates):
            issues.append(
                _warning(
                    "task.duplicate_dependency",
                    f"dependency '{dep}' is listed more than once",
                    task_key=task.key,
                )
            )

    # Dependency checks.
    for task in tasks:
        for dep in task.depends_on:
            if dep == task.key:
                issues.append(
                    _error(
                        "task.self_dependency",
                        "task depends on itself",
                        task_key=task.key,
                        path=[task.key, task.key],
                    )
                )
            elif dep not in tasks_by_key:
                issues.append(
                    _error(
                        "task.unknown_dependency",
                        f"unknown dependency '{dep}'",
                        task_key=task.key,
                    )
                )

    # Cycles.
    for cycle in _find_cycles(tasks_by_key):
        issues.append(
            _error(
                "graph.cycle",
                "dependency cycle: " + " -> ".join(cycle),
                task_key=cycle[0],
                path=cycle,
            )
        )

    # Depth.
    chain = _longest_chain(tasks_by_key)
    if len(chain) > effective.max_depth:
        issues.append(
            _error(
                "graph.too_deep",
                f"dependency depth {len(chain)} exceeds the limit of {effective.max_depth}",
                path=chain,
            )
        )

    # Isolated nodes are usually a planning mistake, but not always fatal.
    if len(tasks_by_key) > 1:
        for key, task in tasks_by_key.items():
            has_dependents = any(key in other.depends_on for other in tasks_by_key.values())
            if not task.depends_on and not has_dependents:
                issues.append(
                    _warning(
                        "graph.isolated_task",
                        "task is isolated (no dependencies and no dependents)",
                        task_key=key,
                    )
                )

    # Potential integration conflicts between unrelated tasks.
    keys = list(tasks_by_key)
    for position, left_key in enumerate(keys):
        left = tasks_by_key[left_key]
        for right_key in keys[position + 1 :]:
            right = tasks_by_key[right_key]
            if not any(
                _paths_overlap(left_path, right_path)
                for left_path in left.expected_files
                for right_path in right.expected_files
            ):
                continue
            if _depends_transitively(left_key, right_key, tasks_by_key) or (
                _depends_transitively(right_key, left_key, tasks_by_key)
            ):
                continue
            issues.append(
                _warning(
                    "graph.file_overlap",
                    f"expected files overlap with '{right_key}' but the tasks have no "
                    "dependency path (possible integration conflict)",
                    task_key=left_key,
                )
            )

    return issues


def _resource_issues(task: TaskDraft, policy_max: ResourceProfile | None) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    resources = task.resources
    if (
        resources.cpu_cores <= 0
        or resources.memory_mb <= 0
        or resources.disk_mb <= 0
        or resources.pids_limit <= 0
    ):
        issues.append(
            _error(
                "task.invalid_resources",
                "resource values must all be > 0",
                task_key=task.key,
            )
        )
    if policy_max is None:
        return issues

    over: list[str] = []
    if resources.cpu_cores > policy_max.cpu_cores:
        over.append(f"cpu_cores {resources.cpu_cores} > {policy_max.cpu_cores}")
    if resources.memory_mb > policy_max.memory_mb:
        over.append(f"memory_mb {resources.memory_mb} > {policy_max.memory_mb}")
    if resources.disk_mb > policy_max.disk_mb:
        over.append(f"disk_mb {resources.disk_mb} > {policy_max.disk_mb}")
    if resources.pids_limit > policy_max.pids_limit:
        over.append(f"pids_limit {resources.pids_limit} > {policy_max.pids_limit}")
    if over:
        issues.append(
            _warning(
                "policy.resources_clamped",
                "resources exceed policy and will be clamped: " + ", ".join(over),
                task_key=task.key,
            )
        )
    return issues


def validation_limits_from_settings(
    settings: DevSandboxSettings,
    *,
    baseline_restricted_paths: Iterable[str] = (),
    policy_max_resources: ResourceProfile | None = None,
) -> ValidationLimits:
    """Build validation limits from application settings."""
    planner = settings.planner
    return ValidationLimits(
        max_tasks=planner.max_tasks,
        max_depth=planner.max_depth,
        max_timeout_seconds=planner.max_task_timeout_seconds,
        max_retries=settings.retries.default_max_retries,
        min_priority=planner.min_priority,
        max_priority=planner.max_priority,
        max_expected_files=planner.max_expected_files,
        baseline_restricted_paths=list(baseline_restricted_paths),
        policy_max_resources=policy_max_resources,
    )


__all__ = [
    "GraphCycleError",
    "critical_path",
    "ready_set",
    "topological_order",
    "validate_graph",
    "validation_limits_from_settings",
]
