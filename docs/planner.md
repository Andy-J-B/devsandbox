# Planner & task graph (plan 06)

The planner turns a `ProjectSpec` plus repository analysis into a **validated
task graph**. It is deliberately split into pure functions, deterministic
planners, an optional LLM planner, and a database-backed lifecycle service.

Modules:

- `src/devsandbox/planner/models.py` — `TaskDraft`, `ProposedPlan`, `Plan`,
  `GraphIssue`, `PlanPatch`, `ValidationLimits`, LLM request/response types
- `src/devsandbox/planner/validate.py` — pure `validate_graph`, `topological_order`,
  `critical_path`, `ready_set`
- `src/devsandbox/planner/normalize.py` — project defaults and policy clamps
- `src/devsandbox/planner/templates.py` — deterministic `TemplatePlanner`
- `src/devsandbox/planner/llm.py` — optional `LLMPlanner` + OpenAI-compatible client
- `src/devsandbox/planner/service.py` — plan lifecycle (`create`, `approve`, `edit`,
  `reject`, `replan`, `plan_diff`)
- `src/devsandbox/planner/render.py` — Markdown / JSON presentation

`planner/types.py` and `planner/interfaces.py` keep the original plan-01 import
paths working (`TaskDraft`, `validate_graph`, `Planner`).

## Graph validation

`validate_graph(tasks, limits=...)` is pure and returns errors and warnings:

| Rule | Severity |
| --- | --- |
| unique, slug-shaped, non-empty keys | error |
| non-empty title and instructions | error |
| at least one success criterion | error |
| at least one **required** check | error |
| non-empty check name/command, positive check timeout | error |
| dependencies exist and are not self-references | error |
| no cycles (the cycle path is reported) | error |
| max task count and max dependency depth | error |
| priority in range, timeout in range, retries in range | error |
| resource values > 0 | error |
| resources above policy ceiling | warning (clamped later) |
| expected files do not overlap restricted files | error |
| baseline restricted paths are present | error |
| isolated tasks | warning |
| expected-file overlap between unrelated tasks | warning |
| duplicate dependency entries | warning |

Project-level defaults are applied by `normalize_tasks` before validation:
tasks that omit checks receive the spec's `validation.commands`, and every task
inherits the spec's baseline `restricted_paths`.

`topological_order` raises `GraphCycleError` on a cycle. `critical_path` uses
task timeouts as durations. `ready_set(tasks, completed)` returns tasks whose
dependencies are all complete.

## Template planner

`TemplatePlanner` needs no model access and is the default for the MVP and E2E
tests:

- one task per spec goal, named `goal-<n>-<slug>`
- goals run in list order (each depends on the previous one)
- optional leading `baseline` task and trailing `final-verification` task
- checks come from the spec, then from detected test/build commands, then from
  a `git diff --check` sanity fallback
- instructions include requirements, constraints, conventions, and detected
  commands; text is passed through secret redaction

```python
from devsandbox.planner import TemplatePlanner

planner = TemplatePlanner()
proposed = await planner.generate_plan(spec, profile, project_context)
```

## Optional LLM planner

`LLMPlanner` implements the same `Planner` protocol and talks to any
OpenAI-compatible `/chat/completions` endpoint through the small `LLMClient`
protocol. It:

1. sends the spec, selected repo profile, and project context (no secrets)
2. parses strict JSON into `ProposedPlan`
3. applies project defaults and policy clamps
4. runs `validate_graph`
5. reprompts at most twice with validation feedback
6. falls back to `TemplatePlanner` and records `generated_by="template-fallback"`

Hostile output (too many tasks, cycles, unknown fields such as a network
request, oversized resources) is rejected, clamped, or replaced by the
deterministic fallback. Token usage is exposed as `last_token_usage` and the
service records it in the `cost_entries` inference ledger.

## Plan lifecycle

`PlanService` is constructed with an async session, a `Planner`, a
`SecurityPolicy`, and optionally a `ContextManager` and repository lookup:

```python
service = PlanService(session, planner, policy, context_manager=contexts)
plan = await service.create_plan(project_id)
await service.approve_plan(plan.id, approver="alice")
```

- `create_plan` runs the planner, applies defaults/clamps, validates, and
  persists the plan, tasks, and dependency edges. Plans with validation errors
  are persisted with status `rejected` so the UI can show the issues; they can
  never become active.
- `approve_plan` only accepts `proposed` plans, re-validates, enforces the
  security policy, supersedes the previous active plan, updates
  `projects.active_plan_id`, and emits audit events. Autonomous projects
  auto-approve only after the same checks.
- `edit_plan` accepts a `PlanPatch` (full replacement or key-based upsert/remove)
  and re-validates before approval.
- `reject_plan` records the reason.
- `replan` preserves `COMPLETED` tasks (keys, attempt counts, and result JSON),
  regenerates the remaining work with failure context, re-applies policy, and
  is bounded by `planner.max_replans` in autonomous mode.
- `plan_diff` reports added/removed/changed tasks and dependency changes.

The service flushes but does not commit; callers own the transaction boundary
(see `db.session.unit_of_work`).

## Configuration

New `planner` settings (all `DEVSANDBOX_PLANNER__*` overridable):

| Setting | Default |
| --- | --- |
| `max_tasks` | 100 |
| `max_depth` | 12 |
| `max_task_timeout_seconds` | 7200 |
| `default_task_timeout_seconds` | 1800 |
| `max_replans` | 2 |
| `min_priority` / `max_priority` | 0 / 100 |
| `max_expected_files` | 200 |
| `planner_version` | 1.0 |

Resource, timeout, and retry values can only be narrowed by the spec/planner;
`SecurityPolicy` is the ceiling.

## Presentation

`render_plan_markdown(plan)` includes rationale, assumptions, risks, an ASCII
dependency forest, per-task success criteria, checks, resources, and validation
issues. `render_plan_json(plan)` emits stable pretty JSON for the API/CLI.

## Testing

```bash
pytest tests/unit/test_validate_graph.py \
       tests/unit/test_planner_template.py \
       tests/unit/test_planner_llm.py \
       tests/unit/test_planner_render.py \
       tests/integration/test_planner_service.py -q
```

Coverage includes table-driven rules for every validation case, seeded random
DAGs, injected cycles, golden-file template plans, LLM repair/fallback and
hostile output, and service approval/supersession/replan/policy behaviour.

## Out of scope

Task execution, scheduling, and validation execution belong to plans 07–09.
