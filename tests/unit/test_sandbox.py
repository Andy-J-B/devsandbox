"""Unit tests for ``DockerSandboxManager`` that do not require a Docker daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from devsandbox.common.errors import PolicyViolation
from devsandbox.common.types import AttemptId, NetworkPolicy, ResourceProfile
from devsandbox.config.settings import DevSandboxSettings
from devsandbox.sandbox.interfaces import SandboxRequest
from devsandbox.sandbox.manager import _TRUNCATION_MARKER, DockerSandboxManager


class FakeContainer:
    """Minimal stand-in for a docker-py Container."""

    def __init__(self, container_id: str = "container-1") -> None:
        self.id = container_id
        self.attrs = {"State": {"Status": "running", "OOMKilled": False}}
        self.labels = {"devsandbox.managed": "true"}
        self.started = False
        self.removed = False
        self.killed = False

    def start(self) -> None:
        self.started = True

    def wait(self, timeout: int | None = None) -> dict[str, Any]:
        return {"StatusCode": 0}

    def kill(self, signal: str = "SIGKILL") -> None:
        self.killed = True

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def reload(self) -> None:
        return None

    def attach_socket(self, params: dict[str, Any]) -> Any:
        return FakeSocket([])

    def stats(self, stream: bool = False) -> dict[str, Any]:
        return {"memory_stats": {"usage": 0}}


class FakeSocket:
    """Byte stream that returns at most ``size`` bytes per ``recv`` call."""

    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        self._buffer = b""
        for stream_type, payload in frames:
            header = bytes([stream_type]) + b"\x00\x00\x00" + len(payload).to_bytes(4, "big")
            self._buffer += header + payload

    def recv(self, size: int) -> bytes:
        chunk = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return chunk


class FakeImages:
    def __init__(self) -> None:
        self.pulled: list[str] = []
        self._missing: set[str] = set()

    def get(self, image: str) -> None:
        if image in self._missing:
            from docker.errors import ImageNotFound

            raise ImageNotFound(image)

    def pull(self, image: str) -> None:
        self.pulled.append(image)


class FakeAPI:
    def __init__(self) -> None:
        self.exec_create_calls: list[tuple[str, list[str]]] = []

    def exec_create(self, container_id: str, cmd: list[str]) -> str:
        self.exec_create_calls.append((container_id, list(cmd)))
        return "exec-1"

    def exec_start(self, exec_id: str, timeout: int | None = None) -> bytes:
        return b""

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": 0}


class FakeContainers:
    def __init__(self) -> None:
        self._containers: dict[str, FakeContainer] = {}
        self.run_kwargs: list[dict[str, Any]] = []
        self.list_filters: dict[str, Any] | None = None

    def get(self, name: str) -> FakeContainer:
        if name not in self._containers:
            from docker.errors import NotFound

            raise NotFound(name)
        return self._containers[name]

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_kwargs.append(kwargs)
        container = FakeContainer(kwargs.get("name", "container-1"))
        self._containers[container.id] = container
        return container

    def list(
        self,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[FakeContainer]:
        self.list_filters = filters
        return list(self._containers.values())


class FakeDockerClient:
    def __init__(self) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers()
        self.api = FakeAPI()

    def info(self) -> dict[str, Any]:
        return {"NCPU": 2, "MemTotal": 4 * 1024**3}


def _make_settings(*, allowed_images: list[str] | None = None) -> DevSandboxSettings:
    security = {"allow_network_default": True, "allow_push": False, "allow_main_merge": False}
    if allowed_images is not None:
        security["allowed_images"] = allowed_images
    return DevSandboxSettings(security=security)  # type: ignore[arg-type]


def _make_manager(
    settings: DevSandboxSettings | None = None, client: FakeDockerClient | None = None
) -> DockerSandboxManager:
    return DockerSandboxManager(
        settings or _make_settings(),
        data_root="/tmp/devsandbox-tests",
        client=client or FakeDockerClient(),
    )


def _make_request(
    *,
    image: str = "agent-base:latest",
    command: list[str] | None = None,
    network: NetworkPolicy | None = None,
    resources: ResourceProfile | None = None,
    secret_env: dict[str, SecretStr] | None = None,
    labels: dict[str, str] | None = None,
) -> SandboxRequest:
    return SandboxRequest(
        attempt_id=AttemptId("attempt-1"),
        image=image,
        workspace_path=Path("/tmp/devsandbox-tests/workspaces/proj-1/attempt-1"),
        command=command or ["echo", "hi"],
        env={},
        secret_env=secret_env or {},
        resources=resources or ResourceProfile(),
        network=network or NetworkPolicy(mode="none"),
        timeout_seconds=60,
        labels=labels or {},
    )


def test_build_container_kwargs_enforces_isolation() -> None:
    manager = _make_manager()
    req = _make_request(
        resources=ResourceProfile(cpu_cores=2.0, memory_mb=512, pids_limit=128),
        network=NetworkPolicy(mode="none"),
        labels={"devsandbox.project": "proj-1", "devsandbox.task": "task-1"},
    )
    kwargs = manager._build_container_kwargs(req, NetworkPolicy(mode="none"), req.resources)

    # Naming / labels (00 §8.5).
    assert kwargs["name"] == "ds-attempt-1"
    assert kwargs["labels"]["devsandbox.managed"] == "true"
    assert kwargs["labels"]["devsandbox.attempt"] == "attempt-1"
    assert kwargs["labels"]["devsandbox.project"] == "proj-1"

    # Command never carries secrets.
    assert kwargs["command"] == ["echo", "hi"]

    # Mounts: workspace only, no docker.sock, no other host mounts.
    mounts = kwargs["mounts"]
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Target"] == "/workspace"
    assert mount["Source"] == str(req.workspace_path)
    assert mount["Type"] == "bind"
    assert mount.get("ReadOnly") is False
    assert "docker.sock" not in str(mounts)
    assert "/var/run" not in str(mounts)

    # Isolation.
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in kwargs["security_opt"]
    assert kwargs["user"] == "1000"
    assert kwargs["read_only"] is True
    assert "/tmp" in kwargs["tmpfs"]
    assert "/home/agent" in kwargs["tmpfs"]

    # Resource limits.
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["memswap_limit"] == "512m"
    assert kwargs["nano_cpus"] == int(2.0 * 1_000_000_000)
    assert kwargs["pids_limit"] == 128

    # Network.
    assert kwargs["network_mode"] == "none"


def test_build_container_kwargs_full_network_uses_default_bridge() -> None:
    manager = _make_manager()
    req = _make_request(network=NetworkPolicy(mode="full"))
    kwargs = manager._build_container_kwargs(req, NetworkPolicy(mode="full"), req.resources)
    assert kwargs["network_mode"] == "default"


def test_run_clamps_resources_before_building_container() -> None:
    client = FakeDockerClient()
    manager = _make_manager(client=client)
    req = _make_request(
        resources=ResourceProfile(cpu_cores=1000.0, memory_mb=10_000_000, pids_limit=99999)
    )

    import asyncio

    asyncio.run(manager.run(req))

    kwargs = client.containers.run_kwargs[-1]
    assert kwargs["mem_limit"] == "8192m"  # SecurityPolicy.MAX_MEMORY_MB
    assert kwargs["nano_cpus"] == int(4.0 * 1_000_000_000)  # SecurityPolicy.MAX_CPU_CORES
    assert kwargs["pids_limit"] == 1024  # SecurityPolicy.MAX_PIDS_LIMIT


def test_build_container_kwargs_maps_resources_faithfully() -> None:
    manager = _make_manager()
    req = _make_request(resources=ResourceProfile(cpu_cores=2.0, memory_mb=512, pids_limit=128))
    kwargs = manager._build_container_kwargs(req, NetworkPolicy(mode="none"), req.resources)
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["nano_cpus"] == int(2.0 * 1_000_000_000)
    assert kwargs["pids_limit"] == 128


def test_run_disallowed_image_raises_without_docker() -> None:
    manager = _make_manager(_make_settings(allowed_images=["good:img"]))
    req = _make_request(image="bad:img")
    with pytest.raises(PolicyViolation):
        # run is async; the policy check happens before any docker call.
        import asyncio

        asyncio.run(manager.run(req))


def test_run_allowlist_network_raises_without_falling_back_to_full() -> None:
    manager = _make_manager()
    req = _make_request(network=NetworkPolicy(mode="allowlist", allowed_hosts=["example.com"]))
    with pytest.raises(PolicyViolation):
        import asyncio

        asyncio.run(manager.run(req))


def test_run_creates_container_and_returns_result() -> None:
    client = FakeDockerClient()
    manager = _make_manager(client=client)
    req = _make_request(command=["echo", "hi"])

    import asyncio

    result = asyncio.run(manager.run(req))

    assert result.container_id == "ds-attempt-1"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.oom_killed is False
    assert result.stdout_path == Path("/tmp/devsandbox-tests/logs/attempt-1/stdout.log")
    assert result.stderr_path == Path("/tmp/devsandbox-tests/logs/attempt-1/stderr.log")
    # Container was removed (keep_on_failure default False).
    assert client.containers._containers["ds-attempt-1"].removed
    assert client.containers.run_kwargs  # at least one run call


def test_run_secret_env_never_in_command_or_labels() -> None:
    client = FakeDockerClient()
    manager = _make_manager(client=client)
    secret = SecretStr("super-secret-canary")
    req = _make_request(secret_env={"API_KEY": secret})

    import asyncio

    asyncio.run(manager.run(req))
    kwargs = client.containers.run_kwargs[-1]
    assert secret.get_secret_value() not in " ".join(kwargs["command"])
    assert secret.get_secret_value() not in str(kwargs["labels"])


def test_docker_info() -> None:
    client = FakeDockerClient()
    manager = _make_manager(client=client)
    info = manager.docker_info()
    assert info == {"NCPU": 2, "MemTotal": 4 * 1024**3}


@pytest.mark.asyncio
async def test_exec_in_kill_destroy_inspect_list_managed() -> None:
    client = FakeDockerClient()
    container = FakeContainer()
    client.containers._containers["container-1"] = container
    manager = _make_manager(client=client)

    result = await manager.exec_in("container-1", ["ps"], 5)
    assert result.exit_code == 0
    assert client.api.exec_create_calls == [("container-1", ["ps"])]

    await manager.kill("container-1")
    assert container.killed

    await manager.destroy("container-1")
    assert container.removed

    inspected = await manager.inspect("container-1")
    assert inspected is not None
    assert inspected.container_id == "container-1"

    missing = await manager.inspect("does-not-exist")
    assert missing is None

    managed = await manager.list_managed()
    assert len(managed) == 1
    assert managed[0].container_id == "container-1"


@pytest.mark.asyncio
async def test_reap_orphans_removes_unknown_and_old_containers() -> None:
    client = FakeDockerClient()
    client.containers._containers["container-1"] = FakeContainer("container-1")
    client.containers._containers["container-2"] = FakeContainer("container-2")
    manager = _make_manager(client=client)

    removed = await manager.reap_orphans({AttemptId("attempt-1")}, max_age_seconds=0.0)
    assert "container-1" in removed  # attempt active but age 0 > max_age 0
    assert client.containers._containers["container-1"].removed


def test_write_to_cap_truncates_with_marker() -> None:
    from devsandbox.sandbox.manager import DockerSandboxManager

    buf = bytearray()
    fake_file = type("F", (), {"write": lambda self, b: buf.extend(b)})()
    written = DockerSandboxManager._write_to_cap(fake_file, b"hello world", 0, 5)
    assert written == 5
    assert bytes(buf) == b"hello" + _TRUNCATION_MARKER


def test_stream_logs_redacts_and_demuxes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager = _make_manager()
    container = FakeContainer()
    # stdout frame with a canary secret, stderr frame plain.
    frames = [
        (1, b"stdout data sk-1234567890abcdef\n"),
        (2, b"stderr data\n"),
    ]
    container.attach_socket = lambda params: FakeSocket(frames)  # type: ignore[method-assign]

    import devsandbox.sandbox.manager as manager_module

    seen: list[str] = []
    original_redact = manager_module.redact

    def spy(text: str) -> str:
        seen.append(text)
        return original_redact(text)

    monkeypatch.setattr(manager_module, "redact", spy)
    try:
        manager._stream_logs(
            container,
            tmp_path / "stdout.log",
            tmp_path / "stderr.log",
            cap_bytes=1024,
        )
    finally:
        monkeypatch.undo()

    assert any("sk-1234567890abcdef" in s for s in seen)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
