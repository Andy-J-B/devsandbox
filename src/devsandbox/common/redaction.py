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
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_]{20,}\b")
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

_PATTERNS: tuple[re.Pattern[str], ...] = (
    _PRIVATE_KEY_RE,
    _BEARER_RE,
    _SECRET_VALUE_RE,
    _GITHUB_PAT_RE,
    _OPENAI_KEY_RE,
    _AWS_KEY_RE,
)


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
                redacted = redacted.replace(secret, "[REDACTED]")
        for pattern in _PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def redact(self, value: Any) -> Any:
        """Recursively redact strings inside ``value`` (str, dict, list, SecretStr)."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, SecretStr):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(key): self.redact(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value


_redactor = Redactor()


def redact(value: Any) -> Any:
    """Redact ``value`` (deeply) using the global redactor."""
    return _redactor.redact(value)
