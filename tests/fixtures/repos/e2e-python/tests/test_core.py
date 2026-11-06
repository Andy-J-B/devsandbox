"""Baseline tests for the E2E fixture repository."""

from calc import add, subtract


def test_add() -> None:
    assert add(2, 3) == 5


def test_subtract() -> None:
    assert subtract(5, 2) == 3
