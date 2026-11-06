"""Typer CLI for DevSandbox (plan 10).

Every command is a thin wrapper around the ``/v1`` HTTP API.  Use ``--local``
to run the same commands against an in-process ASGI app (real database and
services, no network listener).  ``serve`` runs the API and the orchestration
engine together.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..common.errors import DevSandboxError, SpecError
from ..config.settings import get_settings
from ..spec.models import load_spec
from .client import ApiClient, CliError, open_client

app = typer.Typer(
    name="devsandbox",
    help="Sandboxed coding-agent orchestration: API, plans, tasks, and reports.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@dataclass
class CliState:
    """Global options shared by every command."""

    json_output: bool = False
    local: bool = False
    api_url: str | None = None
    token: str | None = None
    timeout: float = 30.0


def _state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if isinstance(state, CliState):
        return state
    return CliState()


def _client(state: CliState) -> AbstractAsyncContextManager[ApiClient]:
    return open_client(
        local=state.local,
        api_url=state.api_url,
        token=state.token,
        timeout=state.timeout,
    )


def _emit_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _truncate(value: Any, width: int = 60) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(title: str, columns: list[str], rows: list[list[Any]]) -> Table:
    table = Table(title=title, header_style="bold")
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*[_truncate(cell) for cell in row])
    return table


def _print_task_table(tasks: list[dict[str, Any]], title: str = "Tasks") -> None:
    rows = [
        [
            task.get("key"),
            task.get("title"),
            task.get("status"),
            f"{task.get('attempt_count', 0)}/{task.get('max_retries', 0)}",
            task.get("blocked_reason") or "",
        ]
        for task in tasks
    ]
    console.print(
        _table(
            title,
            ["Key", "Title", "Status", "Attempts", "Blocked / failure"],
            rows,
        )
    )


def _print_plan(plan: dict[str, Any]) -> None:
    console.print(
        f"[bold]Plan {plan.get('id')}[/bold]  version={plan.get('version')}  "
        f"status={plan.get('status')}  generated_by={plan.get('generated_by', '')}"
    )
    if plan.get("rationale"):
        console.print(str(plan["rationale"]))
    tasks = plan.get("tasks") or []
    rows = [
        [
            task.get("key"),
            task.get("title"),
            ", ".join(task.get("depends_on") or []) or "—",
            task.get("priority", 50),
        ]
        for task in tasks
        if isinstance(task, dict)
    ]
    console.print(_table("Plan tasks", ["Key", "Title", "Depends on", "Priority"], rows))
    issues = plan.get("issues") or []
    if issues:
        console.print("\n[bold]Graph issues[/bold]")
        for issue in issues:
            if isinstance(issue, dict):
                level = "error" if issue.get("is_error") else "warning"
                console.print(f"- {level}: {issue.get('message', issue)}")


def _print_project(project: dict[str, Any]) -> None:
    counts = project.get("counts") or {}
    console.print(
        f"[bold]{project.get('name')}[/bold]  id={project.get('id')}\n"
        f"status={project.get('status')}  mode={project.get('mode')}  "
        f"active_plan={project.get('active_plan_id')}\n"
        f"repo={project.get('repo_source')}  channel={project.get('main_branch')}  "
        f"staging={project.get('staging_branch')}"
    )
    console.print(
        "tasks: "
        f"{counts.get('total', 0)} total, {counts.get('completed', 0)} completed, "
        f"{counts.get('failed', 0)} failed, {counts.get('blocked', 0)} blocked, "
        f"{counts.get('running', 0)} running, {counts.get('pending', 0)} pending"
    )


def _failed_status(status: str | None) -> bool:
    return status in {"failed", "cancelled"}


def _exit_if_failed(status: str | None) -> None:
    if _failed_status(status):
        raise typer.Exit(code=1)


def _alembic_upgrade(database_url: str) -> None:
    """Apply the packaged migrations to ``database_url``."""
    from alembic import command
    from alembic.config import Config

    # Resolve migrations from the installed package (site-packages in a wheel,
    # src/ in a checkout) instead of assuming a repository checkout exists.
    package_root = Path(__file__).resolve().parents[1]
    config = Config()
    config.set_main_option("script_location", str(package_root / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback()
def root(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of tables."
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Use an in-process ASGI client (no HTTP server required).",
    ),
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        envvar="DEVSANDBOX_API_URL",
        help="Base URL of a running DevSandbox API.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="DEVSANDBOX_API_TOKEN",
        help="Bearer token (or set DEVSANDBOX_API_TOKEN).",
    ),
    timeout: float = typer.Option(30.0, "--timeout", help="HTTP timeout in seconds."),
) -> None:
    """Global options for every command."""
    ctx.obj = CliState(
        json_output=json_output,
        local=local,
        api_url=api_url,
        token=token,
        timeout=timeout,
    )


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Create/upgrade the database schema using Alembic."""
    settings = get_settings()
    try:
        _alembic_upgrade(settings.database_url)
    except Exception as exc:  # alembic raises its own exception types
        typer.secho(f"database migration failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"database is up to date: {settings.database_url}")


@app.command()
def serve(
    ctx: typer.Context,
    host: str | None = typer.Option(None, "--host", help="Bind address (default 127.0.0.1)."),
    port: int | None = typer.Option(None, "--port", help="Bind port (default 8000)."),
    token: str | None = typer.Option(
        None, "--token", envvar="DEVSANDBOX_API_TOKEN", help="Override the API token."
    ),
    no_engine: bool = typer.Option(
        False, "--no-engine", help="Serve read-only HTTP without the orchestration engine."
    ),
    log_level: str = typer.Option("info", "--log-level"),
) -> None:
    """Run the API server (and orchestration engine) until interrupted."""
    from pydantic import SecretStr

    from ..api.server import run

    settings = get_settings()
    if token:
        settings = settings.model_copy(update={"api_token": SecretStr(token)})
    if settings.effective_api_token is None:
        typer.secho(
            "DEVSANDBOX_API_TOKEN (or --token) is required before serving the API.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        run(settings, host=host, port=port, run_engine=not no_engine, log_level=log_level)
    except KeyboardInterrupt:
        return


@app.command()
def submit(
    ctx: typer.Context,
    spec: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a project YAML spec."
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="Override the spec repository with a local path or URL."
    ),
) -> None:
    """Submit a project spec."""
    state = _state(ctx)
    text = spec.read_text(encoding="utf-8")

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.submit_project(text, repo=repo)

    try:
        project = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(project)
    else:
        console.print(
            f"submitted project [bold]{project.get('id')}[/bold] "
            f"({project.get('name')}) status={project.get('status')}"
        )


@app.command()
def plan(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """Generate a new plan version for a project."""
    state = _state(ctx)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.create_plan(project)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        _print_plan(result)


@app.command("show-plan")
def show_plan(
    ctx: typer.Context,
    project: str = typer.Argument(...),
    md: bool = typer.Option(False, "--md", help="Render the plan(s) as Markdown."),
) -> None:
    """Show the newest plan (or all plans as Markdown)."""
    state = _state(ctx)

    async def _run() -> Any:
        async with _client(state) as client:
            if md:
                return await client.plans_markdown(project)
            plans = await client.list_plans(project)
            return plans[0] if plans else None

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if result is None:
        typer.secho("no plan exists for this project", fg="yellow", err=True)
        raise typer.Exit(code=1)
    if md:
        typer.echo(result)
    elif state.json_output:
        _emit_json(result)
    else:
        _print_plan(result)


@app.command()
def approve(
    ctx: typer.Context,
    plan_id: str = typer.Argument(..., help="Plan id returned by `plan` / `show-plan`."),
    approver: str = typer.Option("user", "--approver"),
) -> None:
    """Approve a proposed plan and activate it."""
    state = _state(ctx)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.approve_plan(plan_id, approver=approver)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        console.print(
            f"approved plan [bold]{result.get('id')}[/bold] status={result.get('status')}"
        )


@app.command()
def reject(
    ctx: typer.Context,
    plan_id: str = typer.Argument(...),
    reason: str = typer.Option("rejected by user", "--reason"),
) -> None:
    """Reject a proposed plan (the project stays awaiting approval)."""
    state = _state(ctx)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.reject_plan(plan_id, reason=reason)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        console.print(
            f"rejected plan [bold]{result.get('id')}[/bold] reason={result.get('rejection_reason')}"
        )


def _control(ctx: typer.Context, project: str, action: str) -> None:
    state = _state(ctx)
    actions = {
        "start": ApiClient.start,
        "pause": ApiClient.pause,
        "resume": ApiClient.resume,
        "cancel": ApiClient.cancel,
    }
    method = actions[action]

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await method(client, project)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        console.print(f"{action}: [bold]{result.get('id')}[/bold] status={result.get('status')}")


@app.command()
def start(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """Start/resume execution of an approved, planned project."""
    _control(ctx, project, "start")


@app.command()
def pause(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """Pause task scheduling for a project."""
    _control(ctx, project, "pause")


@app.command()
def resume(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """Resume a paused project."""
    _control(ctx, project, "resume")


@app.command()
def cancel(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """Cancel all outstanding work for a project."""
    _control(ctx, project, "cancel")


@app.command()
def mode(
    ctx: typer.Context,
    project: str = typer.Argument(...),
    new_mode: str = typer.Argument(..., metavar="MODE", help="supervised|autonomous"),
) -> None:
    """Explicitly change the project mode (policy-checked)."""
    state = _state(ctx)
    if new_mode not in {"supervised", "autonomous"}:
        typer.secho("mode must be 'supervised' or 'autonomous'", fg="red", err=True)
        raise typer.Exit(code=2)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.set_mode(project, new_mode)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        console.print(f"mode: [bold]{result.get('mode')}[/bold] status={result.get('status')}")


@app.command()
def status(
    ctx: typer.Context,
    project: str | None = typer.Argument(None),
    watch: bool = typer.Option(False, "--watch", help="Keep polling the project status."),
    interval: float = typer.Option(2.0, "--interval"),
) -> None:
    """Show system status, or one project's status."""
    state = _state(ctx)

    async def _run() -> Any:
        async with _client(state) as client:
            if project is None:
                return await client.system_status()
            return await client.get_project(project)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc

    if project is None:
        if state.json_output:
            _emit_json(result)
        else:
            engine = (result or {}).get("engine") or {}
            console.print(
                f"[bold]DevSandbox[/bold] version={result.get('version')}  "
                f"pressure={result.get('pressure')}  projects={result.get('projects')}\n"
                f"engine: running={len(engine.get('running') or [])} "
                f"ready={len(engine.get('ready_queue') or [])} "
                f"paused={engine.get('paused')} lease={engine.get('lease_holder')}"
            )
        return

    if state.json_output:
        _emit_json(result)
    else:
        _print_project(result)
    if watch:
        import time

        last_status = result.get("status")
        while not _failed_status(last_status) and last_status not in {"completed"}:
            time.sleep(max(0.2, interval))
            result = asyncio.run(_run())
            last_status = result.get("status")
            if not state.json_output:
                console.print(f"status={last_status}")
        _exit_if_failed(last_status)
    _exit_if_failed(result.get("status"))


@app.command()
def tasks(ctx: typer.Context, project: str = typer.Argument(...)) -> None:
    """List tasks (status, attempts, blocked/failure info)."""
    state = _state(ctx)

    async def _run() -> list[dict[str, Any]]:
        async with _client(state) as client:
            return await client.list_tasks(project)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        _print_task_table(result)


@app.command()
def logs(
    ctx: typer.Context,
    task: str = typer.Argument(...),
    attempt: int | None = typer.Option(None, "--attempt", "-a", help="Attempt number."),
    stream: str = typer.Option("agent", "--stream", help="agent|stdout|stderr"),
    tail: int | None = typer.Option(None, "--tail", help="Maximum lines to show."),
) -> None:
    """Show redacted logs for a task attempt."""
    state = _state(ctx)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            detail = await client.get_task(task)
            attempts = detail.get("attempts") or []
            number = attempt
            if number is None:
                if not attempts:
                    raise CliError("task has no attempts yet")
                number = max(int(item.get("number", 0)) for item in attempts)
            return await client.logs(task, number, stream=stream, tail=tail)

    try:
        chunk = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(chunk)
    else:
        typer.echo(chunk.get("content", ""))
        if chunk.get("truncated"):
            typer.secho(
                f"(truncated to {chunk.get('lines')} lines; file={chunk.get('path')})",
                fg="yellow",
                err=True,
            )


def _print_event(event: dict[str, Any]) -> None:
    console.print(
        f"{event.get('at')}  [bold]{event.get('type')}[/bold]  "
        f"project={event.get('project_id')} task={event.get('task_id') or '—'}"
    )
    payload = event.get("payload") or {}
    if payload:
        console.print(f"  {json.dumps(payload, sort_keys=True, default=str)}")


@app.command()
def events(
    ctx: typer.Context,
    project: str = typer.Argument(...),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events."),
    since: str | None = typer.Option(None, "--since", help="Event id to resume after."),
    limit: int = typer.Option(200, "--limit"),
) -> None:
    """Show the append-only audit trail."""
    state = _state(ctx)

    async def _run() -> None:
        async with _client(state) as client:
            if follow:
                async for event in client.stream_events(project, since=since):
                    if state.json_output:
                        _emit_json(event)
                    else:
                        _print_event(event)
                return
            rows = await client.events(project, since=since, limit=limit)
            if state.json_output:
                _emit_json(rows)
                return
            for event in rows:
                _print_event(event)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def report(
    ctx: typer.Context,
    project: str = typer.Argument(...),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write the report to a file."),
    fmt: str = typer.Option("markdown", "--format", help="markdown|json|html"),
) -> None:
    """Render the current project report (read-only and deterministic)."""
    state = _state(ctx)
    if fmt not in {"markdown", "json", "html"}:
        typer.secho("format must be markdown, json, or html", fg="red", err=True)
        raise typer.Exit(code=2)
    selected = "json" if state.json_output else fmt

    async def _run() -> Any:
        async with _client(state) as client:
            detail = await client.get_project(project)
            payload = await client.report(project, fmt=selected)
            return detail, payload

    try:
        detail, payload = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc

    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    if out is None:
        typer.echo(text)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        typer.secho(f"wrote report to {out}", fg="green", err=True)
    _exit_if_failed((detail or {}).get("status"))


@app.command("validate-spec")
def validate_spec(
    ctx: typer.Context,
    spec: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Validate a project spec offline (no database or network needed)."""
    state = _state(ctx)
    try:
        parsed = load_spec(spec)
    except SpecError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(parsed.model_dump(mode="json"))
        return
    console.print(
        _table(
            f"Spec: {spec}",
            ["Field", "Value"],
            [
                ["name", parsed.name],
                ["repo", parsed.repo.path or parsed.repo.url],
                ["ref", parsed.repo.ref],
                ["goals", len(parsed.goals)],
                ["requirements", len(parsed.requirements)],
                ["checks", len(parsed.validation.commands)],
            ],
        )
    )


@app.command()
def retry(
    ctx: typer.Context,
    task: str = typer.Argument(...),
    reset_budget: bool = typer.Option(
        False, "--reset-budget", help="Reset attempt counters (audited, explicit)."
    ),
) -> None:
    """Manually re-open a BLOCKED task (FAILED tasks require a replan)."""
    state = _state(ctx)

    async def _run() -> dict[str, Any]:
        async with _client(state) as client:
            return await client.retry(task, reset_budget=reset_budget)

    try:
        result = asyncio.run(_run())
    except CliError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc
    if state.json_output:
        _emit_json(result)
    else:
        console.print(
            f"retry: task={result.get('task_id')} status={result.get('status')} "
            f"reset_budget={result.get('reset_budget')}"
        )


def main() -> None:
    """Console-script entrypoint."""
    try:
        app()
    except DevSandboxError as exc:  # pragma: no cover - defensive
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc


__all__ = ["app", "main"]
