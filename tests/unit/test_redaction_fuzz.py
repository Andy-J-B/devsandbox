"""Deterministic fuzz tests for the shared secret redactor.

The tests generate many small payloads with a seeded PRNG instead of relying on
a property-testing dependency.  They cover registration, nested container
types, mapping keys, overlapping secrets, and the common credential formats
recognised by regex.
"""

from __future__ import annotations

import random
import string
from typing import Any

import pytest
from pydantic import SecretStr

from devsandbox.common.redaction import Redactor

_ALPHABET = string.ascii_letters + string.digits + "_-"
_TOKEN_ALPHABET = string.ascii_letters + string.digits
_HEX_UPPER = string.digits + "ABCDEF"
_SEEDS = range(40)


def _random_text(rng: random.Random, length: int) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def _secret(rng: random.Random) -> str:
    return "svc_" + _random_text(rng, rng.randint(12, 40))


def _contains(value: Any, needle: str) -> bool:
    """Return whether ``needle`` survives anywhere in a nested value."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, bytes):
        return needle in value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains(item, needle) for item in value)
    if isinstance(value, SecretStr):
        return needle in value.get_secret_value()
    return False


def _string_with_secret(rng: random.Random, secret: str) -> str:
    prefix = _random_text(rng, rng.randint(0, 24))
    suffix = _random_text(rng, rng.randint(0, 24))
    separator = rng.choice(["", " ", "\n", "\t", '"', "'", ":", "="])
    return f"{prefix}{separator}{secret}{separator}{suffix}"


def _payload(rng: random.Random, secret: str, depth: int = 0) -> Any:
    """Build a random nested payload that always contains ``secret``."""
    if depth >= 3:
        return _string_with_secret(rng, secret)

    kind = rng.randrange(8)
    if kind == 0:
        return _string_with_secret(rng, secret)
    if kind == 1:
        return SecretStr(secret)
    if kind == 2:
        return _string_with_secret(rng, secret).encode("utf-8")
    if kind == 3:
        return [
            _string_with_secret(rng, secret),
            _payload(rng, secret, depth + 1),
            rng.randint(0, 100),
        ]
    if kind == 4:
        return (
            _string_with_secret(rng, secret),
            _payload(rng, secret, depth + 1),
        )
    if kind == 5:
        return {_string_with_secret(rng, secret), _random_text(rng, 8)}
    if kind == 6:
        return frozenset({_string_with_secret(rng, secret), _random_text(rng, 8)})
    # Include the secret in a mapping key as well as a value.
    return {
        f"key-{_random_text(rng, 6)}-{secret}": _payload(rng, secret, depth + 1),
        "plain": _random_text(rng, 20),
        "nested": {"value": _string_with_secret(rng, secret)},
    }


@pytest.mark.parametrize("seed", _SEEDS)
def test_registered_secret_never_survives_random_nested_payload(seed: int) -> None:
    rng = random.Random(seed)
    secret = _secret(rng)
    redactor = Redactor()
    redactor.register_secret(secret)
    payload = {"canary": _string_with_secret(rng, secret), "body": _payload(rng, secret)}

    cleaned = redactor.redact(payload)

    assert not _contains(cleaned, secret)
    assert "[REDACTED]" in repr(cleaned)
    assert "\x00" not in repr(cleaned)


@pytest.mark.parametrize("seed", _SEEDS)
def test_overlapping_registered_secrets_are_all_removed(seed: int) -> None:
    rng = random.Random(seed)
    base = _secret(rng)
    secrets = [base, f"{base}-extra", f"prefix-{base}"]
    redactor = Redactor()
    for value in secrets:
        redactor.register_secret(value)

    text = "\n".join(
        [
            f"one: {secrets[0]}",
            f"two: {secrets[1]}",
            f"three: {secrets[2]}",
            f"four: {secrets[0]} and {secrets[1]}",
        ]
    )
    cleaned = redactor.redact_text(text)

    assert all(value not in cleaned for value in secrets)
    assert cleaned.count("[REDACTED]") == 5


@pytest.mark.parametrize("seed", _SEEDS)
def test_common_credential_formats_are_removed_from_random_context(seed: int) -> None:
    rng = random.Random(seed)
    tail = lambda length, alphabet=_TOKEN_ALPHABET: "".join(  # noqa: E731
        rng.choice(alphabet) for _ in range(length)
    )
    tokens = [
        f"ghp_{tail(24)}",
        f"sk-{tail(28)}",
        f"AKIA{tail(16, _HEX_UPPER)}",
        f"glpat-{tail(24)}",
        f"xoxb-{tail(16)}",
        f"AIza{tail(35)}",
        f"hf_{tail(32)}",
        f"npm_{tail(32)}",
        f"eyJ{tail(12)}.{tail(12)}.{tail(12)}",
    ]

    for token in tokens:
        context = f"{_random_text(rng, 20)} {token} {_random_text(rng, 20)}"
        cleaned = Redactor().redact_text(context)
        assert token not in cleaned
        assert "[REDACTED]" in cleaned


def test_empty_registered_secret_is_ignored() -> None:
    redactor = Redactor()
    redactor.register_secret("")
    assert redactor.redact_text("nothing to hide") == "nothing to hide"
