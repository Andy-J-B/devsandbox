"""Bearer-token loading and zero-downtime rotation.

Authentication is configured from settings (``DEVSANDBOX_API_TOKEN`` or
``api.token``), an overlap list (``api.additional_tokens``), and optionally a
newline-delimited token file (``api.token_file``).  The file is the practical
way to rotate without restarting: rewrite it, then send ``SIGHUP``.

The token store keeps the last known-good set.  A reload that produces no
tokens (for example the file is temporarily empty or unreadable) is rejected so
a bad edit cannot accidentally open or lock the API.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from ..common.errors import ConfigError
from ..common.logging import get_logger
from ..config.settings import DevSandboxSettings

_log = get_logger("devsandbox.api.tokens")


@dataclass(frozen=True)
class TokenReloadResult:
    """Outcome of a token reload attempt (never contains token values)."""

    accepted: bool
    token_count: int
    detail: str


def read_token_file(path: str | Path) -> tuple[SecretStr, ...]:
    """Read newline-delimited tokens, ignoring blank lines and comments."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read API token file {source}: {exc}") from exc
    tokens: list[SecretStr] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value in seen:
            continue
        seen.add(value)
        tokens.append(SecretStr(value))
    return tuple(tokens)


def tokens_from_settings(settings: DevSandboxSettings) -> tuple[SecretStr, ...]:
    """Combine settings tokens and the optional token file, de-duplicated.

    Settings tokens come first (primary, then rotation overlap); file tokens
    follow.  Empty and duplicate values are ignored.
    """
    candidates: list[SecretStr] = list(settings.effective_api_tokens)
    token_file = settings.api.token_file
    if token_file:
        candidates.extend(read_token_file(token_file))
    tokens: list[SecretStr] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = candidate.get_secret_value()
        if not value or value in seen:
            continue
        seen.add(value)
        tokens.append(candidate)
    return tuple(tokens)


class TokenStore:
    """Mutable, process-wide set of accepted bearer tokens.

    ``loader`` is called on reload and returns a fresh candidate tuple.  The
    initial set is loaded eagerly so the API still fails closed when no token
    is configured at startup.
    """

    def __init__(
        self,
        initial: Iterable[SecretStr],
        *,
        loader: Callable[[], tuple[SecretStr, ...]] | None = None,
    ) -> None:
        self._tokens = tuple(initial)
        if not self._tokens:
            raise ConfigError(
                "DEVSANDBOX_API_TOKEN (or api.token / api.token_file) is required "
                "before the API can serve requests"
            )
        self._loader = loader or _default_loader

    @property
    def tokens(self) -> tuple[SecretStr, ...]:
        """Return the currently accepted tokens (primary first)."""
        return self._tokens

    def reload(self) -> TokenReloadResult:
        """Replace the accepted tokens from the configured loader.

        A loader error or an empty result leaves the previous tokens in place.
        """
        try:
            candidate = self._loader()
        except Exception as exc:
            detail = f"token reload failed; keeping {len(self._tokens)} token(s): {exc}"
            _log.error("api token reload failed", error_type=type(exc).__name__, error=str(exc))
            return TokenReloadResult(False, len(self._tokens), detail)
        if not candidate:
            detail = f"token reload produced no tokens; keeping {len(self._tokens)} token(s)"
            _log.warning("api token reload rejected", reason="empty token set")
            return TokenReloadResult(False, len(self._tokens), detail)
        previous = len(self._tokens)
        self._tokens = tuple(candidate)
        detail = f"accepted {len(self._tokens)} token(s); previous count {previous}"
        _log.info("api token reload", tokens=len(self._tokens), previous=previous)
        return TokenReloadResult(True, len(self._tokens), detail)


def _default_loader() -> tuple[SecretStr, ...]:
    return tokens_from_settings(DevSandboxSettings())


def install_reload_handler(
    app: object,
    *,
    signum: int | None = None,
) -> bool:
    """Install a ``SIGHUP`` handler that reloads the app's token store.

    Returns ``False`` when the platform has no ``SIGHUP`` or the process is not
    on the main thread (``signal.signal`` would raise).  The handler never logs
    token values.
    """
    signal_number = signum if signum is not None else getattr(signal, "SIGHUP", None)
    if signal_number is None:
        return False
    store = getattr(getattr(app, "state", None), "token_store", None)
    if not isinstance(store, TokenStore):
        return False

    def _handler(_signum: int, _frame: object) -> None:
        store.reload()

    try:
        signal.signal(signal_number, _handler)
    except (ValueError, OSError):
        return False
    return True


__all__ = [
    "TokenReloadResult",
    "TokenStore",
    "install_reload_handler",
    "read_token_file",
    "tokens_from_settings",
]
