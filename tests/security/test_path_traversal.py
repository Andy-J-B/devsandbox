"""Path-traversal safety for log and artifact reads."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from devsandbox.api.errors import ApiError
from devsandbox.api.routes.tasks import _log_path, _read_tail, _within

pytestmark = pytest.mark.security


def _attempt(attempt_id: str = "att-1", agent_log_path: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=attempt_id, agent_log_path=agent_log_path)


def test_standard_log_paths_stay_inside_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    assert _log_path(str(data_root), _attempt(), "stdout").name == "stdout.log"
    assert _log_path(str(data_root), _attempt(), "stderr").name == "stderr.log"


def test_agent_log_path_outside_data_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        _log_path(str(tmp_path / "data"), _attempt(agent_log_path="/etc/passwd"), "agent")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "unsafe_log_path"


def test_agent_log_path_traversal_is_rejected(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    traversal = str(data_root / "logs" / "att-1" / ".." / ".." / ".." / "etc" / "passwd")
    with pytest.raises(ApiError, match="outside"):
        _log_path(str(data_root), _attempt(agent_log_path=traversal), "agent")


def test_symlink_inside_logs_cannot_escape(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host-only\n", encoding="utf-8")
    log_dir = data_root / "logs" / "att-1"
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.log").symlink_to(secret)

    with pytest.raises(ApiError, match="outside"):
        _log_path(str(data_root), _attempt(), "stdout")


def test_legitimate_agent_log_inside_workspaces_is_allowed(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    agent_log = data_root / "workspaces" / "p1" / "att-1" / "agent.log"
    agent_log.parent.mkdir(parents=True)
    agent_log.write_text("ok\n", encoding="utf-8")

    resolved = _log_path(str(data_root), _attempt(agent_log_path=str(agent_log)), "agent")

    assert resolved == agent_log


def test_read_tail_enforces_byte_and_line_caps(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    log.write_text("\n".join(f"line-{i:04d}" for i in range(10_000)), encoding="utf-8")

    content, lines, truncated = _read_tail(log, max_bytes=200, max_lines=5)

    assert truncated is True
    assert lines <= 5
    assert len(content.encode("utf-8")) <= 200


def test_missing_log_is_a_typed_404(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as excinfo:
        _read_tail(tmp_path / "missing.log", max_bytes=100, max_lines=10)
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "log_not_found"


def test_within_rejects_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    sibling = tmp_path / "logs-evil" / "x.log"
    root.mkdir()
    assert _within(root / "x.log", root) is True
    assert _within(sibling, root) is False
