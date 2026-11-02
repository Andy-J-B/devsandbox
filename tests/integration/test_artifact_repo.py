"""Integration tests for the context artifact repository (plan 05)."""

from __future__ import annotations

from devsandbox.db.models import ContextArtifactModel
from devsandbox.db.repositories.artifact_repo import ArtifactRepo


def _artifact(
    artifact_id: str, kind: str, revision: str, *, stale: bool = False
) -> ContextArtifactModel:
    return ContextArtifactModel(
        id=artifact_id,
        project_id="proj-context",
        kind=kind,
        revision=revision,
        content_json={"revision": revision},
        content_hash="a" * 64,
        generated_by="analyzer",
        depends_on_paths_json=["pyproject.toml"],
        stale=stale,
    )


async def test_artifact_repo_roundtrip_and_history(db_session) -> None:
    repo = ArtifactRepo(db_session)
    first = await repo.create(_artifact("a1", "repo_profile", "rev-1"))
    second = await repo.create(_artifact("a2", "repo_profile", "rev-2"))
    other = await repo.create(_artifact("a3", "project_summary", "rev-2"))

    assert (await repo.get("a1")) is first
    assert await repo.get("missing") is None

    profiles = await repo.list_for_project("proj-context", "repo_profile")
    assert [row.id for row in profiles] == [second.id, first.id]

    everything = await repo.list_for_project("proj-context")
    assert {row.id for row in everything} == {first.id, second.id, other.id}

    latest = await repo.latest_for_project("proj-context", "repo_profile")
    assert latest is not None and latest.id == second.id
    assert await repo.latest_for_project("proj-context", "conventions") is None

    await repo.mark_stale(first.id)
    assert first.stale is True
    assert second.stale is False

    await repo.mark_stale(first.id, False)
    assert first.stale is False
