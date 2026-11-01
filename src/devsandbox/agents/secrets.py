"""Secret resolution for agent runs.

``SecretProvider`` resolves a secret reference (a configured name, a host env
var name, or a file path) to a :class:`pydantic.SecretStr`, registers the
value with the redactor so it is never logged, and returns it for injection
into a container via ``SandboxRequest.secret_env``.

Only the secret named by the model config is ever passed into a container;
nothing else from the host environment leaks in.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import SecretStr

from ..common.errors import ConfigError
from ..common.redaction import Redactor, get_redactor
from ..common.types import ModelConfig
from ..config.settings import DevSandboxSettings, SecretRef

# Environment variable used for generic / OpenAI-compatible providers.  The
# rendered OpenCode config references the *name* only, never the value.
MODEL_API_KEY_ENV = "DEVSANDBOX_MODEL_API_KEY"

_PROVIDER_SECRET_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "opencode": MODEL_API_KEY_ENV,
}


def provider_secret_env(provider_id: str) -> str:
    """Return the environment variable OpenCode expects for ``provider_id``."""
    return _PROVIDER_SECRET_ENV.get(provider_id.lower(), MODEL_API_KEY_ENV)


class SecretProvider:
    """Resolve secret references to :class:`SecretStr` values."""

    def __init__(
        self,
        settings: DevSandboxSettings | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self._settings = settings
        self._redactor = redactor or get_redactor()

    @property
    def redactor(self) -> Redactor:
        """Return the redactor secrets are registered with."""
        return self._redactor

    def resolve(self, secret_ref: SecretRef | str) -> SecretStr:
        """Resolve ``secret_ref`` to a :class:`SecretStr`.

        Strings are interpreted as (in order) a name in
        ``settings.secrets``, an existing file path, or a host environment
        variable name.  Files must not be readable by group/other users.
        """
        if isinstance(secret_ref, SecretRef):
            value = self._resolve_ref(secret_ref)
        else:
            value = self._resolve_string(secret_ref)

        if not value:
            raise ConfigError("resolved secret is empty")
        self._redactor.register_secret(value)
        # The sandbox manager's log streamer uses the process-wide redactor;
        # registering there too guarantees container logs are scrubbed even
        # when a custom redactor was injected for tests or embedding.
        global_redactor = get_redactor()
        if global_redactor is not self._redactor:
            global_redactor.register_secret(value)
        return SecretStr(value)

    def secret_env_for(self, model: ModelConfig) -> dict[str, SecretStr]:
        """Return the container secret env for ``model`` (empty when unset)."""
        if not model.secret_ref:
            return {}
        return {provider_secret_env(model.provider_id): self.resolve(model.secret_ref)}

    def redact(self, value: object) -> object:
        """Redact registered secrets from ``value`` before logging/storage."""
        return self._redactor.redact(value)

    # -- internals --------------------------------------------------------

    def _resolve_string(self, ref: str) -> str:
        if not ref:
            raise ConfigError("secret_ref must not be empty")
        if self._settings is not None and ref in self._settings.secrets:
            return self._resolve_ref(self._settings.secrets[ref])
        path = Path(ref)
        if path.exists():
            return self._read_file(path)
        value = os.environ.get(ref)
        if value is None:
            raise ConfigError(f"secret not found (settings.secrets, file, or env): {ref}")
        return value

    def _resolve_ref(self, ref: SecretRef) -> str:
        if ref.env_var:
            value = os.environ.get(ref.env_var)
            if value is None:
                raise ConfigError(f"secret env var not set: {ref.env_var}")
            return value
        if ref.path:
            return self._read_file(Path(ref.path))
        raise ConfigError("secret_ref must set env_var or path")

    @staticmethod
    def _read_file(path: Path) -> str:
        try:
            info = path.stat()
        except OSError as exc:
            raise ConfigError(f"failed to stat secret file {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"secret path is not a regular file: {path}")
        if info.st_mode & 0o077:
            raise ConfigError(f"secret file {path} is readable by group/other; chmod 600 it first")
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"failed to read secret file {path}: {exc}") from exc


__all__ = ["MODEL_API_KEY_ENV", "SecretProvider", "provider_secret_env"]
