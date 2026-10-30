"""Unit tests for the secret redaction utilities."""

from __future__ import annotations

from devsandbox.common.redaction import Redactor, redact


def test_redact_registered_secret() -> None:
    redactor = Redactor()
    secret = "canary-secret-value-abc123xyz"
    redactor.register_secret(secret)
    text = f"Authorization header was {secret}"
    redacted = redactor.redact_text(text)
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_redact_github_token_pattern() -> None:
    text = "git remote set with ghp_abcdefghijklmnopqrstuvwx"
    redacted = redact(text)
    assert "ghp_" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_bearer_token_pattern() -> None:
    redacted = redact("Authorization: Bearer abcdefghijklmnop1234abcd")
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_private_key_block() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nabcdefghijklmnopqrstuvwxyz\n-----END RSA PRIVATE KEY-----"
    )
    redacted = redact(text)
    assert "PRIVATE KEY" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_nested_dict() -> None:
    value = {"api_key": "sk-ant-api03-supersecretvalue123", "model": "opencode"}
    redacted = redact(value)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["model"] == "opencode"


def test_redact_secret_str() -> None:
    from pydantic import SecretStr

    assert redact(SecretStr("topsecretvalue123")) == "[REDACTED]"
