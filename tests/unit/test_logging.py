"""Unit tests for logging configuration."""

from __future__ import annotations

from devsandbox.common.logging import get_logger


def test_get_logger_returns_bound_logger() -> None:
    logger = get_logger("devsandbox.test")
    assert logger is not None
