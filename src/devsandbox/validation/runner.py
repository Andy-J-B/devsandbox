"""Sandboxed validation check runner (plan 09).

Every check runs in a *fresh* container created by the injected
:class:`~devsandbox.sandbox.interfaces.SandboxManager`.  The runner never
reuses the agent's container, never passes secret environment variables, and
never trusts the agent's exit status: it reads the container's captured
stdout/stderr, applies the orchestrator-approved exit-code/regex rules, and
returns a structured :class:`~devsandbox.validation.models.CheckResult`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..common.redaction import redact
from ..common.time import utcnow
from ..common.types import AttemptId, CheckSpec, NetworkPolicy
from ..config.policy import SecurityPolicy
from ..sandbox.interfaces import SandboxManager, SandboxRequest, SandboxResult
from .config import ValidationConfig
from .models import CheckResult, FailureKind

_TRUNCATION_MARKER = "\n[... output truncated by devsandbox ...]\n"
_ALLOWED_ATTEMPT_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_attempt_id(value: str) -> str:
    """Return a Docker-name-safe attempt id (00 §8.5 deterministic naming)."""
    cleaned = _ALLOWED_ATTEMPT_CHARS.sub("-", value).strip("-.")
    return cleaned or "validation"


def _container_attempt_id(base: str, phase: str, index: int) -> AttemptId:
    """Build a deterministic, per-check container attempt id."""
    return AttemptId(f"{safe_attempt_id(base)}-{phase}-{index}")


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut + _TRUNCATION_MARKER


def _read_tail(path: Path, max_bytes: int) -> str:
    """Read and redact the last ``max_bytes`` of a log file (best effort)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = _TRUNCATION_MARKER + text
    return str(redact(text))


def _normalise_command(check: CheckSpec) -> tuple[list[str], str]:
    """Return ``(argv, display)`` for a check.

    ``run`` strings are executed with ``/bin/sh -lc``; argv lists are executed
    directly (via ``exec "$@"``) so quoting is never re-interpreted.
    """
    working_dir = check.working_dir or "."
    pure = PurePosixPath(working_dir)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("check working_dir must be a relative path inside /workspace")
    quoted_dir = shlex.quote(str(pure))
    if isinstance(check.run, list):
        display = shlex.join(check.run)
        command = [
            "/bin/sh",
            "-lc",
            f'cd {quoted_dir} && exec "$@"',
            "devsandbox-check",
            *check.run,
        ]
        return command, display
    display = check.run
    return ["/bin/sh", "-lc", f"cd {quoted_dir} && {check.run}"], display


def _narrow_network(requested: NetworkPolicy, ceiling: NetworkPolicy) -> NetworkPolicy:
    """Return the stricter of two network policies (never widens)."""
    rank = {"none": 0, "allowlist": 1, "full": 2}
    mode = requested.mode if rank[requested.mode] <= rank[ceiling.mode] else ceiling.mode
    allowed_hosts = list(requested.allowed_hosts) if mode == "allowlist" else []
    if mode == "allowlist" and ceiling.mode == "allowlist":
        allowed_hosts = [host for host in allowed_hosts if host in ceiling.allowed_hosts]
    return NetworkPolicy(mode=mode, allowed_hosts=allowed_hosts)


def _infer_failure_kind(check: CheckSpec, status: str) -> FailureKind | None:
    """Map a check's declared kind/name to a stable failure taxonomy value."""
    if status == "timeout":
        return FailureKind.TIMEOUT
    if status == "error":
        return FailureKind.INFRA_ERROR
    if status == "passed" or status == "skipped":
        return None
    if check.kind == "test":
        return FailureKind.TEST_FAILURE
    if check.kind == "lint":
        return FailureKind.LINT_FAILURE
    if check.kind == "build":
        return FailureKind.BUILD_FAILURE
    if check.kind == "setup":
        return FailureKind.INFRA_ERROR
    lowered = check.name.lower()
    if any(token in lowered for token in ("test", "pytest", "spec", "unittest")):
        return FailureKind.TEST_FAILURE
    if any(token in lowered for token in ("lint", "ruff", "eslint", "flake", "format")):
        return FailureKind.LINT_FAILURE
    if any(token in lowered for token in ("build", "compile", "mypy", "typecheck")):
        return FailureKind.BUILD_FAILURE
    return FailureKind.TEST_FAILURE


