# DevSandbox report — Report all_success

| Field | Value |
| --- | --- |
| Project id | proj-all_success |
| Mode | autonomous |
| Status | completed |
| Spec hash | spec-hash-all_success |
| Repository | /tmp/demo-repo |
| Main branch | main |
| Base commit | base-all_success |
| Staging branch | devsandbox/proj-all_success/staging |
| Staging head | s2 |
| Plan | v1 (plan-all_success) |
| Started | 2026-01-01T10:00:00+00:00 |
| Finished | 2026-01-01T11:30:00+00:00 |
| Generated | 2026-01-01T12:00:00+00:00 |

## Summary

| Total | Completed | Failed | Blocked | Cancelled | Pending | Ready | Running | Retry wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Tasks

### build — Task build

- **Status:** `completed`
- **Attempts:** 1 / retries 1
- **Duration:** 960.0s
- **Files changed:** src/app.py
- **Commits:** `c1` build the app

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:02:00+00:00 | 960.0s | c1 | passed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | passed | yes | 1.5s | 0 | — |

### test — Task test

- **Status:** `completed`
- **Attempts:** 1 / retries 1
- **Duration:** 960.0s
- **Files changed:** tests/test_app.py
- **Commits:** `c2` add tests

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:22:00+00:00 | 960.0s | c2 | passed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | passed | yes | 1.5s | 0 | — |

## Integration history

| Task | Attempt | Status | Before | After | Conflicts | Detail |
| --- | --- | --- | --- | --- | --- | --- |
| task-all_success-build | att-all_success-build-1 | integrated | — | s1 | — | — |
| task-all_success-test | att-all_success-test-1 | integrated | s1 | s2 | — | — |

## Test results

| Check | Passed | Failed | Timeout | Error | Skipped | Total |
| --- | --- | --- | --- | --- | --- | --- |
| pytest | 2 | 0 | 0 | 0 | 0 | 2 |

## Costs

| Infra USD | Inference USD | Total USD | Tokens | Requests | Token budget left | Request budget left |
| --- | --- | --- | --- | --- | --- | --- |
| 0.2500 | 0.1000 | 0.3500 | 1000 | 10 | 4000 | 90 |

## Resource / limit events

_No resource or limit events._

## Outstanding work

_No outstanding work._

## Staging branch

- `Fetch the staging branch from the project mirror (run in your source repository): git fetch $DEVSANDBOX_DATA_ROOT/repos/proj-all_success/repo.git devsandbox/proj-all_success/staging:refs/heads/devsandbox/proj-all_success/staging`
- `Review integrated work: git log main..devsandbox/proj-all_success/staging`
- `Review the diff: git diff main..devsandbox/proj-all_success/staging`
- `DevSandbox never merges into the main branch automatically.`
- `Merge manually when satisfied: git checkout main && git merge --no-ff devsandbox/proj-all_success/staging`
