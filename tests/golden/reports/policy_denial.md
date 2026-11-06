# DevSandbox report — Report policy_denial

| Field | Value |
| --- | --- |
| Project id | proj-policy_denial |
| Mode | supervised |
| Status | failed |
| Spec hash | spec-hash-policy_denial |
| Repository | /tmp/demo-repo |
| Main branch | main |
| Base commit | base-policy_denial |
| Staging branch | devsandbox/proj-policy_denial/staging |
| Staging head | — |
| Plan | v1 (plan-policy_denial) |
| Started | 2026-01-01T10:00:00+00:00 |
| Finished | 2026-01-01T11:30:00+00:00 |
| Generated | 2026-01-01T12:00:00+00:00 |

## Summary

| Total | Completed | Failed | Blocked | Cancelled | Pending | Ready | Running | Retry wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

## Tasks

### guarded — Task guarded

- **Status:** `failed`
- **Attempts:** 1 / retries 1
- **Duration:** 2160.0s
- **Commits:** `c1`
- **Failure:** `tampering` — agent modified a restricted test file

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:02:00+00:00 | 2160.0s | c1 | failed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | failed | yes | 1.5s | 1 | — |

## Integration history

_No integrations._

## Test results

| Check | Passed | Failed | Timeout | Error | Skipped | Total |
| --- | --- | --- | --- | --- | --- | --- |
| pytest | 0 | 1 | 0 | 0 | 0 | 1 |

## Costs

| Infra USD | Inference USD | Total USD | Tokens | Requests | Token budget left | Request budget left |
| --- | --- | --- | --- | --- | --- | --- |
| 0.0200 | 0.0100 | 0.0300 | 120 | 2 | 4880 | 98 |

## Resource / limit events

| Type | Task | Attempt | At | Detail |
| --- | --- | --- | --- | --- |
| POLICY_DENIED | — | — | 2026-01-01T10:20:00+00:00 | main-branch merge is disabled by policy |

## Outstanding work

**Unresolved failures**

- guarded: agent modified a restricted test file

**Suggested actions**

- inspect task guarded logs and retry or replan

**Warnings**

- baseline validation: baseline pytest failed
- policy denial: main-branch merge is disabled by policy
- test tampering signal in task guarded: agent modified tests/test_app.py

## Staging branch

- `Review integrated work: git log main..devsandbox/proj-policy_denial/staging`
- `Review the diff: git diff main..devsandbox/proj-policy_denial/staging`
- `DevSandbox never merges into the main branch automatically.`
- `Merge manually when satisfied: git checkout main && git merge --no-ff devsandbox/proj-policy_denial/staging`
