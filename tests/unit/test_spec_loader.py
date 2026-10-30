"""Unit tests for the YAML project spec loader."""

from __future__ import annotations

import pytest

from devsandbox.common.errors import SpecError
from devsandbox.spec.models import load_spec

EXAMPLE_SPEC = """
version: 1
name: todo-cli
description: "CLI todo app"
mode: supervised
repo:
  path: ./existing-repo
goals:
  - "Add `todo done <id>` command"
requirements:
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
limits:
  max_retries: 2
  task_timeout_seconds: 1800
  max_concurrency: 1
agent:
  runtime: opencode
  model: null
"""


def test_load_spec_accepts_example() -> None:
    spec = load_spec(EXAMPLE_SPEC)
    assert spec.name == "todo-cli"
    assert spec.version == 1
    assert spec.mode == "supervised"


def test_spec_hash_is_stable() -> None:
    first = load_spec(EXAMPLE_SPEC)
    second = load_spec(EXAMPLE_SPEC)
    assert first.spec_hash == second.spec_hash
    assert len(first.spec_hash) == 64


def test_load_spec_rejects_both_repo_sources() -> None:
    bad = EXAMPLE_SPEC.replace(
        "  path: ./existing-repo", "  path: ./existing-repo\n  url: https://github.com/org/repo.git"
    )
    with pytest.raises(SpecError):
        load_spec(bad)


def test_load_spec_rejects_unknown_keys() -> None:
    bad = EXAMPLE_SPEC + "bogus_key: 1\n"
    with pytest.raises(SpecError):
        load_spec(bad)


def test_load_spec_rejects_secret_literal() -> None:
    bad = EXAMPLE_SPEC + "api_key: supersecretvalue123\n"
    with pytest.raises(SpecError):
        load_spec(bad)


def test_load_spec_rejects_negative_limits() -> None:
    bad = EXAMPLE_SPEC.replace("  max_retries: 2", "  max_retries: -2")
    with pytest.raises(SpecError):
        load_spec(bad)


def test_load_spec_rejects_invalid_yaml() -> None:
    with pytest.raises(SpecError):
        load_spec("version: 1\nname: [unclosed")
