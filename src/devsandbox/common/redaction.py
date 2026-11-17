"""Secret redaction: registered secrets plus common token regex patterns."""

from __future__ import annotations

import re
from typing import Any

from pydantic import SecretStr

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._]{8,}")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token"
    r"|client[_-]?secret|secret|password|passwd|pwd|token)\s*[:=]\s*(\S+)",
)
_GITHUB_PAT_RE = re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_GITLAB_PAT_RE = re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")
_HUGGINGFACE_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")
_NPM_TOKEN_RE = re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")

_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PRIVATE_KEY_RE,
    _BEARER_RE,
    _SECRET_VALUE_RE,
    _GITHUB_PAT_RE,
    _OPENAI_KEY_RE,
    _AWS_KEY_RE,
    _GITLAB_PAT_RE,
    _SLACK_TOKEN_RE,
    _GOOGLE_API_KEY_RE,
    _HUGGINGFACE_TOKEN_RE,
    _NPM_TOKEN_RE,
    _JWT_RE,
)

# Secrets are temporarily replaced with a NUL byte so a later pattern (or a
# shorter registered secret) cannot match inside the replacement marker.  The
# marker is restored only after every registered secret and pattern has been
# processed.
_PLACEHOLDER = "\x00"
_MARKER = "[REDACTED]"


class Redactor:
    """Redaction registry holding known secret values plus compiled token patterns."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register_secret(self, value: str) -> None:
        """Register a secret value for redaction."""
        self._secrets.add(value)

    def redact_text(self, text: str) -> str:
        """Redact registered secrets and token patterns from ``text``."""
        redacted = text
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, _PLACEHOLDER)
        for pattern in _PATTERNS:
            redacted = pattern.sub(_PLACEHOLDER, redacted)
        return redacted.replace(_PLACEHOLDER, _MARKER)

    def redact(self, value: Any) -> Any:
        """Recursively redact strings and mapping keys inside ``value``.

        Handles strings, ``bytes``, mappings, sequences, sets, and
        :class:`~pydantic.SecretStr` so a caller cannot bypass scrubbing by
        choosing a different container type.
        """
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, SecretStr):
            return _MARKER
        if isinstance(value, bytes):
            return self.redact_text(value.decode("utf-8", errors="replace"))
        if isinstance(value, dict):
            return {self.redact_text(str(key)): self.redact(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, (set, frozenset)):
            # Redaction turns tuples into lists, which are unhashable.  Restore
            # hashability recursively so the container type survives.
            cleaned = {_hashable(self.redact(item)) for item in value}
            return frozenset(cleaned) if isinstance(value, frozenset) else cleaned
        return value


def _hashable(value: Any) -> Any:
    """Recursively turn redactor output into a hashable value for sets."""
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple((key, _hashable(item)) for key, item in value.items())
    return value


_redactor = Redactor()


def get_redactor() -> Redactor:
    """Return the process-wide redactor (used by secret providers and logging)."""
    return _redactor


def register_secret(value: str) -> None:
    """Register a secret value with the process-wide redactor."""
    _redactor.register_secret(value)


def reset_redactor() -> None:
    """Drop all registered secret values (mainly for tests)."""
    global _redactor
    _redactor = Redactor()


def redact(value: Any) -> Any:
    """Redact ``value`` (deeply) using the global redactor."""
    return _redactor.redact(value)