def _parse_junit(path: Path, max_bytes: int) -> tuple[int, int, int, int] | None:
    """Return ``(total, failed, skipped, errors)`` from a JUnit XML file."""
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
        root = ET.fromstring(data)
    except (OSError, ET.ParseError):
        return None

    def _ints(element: ET.Element) -> tuple[int, int, int, int]:
        return (
            int(element.attrib.get("tests", 0) or 0),
            int(element.attrib.get("failures", 0) or 0),
            int(element.attrib.get("skipped", 0) or 0),
            int(element.attrib.get("errors", 0) or 0),
        )

    if root.tag == "testsuite":
        return _ints(root)
    if root.tag == "testsuites":
        total = failed = skipped = errors = 0
        for child in root.findall("testsuite"):
            t, f, s, e = _ints(child)
            total += t
            failed += f
            skipped += s
            errors += e
        return total, failed, skipped, errors
    return None


class CheckRunner:
    """Execute :class:`CheckSpec` objects in disposable validation containers."""

    def __init__(
        self,
        sandbox: SandboxManager,
        config: ValidationConfig | None = None,
        *,
        policy: SecurityPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sandbox = sandbox
        self._config = config or ValidationConfig()
        self._policy = policy
        self._clock = clock

    @property
    def config(self) -> ValidationConfig:
        """Return the runner configuration."""
        return self._config

    async def run_checks(
        self,
        checks: list[CheckSpec],
        workspace_path: str | Path,
        *,
        attempt_id: str,
        phase: str = "task",
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> list[CheckResult]:
        """Run checks sequentially, honoring the optional short-circuit rule."""
        results: list[CheckResult] = []
        required_failed = False
        for index, check in enumerate(checks):
            if (
                required_failed
                and not check.required
                and self._config.short_circuit_on_required_failure
            ):
                results.append(
                    CheckResult(
                        check=check.name,
                        status="skipped",
                        required=check.required,
                        reason="skipped after a required check failed",
                        command=self._display(check),
                    )
                )
                continue
            result = await self.run_check(
                check,
                workspace_path,
                attempt_id=attempt_id,
                index=index,
                phase=phase,
                project_id=project_id,
                task_id=task_id,
            )
            results.append(result)
            if check.required and result.required_failure:
                required_failed = True
        return results

    async def run_check(
        self,
        check: CheckSpec,
        workspace_path: str | Path,
        *,
        attempt_id: str,
        index: int = 0,
        phase: str = "task",
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> CheckResult:
        """Run one check in a fresh validation container."""
        workspace = Path(workspace_path)
        container_attempt = _container_attempt_id(attempt_id, phase, index)
        command, display = self._command_or_error(check)
        if command is None:
            return self._error_result(
                check, display, FailureKind.INFRA_ERROR, "invalid check command"
            )

        network = _narrow_network(
            NetworkPolicy(mode=check.network, allowed_hosts=list(check.allowed_hosts)),
            self._config.network,
        )
        if self._policy is not None:
            network = self._policy.effective_network(network)
        if network.mode == "allowlist":
            return self._error_result(
                check,
                display,
                FailureKind.INFRA_ERROR,
                "allowlist network mode is not supported by the MVP sandbox",
            )
        if self._policy is not None:
            violation = self._policy.image_violation(self._config.image)
            if violation is not None:
                return self._error_result(
                    check,
                    display,
                    FailureKind.INFRA_ERROR,
                    f"validation {violation}",
                )

        resources = self._config.resources
        if self._policy is not None:
            resources = self._policy.effective_resources(resources)

        env = {
            "CI": "1",
            "DEVSANDBOX_VALIDATION": "1",
            "PYTHONUNBUFFERED": "1",
        }
        env.update(check.env)
        labels = {
            "devsandbox.phase": phase,
            "devsandbox.check": check.name,
            "devsandbox.attempt": str(attempt_id),
        }
        if project_id:
            labels["devsandbox.project"] = project_id
        if task_id:
            labels["devsandbox.task"] = task_id

        request = SandboxRequest(
            attempt_id=container_attempt,
            image=self._config.image,
            workspace_path=workspace,
            command=command,
            env=env,
            # Validation containers never receive agent secrets.
            secret_env={},
            resources=resources,
            network=network,
            timeout_seconds=check.timeout_seconds,
            labels=labels,
        )

        sandbox_result: SandboxResult | None = None
        try:
            sandbox_result = await self._sandbox.run(request)
        except Exception as exc:  # infrastructure failure, not the agent's fault
            message = str(redact(f"{type(exc).__name__}: {exc}"))
            return self._error_result(
                check, display, FailureKind.INFRA_ERROR, message, command=display
            )

        try:
            stdout_tail = await asyncio.to_thread(
                _read_tail, sandbox_result.stdout_path, self._config.max_output_bytes
            )
            stderr_tail = await asyncio.to_thread(
                _read_tail, sandbox_result.stderr_path, self._config.max_output_bytes
            )
            combined = _truncate(
                (stdout_tail + ("\n" if stdout_tail and stderr_tail else "") + stderr_tail),
                self._config.max_output_bytes,
            )
            counts = self._junit_counts(check, workspace)
            return self._classify(
                check,
                display,
                sandbox_result,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                output=combined,
                counts=counts,
            )
        finally:
            # Validation containers are always disposable.  The sandbox manager
            # normally removes them already; destroy is idempotent.
            with contextlib.suppress(Exception):
                await self._sandbox.destroy(sandbox_result.container_id)

    # -- helpers -----------------------------------------------------------

    def _display(self, check: CheckSpec) -> str:
        return check.run if isinstance(check.run, str) else shlex.join(check.run)

    def _command_or_error(self, check: CheckSpec) -> tuple[list[str] | None, str]:
        try:
            return _normalise_command(check)
        except ValueError as exc:
            return None, f"{self._display(check)} ({exc})"

    def _error_result(
        self,
        check: CheckSpec,
        display: str,
        kind: FailureKind,
        reason: str,
        *,
        command: str | None = None,
    ) -> CheckResult:
        return CheckResult(
            check=check.name,
            status="error",
            failure_kind=kind,
            reason=reason,
            command=command or display,
            output=reason,
            stderr_tail=reason,
        )

    def _junit_counts(self, check: CheckSpec, workspace: Path) -> tuple[int, int, int, int] | None:
        if not check.junit_xml_path:
            return None
        raw = Path(check.junit_xml_path)
        if raw.is_absolute():
            # Only paths inside the mounted workspace are readable.
            parts = raw.parts
            if len(parts) < 3 or parts[1] != "workspace":
                return None
            relative = Path(*parts[2:])
        else:
            relative = raw
        target = (workspace / relative).resolve()
        try:
            if not target.is_relative_to(workspace.resolve()):
                return None
        except ValueError:
            return None
        return _parse_junit(target, self._config.max_output_bytes)

    def _classify(
        self,
        check: CheckSpec,
        display: str,
        result: SandboxResult,
        *,
        stdout_tail: str,
        stderr_tail: str,
        output: str,
        counts: tuple[int, int, int, int] | None,
    ) -> CheckResult:
        duration = max(0.0, (result.finished_at - result.started_at).total_seconds())
        common: dict[str, Any] = {
            "check": check.name,
            "required": check.required,
            "exit_code": result.exit_code,
            "duration_seconds": duration,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "output": output,
            "command": display,
            "container_id": result.container_id,
        }
        if counts is not None:
            total, failed, skipped, errors = counts
            common.update(
                tests_total=total,
                tests_failed=failed,
                tests_skipped=skipped,
                tests_errors=errors,
            )

        if result.timed_out:
            return CheckResult(
                status="timeout",
                failure_kind=FailureKind.TIMEOUT,
                reason=f"check exceeded {check.timeout_seconds}s",
                **common,
            )
        if result.oom_killed:
            return CheckResult(
                status="error",
                failure_kind=FailureKind.INFRA_ERROR,
                reason="validation container was OOM-killed",
                **common,
            )
        if result.exit_code is None:
            return CheckResult(
                status="error",
                failure_kind=FailureKind.INFRA_ERROR,
                reason="validation container exited without an exit code",
                **common,
            )

        regex_error = self._regex_error(check, output)
        if regex_error is not None:
            return CheckResult(
                status="error",
                failure_kind=FailureKind.INFRA_ERROR,
                reason=regex_error,
                **common,
            )
        if result.exit_code not in check.expected_exit_codes:
            return CheckResult(
                status="failed",
                failure_kind=_infer_failure_kind(check, "failed"),
                reason=f"exit code {result.exit_code} not in {check.expected_exit_codes}",
                **common,
            )
        regex_failure = self._regex_failure(check, output)
        if regex_failure is not None:
            return CheckResult(
                status="failed",
                failure_kind=_infer_failure_kind(check, "failed"),
                reason=regex_failure,
                **common,
            )
        return CheckResult(status="passed", **common)

    @staticmethod
    def _regex_error(check: CheckSpec, output: str) -> str | None:
        for pattern in (check.success_regex, check.failure_regex):
            if pattern is None:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"invalid check regex {pattern!r}: {exc}"
        return None

    @staticmethod
    def _regex_failure(check: CheckSpec, output: str) -> str | None:
        if check.success_regex is not None and not re.search(check.success_regex, output):
            return f"success_regex did not match: {check.success_regex!r}"
        if check.failure_regex is not None and re.search(check.failure_regex, output):
            return f"failure_regex matched: {check.failure_regex!r}"
        return None


__all__ = ["CheckRunner", "safe_attempt_id"]
