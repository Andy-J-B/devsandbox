"""Project summary determinism, caching and LLM budget tests (plan 05)."""

from __future__ import annotations

from tests.context_helpers import build_fixture_repo

from devsandbox.context.analyzer import analyze_repo
from devsandbox.context.summary import (
    NullSummarizer,
    SummaryService,
    render_profile_summary,
)


class CountingSummarizer:
    """Test summarizer that counts calls and optionally returns a custom text."""

    def __init__(self, text: str | None = "LLM summary") -> None:
        self.calls = 0
        self.text = text

    async def summarize(self, profile, readme_excerpt: str) -> str | None:
        self.calls += 1
        return self.text


async def test_deterministic_summary_and_size_cap(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    profile = await analyze_repo(fixture.handle, fixture.revision)

    first = render_profile_summary(profile, profile.readme_excerpt)
    second = render_profile_summary(profile, profile.readme_excerpt)
    capped = render_profile_summary(profile, profile.readme_excerpt, max_chars=120)

    assert first == second
    assert "Primary language: Python" in first
    assert "pytest" in first
    assert "FastAPI" in first
    assert len(capped) <= 120


async def test_null_summarizer_makes_no_model_calls(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    profile = await analyze_repo(fixture.handle, fixture.revision)
    service = SummaryService(NullSummarizer())

    text = await service.summarize(profile, profile.readme_excerpt)

    assert text == render_profile_summary(profile, profile.readme_excerpt)
    assert service.last_generated_by == "analyzer"


async def test_configured_summarizer_is_cached_by_revision(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    profile = await analyze_repo(fixture.handle, fixture.revision)
    summarizer = CountingSummarizer("Custom summary")
    service = SummaryService(summarizer)

    first = await service.summarize(profile, profile.readme_excerpt)
    second = await service.summarize(profile, profile.readme_excerpt)

    assert first == "Custom summary"
    assert second == first
    assert summarizer.calls == 1
    assert service.last_generated_by == "llm"


async def test_token_budget_falls_back_to_deterministic_summary(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    profile = await analyze_repo(fixture.handle, fixture.revision)
    summarizer = CountingSummarizer("Custom summary")
    service = SummaryService(summarizer, token_budget=0)

    text = await service.summarize(profile, profile.readme_excerpt)

    assert summarizer.calls == 0
    assert text == render_profile_summary(profile, profile.readme_excerpt)
    assert service.last_generated_by == "analyzer"
