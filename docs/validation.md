# Independent validation (plan 09)

Validation is the only path to task completion. The orchestrator never trusts
an agent's exit status, summary, or claim of success: it independently runs the
orchestrator-approved checks in a **fresh validation container** and persists
the structured result.

Implementation lives in `src/devsandbox/validation/`.

## Flow

`ValidatorImpl.validate_task(task, workspace, change_set)`:

1. **Host-side pre-checks** (no agent code executed):
   - non-empty change set (unless `allow_no_changes`)
   - restricted paths via `GitManager.check_restricted_paths`
   - changed-file / changed-line caps
   - forbidden binaries/extensions
   - expected-file coverage (`warn` by default)
2. **Copy the attempt workspace** to a disposable scratch directory under
   `<data_root>/validation/<run-id>/workspace`. The agent workspace is never
   mutated; the scratch copy is removed afterwards.
3. **Run checks sequentially** in new containers via `CheckRunner`.
4. **Test-tampering heuristics** (deleted tests, added skips, removed
   assertions, test/CI config edits).
5. **Baseline attribution**: failures already present on the recorded baseline
   are reported as `pre_existing_failure` instead of blaming the agent.
6. Return a `ValidationReport` with per-check results, evidence, warnings,
   failure taxonomy, tampering signal, baseline comparison, and a retry-ready
   `summary_for_retry`.

Any infrastructure problem (sandbox unavailable, checkout failed, invalid
check configuration) produces `status="error"`, which the orchestrator
classifies as `INFRA_ERROR` and does not charge to the task's retry budget.

## Check specification

`CheckSpec` (in `common/types.py`) is orchestrator input, never agent input:

```yaml
checks:
  - name: tests
    run: "pytest -q"            # shell string, or an argv list
    required: true
    timeout_seconds: 300
    working_dir: "."            # relative to /workspace
    env: {PYTHONWARNINGS: error}
    expected_exit_codes: [0]
    success_regex: null
    failure_regex: "\\d+ failed"
    junit_xml_path: reports/junit.xml
    kind: test                  # test | lint | build | setup | other
    network: none               # none | allowlist | full
    allowed_hosts: []
```

- Shell strings run with `/bin/sh -lc`; argv lists are executed directly.
- `working_dir` must stay inside `/workspace`; absolute paths and `..` are
  rejected as infrastructure errors.
- `expected_exit_codes` defaults to `[0]`. `success_regex` must match and
  `failure_regex` must not match the captured output.
- JUnit XML is parsed for `tests_total/failed/skipped/errors` when present.
- Checks with `required: false` only warn unless
  `validation.fail_on_optional_failure` is enabled.
- Required checks are validated before any container starts: a report with no
  checks, or no required check when `require_required_check=true`, is an
  infrastructure error rather than a pass.

## Sandbox guarantees

Each check gets a new container from the configured validator image:

- the agent's container is never reused;
- `secret_env={}` — validation containers receive **no agent secrets**;
- network is `none` by default; a check may request `allowlist`/`full`, but the
  policy can only narrow it (and `allowlist` is rejected by the MVP sandbox);
- resources are clamped by `SecurityPolicy.effective_resources`;
- stdout/stderr are redacted, size-capped, and only tails are retained;
- the container is destroyed in a `finally` block.

Dependency installation is not attempted at check time by default. Use a
pre-baked validator image or a check that declares a `setup` kind and network
access **only when policy permits it**; otherwise checks run offline.

## Failure taxonomy

`FailureKind`: `TEST_FAILURE`, `LINT_FAILURE`, `BUILD_FAILURE`, `TIMEOUT`,
`NO_CHANGES`, `RESTRICTED_PATH`, `TAMPERING`, `SIZE_LIMIT`, `INFRA_ERROR`
(plus `FORBIDDEN_FILE`, `EXPECTED_FILES`, `UNKNOWN` for pre-check detail).

`ValidationReport.status` is `passed | failed | error`. `CheckResult.status` is
`passed | failed | timeout | error | skipped`. `passed` is kept in sync with
`status` for backwards compatibility.

## Tampering heuristics

`detect_tampering` flags:

- deleted test files;
- added skip/disable markers (`pytest.skip`, `@pytest.mark.skip`,
  `@pytest.mark.xfail`, `test.skip`, `@Ignore`, `#[ignore]`, ...);
- net removal of assertions above `assert_removal_threshold`;
- edits to test configuration (`pytest.ini`, `conftest.py`, `pyproject.toml`
  with pytest config, Jest/Vitest/Karma/Mocha config, CI workflows).

`validation.tamper_mode` controls the outcome: `fail` (default), `warn`, or
`ignore`.

## Baseline checks

`ValidatorImpl.run_baseline(repo, revision, checks)` (or the function wrapper
`validation.baseline.run_baseline`) checks out an unmodified revision in a
scratch workspace and records `BaselineReport.failed_checks`. The orchestrator
calls it once after planning/approval.

Task validation compares its failed checks against the baseline:

- only pre-existing failures → report `passed`, `pre_existing_failure=true`,
  `blame="baseline"`;
- any newly failing check → report `failed`, `blame="agent"`;
- baseline itself errored → warning, no attribution (the task failure stands).

## Integration validation

`validate_integration(repo, staging_commit, checks)` checks out the candidate
staging commit (including custom candidate refs) in a scratch clone, runs the
project-level checks, and leaves staging untouched. The orchestrator commits or
aborts the candidate based on the report.

When the candidate fails, the validator runs the same checks on the previous
staging head once and attributes blame:

- candidate-only failure → `blame="agent"`, retry with the integration failure;
- failure also present on the previous head → `passed` with
  `pre_existing_failure=true` and `blame="baseline"`;
- previous head unavailable → comparison `checked=false`, candidate failure
  stands;
- baseline infrastructure error → warning, candidate failure stands.

## Persistence and retries

Reports are JSON-serialisable and persisted by the orchestrator in
`attempts.validation_report_json`. `summary_for_retry` is a bounded, structured
text block (failed checks, kinds, exit codes, log tails, tampering findings,
warnings) suitable for the next attempt's prompt. Infrastructure-error reports
tell the next attempt to retry without code changes.

## Testing

- `tests/unit/test_validation_runner.py` — pass/fail/timeout/error mapping,
  output truncation/redaction, JUnit parsing, network policy, argv checks.
- `tests/unit/test_validation_prechecks.py` — no-change, restricted path, size,
  forbidden binary, expected files, missing workspace.
- `tests/unit/test_validation_tampering.py` — deleted/skipped tests, removed
  assertions, config/CI edits.
- `tests/unit/test_validation_validator.py` — lying agent, optional checks,
  infrastructure errors, tampering, scratch isolation, baseline attribution.
- `tests/unit/test_validation_integration.py` — candidate rejection, baseline
  comparison, staging immutability, checkout errors.
- `tests/unit/test_validation_workspace.py` — real git checkout and scratch
  copies.
- `tests/integration/test_validation_docker.py` — opt-in real Docker tests
  (pass/fail, network blocked, write escape, timeout):
  `DEVSANDBOX_RUN_VALIDATION_INTEGRATION=1 pytest -m docker`.

The validator fake (`tests/fakes/validation.py`) supports keyed outcomes,
queues, callbacks, and explicit pass/fail/error helpers.
