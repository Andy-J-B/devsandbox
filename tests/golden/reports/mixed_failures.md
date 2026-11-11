# DevSandbox report — Report mixed_failures

| Field | Value |
| --- | --- |
| Project id | proj-mixed_failures |
| Mode | supervised |
| Status | failed |
| Spec hash | spec-hash-mixed_failures |
| Repository | /tmp/demo-repo |
| Main branch | main |
| Base commit | base-mixed_failures |
| Staging branch | devsandbox/proj-mixed_failures/staging |
| Staging head | s1 |
| Plan | v1 (plan-mixed_failures) |
| Started | 2026-01-01T10:00:00+00:00 |
| Finished | 2026-01-01T11:30:00+00:00 |
| Generated | 2026-01-01T12:00:00+00:00 |

## Summary

| Total | Completed | Failed | Blocked | Cancelled | Pending | Ready | Running | Retry wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | 1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 |

## Tasks

### build — Task build

- **Status:** `completed`
- **Attempts:** 1 / retries 1
- **Duration:** 960.0s
- **Files changed:** src/app.py
- **Commits:** `c1`

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:02:00+00:00 | 960.0s | c1 | passed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | passed | yes | 1.5s | 0 | — |

### flaky — Task flaky

- **Status:** `failed`
- **Attempts:** 1 / retries 1
- **Duration:** 1080.0s
- **Failure:** `timeout` — agent exceeded the time limit

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | failed | timeout | 2026-01-01T10:41:00+00:00 | 1080.0s | — | error |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | error | yes | 1.5s | 1 | not run: agent timed out |

### test — Task test

- **Status:** `failed`
- **Attempts:** 2 / retries 1
- **Duration:** 2160.0s
- **Commits:** `c2`, `c3`
- **Failure:** `validation_failed` — tests failed after retries
- **Log pointer:** `logs/att-mixed_failures-test-2/stdout.log`

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:22:00+00:00 | 1080.0s | c2 | failed |
| 2 | failed | exited_ok | 2026-01-01T10:41:00+00:00 | 1080.0s | c3 | failed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | failed | yes | 1.5s | 1 | 2 tests failed |

## Integration history

| Task | Attempt | Status | Before | After | Conflicts | Detail |
| --- | --- | --- | --- | --- | --- | --- |
| task-mixed_failures-build | att-mixed_failures-build-1 | integrated | — | s1 | — | — |

## Test results

| Check | Passed | Failed | Timeout | Error | Skipped | Total |
| --- | --- | --- | --- | --- | --- | --- |
| pytest | 1 | 2 | 0 | 1 | 0 | 4 |

## Costs

| Infra USD | Inference USD | Total USD | Tokens | Requests | Token budget left | Request budget left |
| --- | --- | --- | --- | --- | --- | --- |
| 0.3000 | 0.1200 | 0.4200 | 2000 | 20 | 3000 | 80 |

## Resource / limit events

| Type | Task | Attempt | At | Detail |
| --- | --- | --- | --- | --- |
| attempt.timeout | task-mixed_failures-flaky | — | 2026-01-01T10:59:00+00:00 | attempt 1 timed out |

## Outstanding work

**Unresolved failures**

- flaky: agent exceeded the time limit
- test: tests failed after retries

**Suggested actions**

- inspect task flaky logs and retry or replan
- inspect task test logs and retry or replan

## Staging branch

- `Fetch the staging branch from the project mirror (run in your source repository): git fetch "$DEVSANDBOX_DATA_ROOT/repos/proj-mixed_failures/repo.git" "devsandbox/proj-mixed_failures/staging:refs/heads/devsandbox/proj-mixed_failures/staging"`
- `Review integrated work: git log main..devsandbox/proj-mixed_failures/staging`
- `Review the diff: git diff main..devsandbox/proj-mixed_failures/staging`
- `DevSandbox never merges into the main branch automatically.`
- `Merge manually when satisfied: git checkout main && git merge --no-ff devsandbox/proj-mixed_failures/staging`
