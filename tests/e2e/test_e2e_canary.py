"""E2E canary: a leaked secret must not survive any persisted surface.

The agent deliberately prints a registered secret.  The orchestrator must
scrub it from prompts, logs, database rows, reports, and the project's Git
history before the run finishes.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from tests.e2e.agents import Script
from tests.e2e.harness import (
    FixedPlanner,
    assert_no_secret_in_persisted_state,
    task_draft,
)

from devsandbox.common.redaction import register_secret, reset_redactor
from devsandbox.reporting.builder import build_report
from devsandbox.reporting.render import render_html, render_json, render_markdown


@pytest.mark.e2e
async def test_canary_secret_is_absent_from_prompts_logs_reports_db_and_git(e2e_env) -> None:
    env = e2e_env
    canary = "canary-e2e-secret-7f3a2b1c4d5e6f708192"
    register_secret(canary)
    try:
        agent = env.agent(
            default=Script(mode="leak-secret"),
            secret_files={"DEVSANDBOX_FAKE_SECRET": SecretStr(canary)},
        )
        planner = FixedPlanner(lambda spec: [task_draft("leak-secret", max_retries=0)])
        ctx = env.context(agent, planner=planner)
        project_id, _ = await ctx.submit_and_run(
            env.spec(name="e16-canary", max_retries=0, task_timeout_seconds=60),
            timeout=180,
        )

        await assert_no_secret_in_persisted_state(ctx, project_id, canary)

        report = await build_report(
            project_id,
            ctx.session_factory,
            settings=env.settings,
            git=ctx.git,
        )
        for rendered in (render_json(report), render_markdown(report), render_html(report)):
            assert canary not in rendered
    finally:
        reset_redactor()
