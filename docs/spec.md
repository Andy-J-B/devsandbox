# DevSandbox Specification

Project specs are YAML documents describing a coding task for an autonomous
coding agent. The full schema is implemented in
`src/devsandbox/spec/models.py` (`ProjectSpec`) with the loader in
`src/devsandbox/spec/loader.py`.

## Example

```yaml
version: 1
name: todo-cli
description: "CLI todo app"
mode: supervised            # or autonomous
repo:
  path: ./existing-repo     # xor url
  url: https://github.com/org/repo.git
  ref: main
goals:
  - "Add `todo done <id>` command"
requirements:               # free text; used by planner
  - "Persist to JSON file"
constraints:
  languages: [python]
  restricted_paths: ["LICENSE", ".github/**"]
validation:
  commands:
    - name: tests
      run: "pytest -q"
      required: true
      timeout_seconds: 300
    - name: lint
      run: "ruff check ."
      required: false
limits:                     # may only NARROW global policy
  max_retries: 2
  task_timeout_seconds: 1800
  max_concurrency: 1
agent:
  runtime: opencode
  model: null               # falls back to settings
```

## Rules

- Exactly one of `repo.path` / `repo.url` must be set.
- Unknown keys are rejected (`extra="forbid"`).
- Secret-looking keys (e.g. `api_key`, `token`, `password`) must use a
  `secret_ref` name; inline secret literals are rejected.
- Limits may only narrow the global policy (enforced via
  `SecurityPolicy.max_retries`).
- `spec_hash` is the sha256 hex digest of the canonical JSON of the spec.
- Errors carry line / field information via `SpecError`.
