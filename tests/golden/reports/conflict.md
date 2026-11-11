# DevSandbox report — Report conflict

| Field | Value |
| --- | --- |
| Project id | proj-conflict |
| Mode | supervised |
| Status | failed |
| Spec hash | spec-hash-conflict |
| Repository | /tmp/demo-repo |
| Main branch | main |
| Base commit | base-conflict |
| Staging branch | devsandbox/proj-conflict/staging |
| Staging head | — |
| Plan | v1 (plan-conflict) |
| Started | 2026-01-01T10:00:00+00:00 |
| Finished | 2026-01-01T11:30:00+00:00 |
| Generated | 2026-01-01T12:00:00+00:00 |

## Summary

| Total | Completed | Failed | Blocked | Cancelled | Pending | Ready | Running | Retry wait |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

## Tasks

### merge — Task merge

- **Status:** `failed`
- **Attempts:** 1 / retries 1
- **Duration:** 1560.0s
- **Files changed:** src/app.py
- **Commits:** `c1`
- **Failure:** `git_conflict` — conflict in src/app.py

**Attempts**

| # | Status | Outcome | Started | Duration | Head | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | completed | exited_ok | 2026-01-01T10:02:00+00:00 | 1560.0s | c1 | passed |

**Validation checks**


| Check | Status | Required | Duration | Exit | Summary |
| --- | --- | --- | --- | --- | --- |
| pytest | passed | yes | 1.5s | 0 | — |

## Integration history

| Task | Attempt | Status | Before | After | Conflicts | Detail |
| --- | --- | --- | --- | --- | --- | --- |
| task-conflict-merge | att-conflict-merge-1 | conflict | s1 | — | src/app.py | merge conflict; staging unchanged |

## Test results

| Check | Passed | Failed | Timeout | Error | Skipped | Total |
| --- | --- | --- | --- | --- | --- | --- |
| pytest | 1 | 0 | 0 | 0 | 0 | 1 |

## Costs

| Infra USD | Inference USD | Total USD | Tokens | Requests | Token budget left | Request budget left |
| --- | --- | --- | --- | --- | --- | --- |
| 0.0500 | 0.0300 | 0.0800 | 300 | 3 | 4700 | 97 |

## Resource / limit events

_No resource or limit events._

## Outstanding work

**Unresolved failures**

- merge: conflict in src/app.py

**Suggested actions**

- inspect task merge logs and retry or replan
- rebase task merge onto the current staging head

## Staging branch

- `Fetch the staging branch from the project mirror (run in your source repository): git fetch "$DEVSANDBOX_DATA_ROOT/repos/proj-conflict/repo.git" "devsandbox/proj-conflict/staging:refs/heads/devsandbox/proj-conflict/staging"`
- `Review integrated work: git log main..devsandbox/proj-conflict/staging`
- `Review the diff: git diff main..devsandbox/proj-conflict/staging`
- `DevSandbox never merges into the main branch automatically.`
- `Merge manually when satisfied: git checkout main && git merge --no-ff devsandbox/proj-conflict/staging`
