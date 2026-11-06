# Reporting (plan 10)

`build_report(project_id)` returns a frozen `ProjectReport` assembled from the
database only. It is read-only (no writes, no git commands that mutate state),
deterministic for a given DB state and clock, and every free-text field passes
through the shared redaction filter before it is populated.

```python
from devsandbox.reporting.builder import build_report

report = await build_report(project_id, session_factory, settings=settings)
```

The API and CLI render it via:

- `reporting/render.py` — `render_json(report)`, `render_markdown(report)`,
  `render_html(report)` (HTML-lite)
- `GET /v1/projects/{id}/report?format=json|markdown|html`
- `devsandbox report PROJECT [--format ...] [--out FILE]`

## Contents (spec §13 item 13)

| Section | Fields |
| --- | --- |
| Header | project id/name, spec hash, mode, overall status, repo source, main branch, base commit, staging branch and head, plan id/version, started/finished, generated-at |
| Counts | total, completed, failed, blocked, cancelled, pending, ready, running, retry-wait, other |
| Per task | title, status, attempt count/budget, duration, commits (sha + subject), files changed, validation checks with pass/fail and test counts, failure (kind/summary/detail/log pointer), blocked-by chain/reason, result |
| Per attempt | outcome, status, exit status, start/finish/duration, base/head commit, commits, files, checks, validation status/summary, failure, token usage, peak memory, log pointer |
| Integration history | staging before → after, status, conflicting files, detail, timestamp |
| Test results | checks aggregated across attempts: passed/failed/timeout/error/skipped/total |
| Costs | infra USD, inference USD, total, token/request totals, remaining token/request budget (shown separately, 00 §8.10) |
| Resource/limit events | pressure deferrals, circuit breakers, policy denials, timeouts, OOMs |
| Outstanding work | unresolved failures, blocked tasks with reasons, suggested next actions, warnings (baseline failures, tampering signals, policy denials) |
| Staging instructions | read-only commands to review the diff, list commits, and merge manually; DevSandbox never merges to `main` |

## Determinism and redaction

- Ordering is stable (tasks by creation/key, attempts by number, events by
  timestamp/id), so golden tests compare exact JSON and Markdown.
- `generated_at` uses the injected clock; the golden suite pins it.
- Secrets registered with `common.redaction` and secret-looking values are
  replaced with `[REDACTED]` in every string, including failure details and
  Markdown/HTML output.
- `tests/integration/test_reporting_golden.py` covers all-success, mixed
  failures with retries, blocked chains, integration conflicts, and policy
  denial/tampering. Golden files live in `tests/golden/reports/`; regenerate
  deliberately with `UPDATE_GOLDEN=1`.

The report reflects the current DB state at call time. It does not include
unpersisted engine memory, and it does not assert that the staging branch is
currently mergeable — that is the reviewer's manual step.
