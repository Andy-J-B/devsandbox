# DevSandbox report — Report blocked_chain

| Field | Value |
| --- | --- |
| Project id | proj-blocked_chain |
| Mode | supervised |
| Status | failed |
| Spec hash | spec-hash-blocked_chain |
| Repository | /tmp/demo-repo |
| Main branch | main |
| Base commit | base-blocked_chain |
| Staging branch | devsandbox/proj-blocked_chain/staging |
| Staging head | — |
| Plan | v1 (plan-blocked_chain) |
| Started | 2026-01-01T10:00:00+00:00 |
| Finished | 2026-01-01T11:30:00+00:00 |
| Generated | 2026-01-01T12:00:00+00:00 |

## Summary

| Total | Completed | Failed | Blocked | Cancelled | Pending | Ready | Running | Retry wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | 0 | 1 | 1 | 0 | 1 | 0 | 0 | 0 |

## Tasks

### base — Task base

- **Status:** `failed`
- **Attempts:** 1 / retries 1
- **Duration:** 960.0s
- **Failure:** `validation_failed` — base checks failed

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:02:00+00:00 | 960.0s | — | failed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | failed | yes | 1.5s | 1 | — |

### last — Task last

- **Status:** `pending`
- **Attempts:** 0 / retries 1
- **Duration:** 0.0s
- **Depends on:** task-blocked_chain-next

**Attempts**

_No attempts._

### next — Task next

- **Status:** `blocked`
- **Attempts:** 0 / retries 1
- **Duration:** 240.0s
- **Depends on:** task-blocked_chain-base
- **Blocked reason:** dependency base failed

**Attempts**

_No attempts._

## Integration history

_No integrations._

## Test results

| Check | Passed | Failed | Timeout | Error | Skipped | Total |
| --- | --- | --- | --- | --- | --- | --- |
| pytest | 0 | 1 | 0 | 0 | 0 | 1 |

## Costs

| Infra USD | Inference USD | Total USD | Tokens | Requests | Token budget left | Request budget left |
| --- | --- | --- | --- | --- | --- | --- |
| 0.1000 | 0.0500 | 0.1500 | 500 | 5 | 4500 | 95 |

## Resource / limit events

_No resource or limit events._

## Outstanding work

**Unresolved failures**

- base: base checks failed

**Blocked tasks**

- next: dependency base failed

**Suggested actions**

- inspect task base logs and retry or replan
- resolve the failed dependency of task next

## Staging branch

- `Fetch the staging branch from the project mirror (run in your source repository): git fetch $DEVSANDBOX_DATA_ROOT/repos/proj-blocked_chain/repo.git devsandbox/proj-blocked_chain/staging:refs/heads/devsandbox/proj-blocked_chain/staging`
- `Review integrated work: git log main..devsandbox/proj-blocked_chain/staging`
- `Review the diff: git diff main..devsandbox/proj-blocked_chain/staging`
- `DevSandbox never merges into the main branch automatically.`
- `Merge manually when satisfied: git checkout main && git merge --no-ff devsandbox/proj-blocked_chain/staging`
