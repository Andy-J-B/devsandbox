"""Audit shipping sinks (file NDJSON and HTTP webhook)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import SecretStr

from .models import AuditEnvelope


class AuditShipError(RuntimeError):
    """Raised when a sink cannot accept a batch."""


class AuditSink(Protocol):
    """Destination for a batch of audit envelopes."""

    async def write(self, envelopes: list[AuditEnvelope]) -> None: ...


class JsonlFileSink:
    """Append one JSON object per line to a local file.

    The file is opened with ``O_APPEND`` semantics and fsynced after every
    batch.  A crash during a write can duplicate the last batch, never lose an
    earlier one; the cursor is only advanced after ``write`` returns.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def write(self, envelopes: list[AuditEnvelope]) -> None:
        """Append the batch and flush it to disk."""
        if not envelopes:
            return
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lines = "".join(
                json.dumps(envelope.model_dump(mode="json"), separators=(",", ":")) + "\n"
                for envelope in envelopes
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(lines)
                handle.flush()
                os.fsync(handle.fileno())


class WebhookSink:
    """POST batches to an operator-owned HTTP endpoint.

    Transient failures (transport errors, HTTP 408/425/429/5xx) are retried
    with exponential backoff.  Other 4xx responses fail immediately because a
    bad URL, token, or payload will not improve on retry.
    """

    def __init__(
        self,
        url: str,
        *,
        token: SecretStr | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._url = url
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = sleep

    async def write(self, envelopes: list[AuditEnvelope]) -> None:
        """Ship one batch, retrying transient failures."""
        if not envelopes:
            return
        headers = {"Content-Type": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        body = {
            "schema_version": envelopes[0].schema_version,
            "events": [envelope.model_dump(mode="json") for envelope in envelopes],
        }
        last_error = "audit webhook failed"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"audit webhook transport error: {type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return
                last_error = f"audit webhook returned HTTP {response.status_code}"
                if not _retryable_status(response.status_code):
                    raise AuditShipError(last_error)
            if attempt < self._max_retries:
                await self._sleep(self._backoff(attempt))
        raise AuditShipError(last_error)

    def _backoff(self, attempt: int) -> float:
        return float(
            min(
                self._backoff_max_seconds,
                self._backoff_base_seconds * (2**attempt),
            )
        )

    async def aclose(self) -> None:
        """Close the HTTP client when this sink owns it."""
        if self._owns_client:
            await self._client.aclose()


class InMemorySink:
    """Test sink that records every shipped batch."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.batches: list[list[AuditEnvelope]] = []
        self._fail_times = fail_times

    async def write(self, envelopes: list[AuditEnvelope]) -> None:
        """Record a batch, failing the first ``fail_times`` calls if scripted."""
        if self._fail_times > 0:
            self._fail_times -= 1
            raise AuditShipError("scripted sink failure")
        self.batches.append(list(envelopes))


def _retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status < 600


__all__ = [
    "AuditShipError",
    "AuditSink",
    "InMemorySink",
    "JsonlFileSink",
    "WebhookSink",
]
