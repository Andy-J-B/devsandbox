"""Docker-backed :class:`SandboxManager` (plan 02).

Runs each agent/validation attempt in a disposable, locked-down Docker
container.  All blocking docker-py calls are executed through
``asyncio.to_thread`` so the async event loop is never blocked; the
orchestration (timeout, kill, streaming) stays on the main event loop.

Security properties enforced in :meth:`_build_container_kwargs` (unit-tested
in ``tests/unit/test_sandbox.py``):

* no host socket / filesystem mounts other than the workspace bind mount,
* ``cap_drop=["ALL"]``, ``security_opt=["no-new-privileges"]``,
* non-root user (uid 1000), read-only rootfs with tmpfs for ``/tmp`` and
  ``/home/agent``,
* secrets only ever enter through container environment variables (never the
  command line or labels) and are redacted from logs / exceptions,
* the requested network policy can only be *narrowed* by policy, never
  widened; ``allowlist`` is not yet supported and raises ``PolicyViolation``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, ImageNotFound, NotFound  # type: ignore[import-not-found]
from requests.exceptions import ReadTimeout  # type: ignore[import-untyped]

from ..common.errors import PolicyViolation, SandboxError
from ..common.redaction import redact
from ..common.time import utcnow
from ..common.types import AttemptId, NetworkPolicy, ResourceProfile
from ..config.policy import SecurityPolicy
from ..config.settings import DevSandboxSettings
from .interfaces import (
    ExecResult,
    ManagedContainer,
    SandboxRequest,
    SandboxResult,
)

_LOG_CAP_BYTES = 50 * 1024 * 1024
_TRUNCATION_MARKER = b"\n[... log truncated by devsandbox ...]\n"
_GRACE_SECONDS = 10.0
_STATS_SAMPLE_INTERVAL = 2.0
_WAIT_EXTRA_SECONDS = 60.0

_MANAGED_LABEL = "devsandbox.managed"
_ATTEMPT_LABEL = "devsandbox.attempt"


def _container_name(attempt_id: AttemptId) -> str:
    """Deterministic container name for an attempt (00 §8.5)."""
    return f"ds-{attempt_id}"


def _parse_created(value: str) -> datetime:
    """Parse a Docker ``Created`` timestamp into an aware datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class DockerSandboxManager:
    """Docker implementation of :class:`SandboxManager`."""

    def __init__(
        self,
        settings: DevSandboxSettings,
        data_root: str | Path,
        *,
        keep_on_failure: bool = False,
        client: Any = None,
        log_cap_bytes: int = _LOG_CAP_BYTES,
    ) -> None:
        self._settings = settings
        self._policy = SecurityPolicy(settings)
        self._data_root = Path(data_root)
        self._keep_on_failure = keep_on_failure
        self._log_cap_bytes = log_cap_bytes
        self._client: Any = client or docker.from_env()  # type: ignore[attr-defined]

    # -- SandboxManager ----------------------------------------------------

    async def run(self, req: SandboxRequest) -> SandboxResult:
        """Run ``req`` in a disposable container and return :class:`SandboxResult`."""
        # Policy checks that need no docker access happen first (fail fast).
        network = self._policy.effective_network(req.network)
        if network.mode == "allowlist":
            raise PolicyViolation(
                "allowlist network mode is not supported in the MVP; "
                "refusing to fall back to full network access"
            )
        if not self._policy.image_allowed(req.image):
            raise PolicyViolation(f"image not allowed by policy: {req.image}")
        resources = self._policy.effective_resources(req.resources)

        await asyncio.to_thread(self._ensure_image, req.image)
        container = await asyncio.to_thread(
            self._create_or_replace_container, req, network, resources
        )
        container_id = container.id

        log_dir = self._data_root / "logs" / str(req.attempt_id)
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"

        started_at = utcnow()
        timed_out = False
        oom_killed = False
        exit_code: int | None = None
        peak_memory_mb: int | None = None

        stop_sampling = threading.Event()
        peak_usage: list[int] = [0]
        stream_task = asyncio.create_task(
            asyncio.to_thread(
                self._stream_logs,
                container,
                stdout_path,
                stderr_path,
                self._log_cap_bytes,
            )
        )
        sampler_task = asyncio.create_task(
            asyncio.to_thread(self._sample_peak_memory, container, stop_sampling, peak_usage)
        )

        try:
            try:
                wait_result = await asyncio.wait_for(
                    asyncio.to_thread(container.wait, timeout=req.timeout_seconds),
                    timeout=req.timeout_seconds + _WAIT_EXTRA_SECONDS,
                )
                exit_code = int(wait_result.get("StatusCode", 0) or 0)
            except (ReadTimeout, TimeoutError):
                timed_out = True
                await asyncio.to_thread(container.kill, signal="SIGTERM")
                try:
                    wait_result = await asyncio.wait_for(
                        asyncio.to_thread(container.wait, timeout=_GRACE_SECONDS),
                        timeout=_GRACE_SECONDS + 5.0,
                    )
                    exit_code = int(wait_result.get("StatusCode", 0) or 0) if wait_result else None
                except (ReadTimeout, TimeoutError):
                    await asyncio.to_thread(container.kill, signal="SIGKILL")
                    await asyncio.to_thread(container.wait)
                    exit_code = None
        finally:
            stop_sampling.set()
            await asyncio.gather(sampler_task, stream_task, return_exceptions=True)

        try:
            container.reload()
            oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled"))
        except Exception:  # best-effort
            oom_killed = False
        if peak_usage[0]:
            peak_memory_mb = int(peak_usage[0] / (1024 * 1024))

        finished_at = utcnow()
        if not self._keep_on_failure:
            await asyncio.to_thread(self._remove_container_ignoring_errors, container)

        return SandboxResult(
            container_id=container_id,
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            finished_at=finished_at,
            peak_memory_mb=peak_memory_mb,
        )

    # -- docker helpers -----------------------------------------------------

    def _ensure_image(self, image: str) -> None:
        """Pull ``image`` if it is not already present locally."""
        try:
            self._client.images.get(image)
        except ImageNotFound:
            try:
                self._client.images.pull(image)
            except APIError as exc:
                raise SandboxError(f"failed to pull image {image}: {exc}") from exc
        except APIError as exc:
            raise SandboxError(f"failed to inspect image {image}: {exc}") from exc

    def _create_or_replace_container(
        self, req: SandboxRequest, network: NetworkPolicy, resources: ResourceProfile
    ) -> Any:
        """Create (deterministically replacing any prior crashed container)."""
        name = _container_name(req.attempt_id)
        try:
            existing = self._client.containers.get(name)
            with contextlib.suppress(APIError):
                existing.remove(force=True)
        except NotFound:
            pass

        kwargs = self._build_container_kwargs(req, network, resources)
        try:
            return self._client.containers.run(**kwargs)
        except APIError as exc:
            raise SandboxError(f"failed to start container for {req.attempt_id}: {exc}") from exc

    def _build_container_kwargs(
        self, req: SandboxRequest, network: NetworkPolicy, resources: ResourceProfile
    ) -> dict[str, Any]:
        """Build the docker ``containers.run`` kwargs.

        This is a pure function of its inputs (no docker I/O) so it can be
        unit-tested to guarantee the isolation invariants.
        """
        environment: dict[str, str] = dict(req.env)
        for name, secret in req.secret_env.items():
            environment[name] = secret.get_secret_value()

        tmpfs: dict[str, str] = {
            "/tmp": "",
            "/home/agent": "",
        }

        labels: dict[str, str] = {
            _MANAGED_LABEL: "true",
            _ATTEMPT_LABEL: str(req.attempt_id),
        }
        labels.update(req.labels)

        mount = docker.types.Mount(  # type: ignore[attr-defined]
            target="/workspace",
            source=str(req.workspace_path),
            type="bind",
            read_only=False,
        )

        kwargs: dict[str, Any] = {
            "image": req.image,
            "name": _container_name(req.attempt_id),
            "command": list(req.command),
            "environment": environment,
            "labels": labels,
            "user": "1000",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "read_only": True,
            "tmpfs": tmpfs,
            "mounts": [mount],
            "mem_limit": f"{resources.memory_mb}m",
            "memswap_limit": f"{resources.memory_mb}m",
            "nano_cpus": int(resources.cpu_cores * 1_000_000_000),
            "pids_limit": resources.pids_limit,
            "working_dir": "/workspace",
            "detach": True,
        }
        if network.mode == "none":
            kwargs["network_mode"] = "none"
        else:
            # "full": default bridge network (no host access beyond it).
            kwargs["network_mode"] = "default"

        return kwargs

    def _stream_logs(
        self,
        container: Any,
        stdout_path: Path,
        stderr_path: Path,
        cap_bytes: int,
    ) -> None:
        """Demux the container's attach socket into the two log files.

        Output is passed through :func:`redact` and each file is capped at
        ``cap_bytes`` with a truncation marker.
        """
        sock = container.attach_socket(params={"stdout": 1, "stderr": 1, "stream": 1})
        stdout_written = 0
        stderr_written = 0
        try:
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                while True:
                    header = self._recv_exact(sock, 8)
                    if not header:
                        break
                    stream_type = header[0]
                    size = int.from_bytes(header[4:8], "big")
                    payload = self._recv_exact(sock, size) if size else b""
                    if size and not payload:
                        break
                    text = redact(payload.decode("utf-8", errors="replace"))
                    encoded = text.encode("utf-8")
                    if stream_type == 1:
                        stdout_written = self._write_to_cap(
                            stdout_file, encoded, stdout_written, cap_bytes
                        )
                    elif stream_type == 2:
                        stderr_written = self._write_to_cap(
                            stderr_file, encoded, stderr_written, cap_bytes
                        )
        finally:
            with contextlib.suppress(Exception):
                sock.close()

    @staticmethod
    def _recv_exact(sock: Any, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_to_cap(file: Any, data: bytes, already_written: int, cap_bytes: int) -> int:
        """Write up to ``cap_bytes`` total; return new written count."""
        if already_written >= cap_bytes:
            return already_written
        remaining = cap_bytes - already_written
        if len(data) > remaining:
            file.write(data[:remaining])
            file.write(_TRUNCATION_MARKER)
            return cap_bytes
        file.write(data)
        return already_written + len(data)

    def _sample_peak_memory(self, container: Any, stop: threading.Event, peak: list[int]) -> None:
        """Best-effort peak memory sampler (runs in a worker thread)."""
        while not stop.is_set():
            try:
                stats = container.stats(stream=False)
                mem_stats = stats.get("memory_stats", {}) or {}
                usage = int(mem_stats.get("usage", 0) or 0)
                if usage > peak[0]:
                    peak[0] = usage
            except Exception:  # best-effort
                pass
            stop.wait(_STATS_SAMPLE_INTERVAL)

    def _remove_container_ignoring_errors(self, container: Any) -> None:
        with contextlib.suppress(Exception):
            container.remove(force=True)

    async def exec_in(self, container_id: str, cmd: list[str], timeout_seconds: int) -> ExecResult:
        """Execute ``cmd`` inside a running container."""
        try:
            await asyncio.to_thread(self._client.containers.get, container_id)
        except NotFound as exc:
            raise SandboxError(f"container {container_id} not found") from exc

        try:
            exec_id = await asyncio.to_thread(self._client.api.exec_create, container_id, cmd)
            await asyncio.wait_for(
                asyncio.to_thread(self._client.api.exec_start, exec_id, timeout=timeout_seconds),
                timeout=timeout_seconds + 10.0,
            )
            inspect = await asyncio.to_thread(self._client.api.exec_inspect, exec_id)
            exit_code = int(inspect.get("ExitCode", 0) or 0)
            timed_out = False
        except TimeoutError:
            timed_out = True
            exit_code = None
        except APIError as exc:
            raise SandboxError(f"exec in {container_id} failed: {exc}") from exc

        return ExecResult(
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=False,
        )

    async def kill(self, container_id: str) -> None:
        try:
            container = await asyncio.to_thread(self._client.containers.get, container_id)
        except NotFound:
            return
        with contextlib.suppress(APIError):
            await asyncio.to_thread(container.kill)

    async def destroy(self, container_id: str) -> None:
        try:
            container = await asyncio.to_thread(self._client.containers.get, container_id)
        except NotFound:
            return
        with contextlib.suppress(APIError):
            await asyncio.to_thread(container.remove, force=True)

    async def list_managed(self) -> list[ManagedContainer]:
        containers = await asyncio.to_thread(
            self._client.containers.list,
            filters={"label": f"{_MANAGED_LABEL}=true"},
            all=True,
        )
        return [
            ManagedContainer(
                container_id=c.id,
                state=(c.attrs.get("State", {}) or {}).get("Status", "unknown"),
                labels=dict(c.labels or {}),
            )
            for c in containers
        ]

    async def inspect(self, container_id: str) -> ManagedContainer | None:
        try:
            container = await asyncio.to_thread(self._client.containers.get, container_id)
        except NotFound:
            return None
        return ManagedContainer(
            container_id=container.id,
            state=(container.attrs.get("State", {}) or {}).get("Status", "unknown"),
            labels=dict(container.labels or {}),
        )

    async def reap_orphans(
        self, known_attempt_ids: set[AttemptId], max_age_seconds: float = 3600.0
    ) -> list[str]:
        """Destroy managed containers that are not active or are too old.

        Returns the list of removed container ids.
        """
        removed: list[str] = []
        now = utcnow()
        for container in await self.list_managed():
            labels = container.labels
            attempt = labels.get(_ATTEMPT_LABEL)
            stale = attempt not in known_attempt_ids
            created_raw = ""
            try:
                container_obj = await asyncio.to_thread(
                    self._client.containers.get, container.container_id
                )
                created_raw = container_obj.attrs.get("Created", "") or ""
            except NotFound:
                removed.append(container.container_id)
                continue
            if not stale and created_raw:
                try:
                    age = (now - _parse_created(created_raw)).total_seconds()
                    stale = age > max_age_seconds
                except ValueError:
                    stale = False
            if stale:
                try:
                    container_obj = await asyncio.to_thread(
                        self._client.containers.get, container.container_id
                    )
                    await asyncio.to_thread(container_obj.remove, force=True)
                except APIError:  # best-effort
                    pass
                removed.append(container.container_id)
        return removed

    def docker_info(self) -> dict[str, Any]:
        """Return daemon-level info (CPU/memory totals) for plan 07 sanity checks."""
        return dict(self._client.info())
