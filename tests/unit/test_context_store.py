"""Artifact store versioning, hashing, and staleness matrix tests (plan 05)."""

from __future__ import annotations

import json

from tests.context_helpers import (
    InMemoryArtifactRepository,
    build_fixture_repo,
    commit_file,
)

from devsandbox.context.store import (
    ArtifactKind,
    ArtifactStore,
    GeneratedArtifact,
    canonical_json,
    content_hash,
)


def _store(fixture) -> tuple[ArtifactStore, InMemoryArtifactRepository]:
    repository = InMemoryArtifactRepository()
    store = ArtifactStore(repository, repo_lookup=lambda _project_id: fixture.handle)
    return store, repository


async def test_put_get_latest_and_content_hash(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "node-jest")
    store, _ = _store(fixture)

    artifact = await store.put(
        ArtifactKind.BUILD_TEST_COMMANDS,
        fixture.name,
        fixture.revision,
        {"test": ["npm test"]},
        depends_on_paths=["package.json"],
    )

    assert artifact.revision == fixture.revision
    assert artifact.content_hash == content_hash({"test": ["npm test"]})
    assert artifact.depends_on_paths == ["package.json"]
    assert await store.get(artifact.id) == artifact
    assert await store.latest(fixture.name, ArtifactKind.BUILD_TEST_COMMANDS) == artifact
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert json.loads(canonical_json({"a": 2})) == {"a": 2}


async def test_get_or_generate_caches_same_revision(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "python-pytest")
    store, _ = _store(fixture)
    calls: list[str] = []

    async def generator(project_id: str, revision: str) -> GeneratedArtifact:
        calls.append(revision)
        return GeneratedArtifact(content={"revision": revision}, depends_on_paths=["*"])

    store.register_generator(ArtifactKind.REPO_STRUCTURE, generator)
    first = await store.get_or_generate(
        ArtifactKind.REPO_STRUCTURE, fixture.handle, fixture.revision
    )
    second = await store.get_or_generate(
        ArtifactKind.REPO_STRUCTURE, fixture.handle, fixture.revision
    )

    assert first.id == second.id
    assert calls == [fixture.revision]


async def test_staleness_matrix_for_command_artifacts(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "node-jest")
    store, _ = _store(fixture)
    artifact = await store.put(
        ArtifactKind.BUILD_TEST_COMMANDS,
        fixture.name,
        fixture.revision,
        {"test": ["npm test"]},
        depends_on_paths=["package.json"],
    )

    # Unchanged revision is fresh.
    assert not await store.is_stale(artifact.id, fixture.revision)

    # README-only change does not invalidate command artifacts.
    readme_revision = commit_file(fixture, "README.md", "# Fixture Node\n\nChanged.\n")
    assert not await store.is_stale(artifact.id, readme_revision)

    # A manifest change invalidates command artifacts.
    package = (fixture.source / "package.json").read_text(encoding="utf-8")
    package = package.replace('"private": true', '"private": true, "description": "changed"')
    package_revision = commit_file(fixture, "package.json", package)
    assert await store.is_stale(artifact.id, package_revision)


async def test_structural_artifacts_invalidate_on_add_or_delete(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "node-jest")
    store, _ = _store(fixture)
    artifact = await store.put(
        ArtifactKind.REPO_STRUCTURE,
        fixture.name,
        fixture.revision,
        {"tree_summary": []},
        depends_on_paths=["*"],
    )

    readme_revision = commit_file(fixture, "README.md", "# Changed\n")
    assert not await store.is_stale(artifact.id, readme_revision)

    new_file_revision = commit_file(fixture, "src/extra.ts", "export const x = 1;\n")
    assert await store.is_stale(artifact.id, new_file_revision)


async def test_get_or_generate_marks_old_version_stale_and_keeps_history(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "node-jest")
    store, _ = _store(fixture)
    calls: list[str] = []

    async def generator(project_id: str, revision: str) -> GeneratedArtifact:
        calls.append(revision)
        return GeneratedArtifact(content={"revision": revision}, depends_on_paths=["package.json"])

    store.register_generator(ArtifactKind.BUILD_TEST_COMMANDS, generator)
    first = await store.get_or_generate(
        ArtifactKind.BUILD_TEST_COMMANDS, fixture.handle, fixture.revision
    )
    package_revision = commit_file(
        fixture, "package.json", '{"name": "fixture-node", "scripts": {"test": "jest"}}\n'
    )
    second = await store.get_or_generate(
        ArtifactKind.BUILD_TEST_COMMANDS, fixture.handle, package_revision
    )

    assert first.id != second.id
    assert second.revision == package_revision
    assert calls == [fixture.revision, package_revision]
    history = await store.history(fixture.name, ArtifactKind.BUILD_TEST_COMMANDS)
    assert [item.id for item in history] == [second.id, first.id]
    stale_first = await store.get(first.id)
    assert stale_first is not None and stale_first.stale


async def test_missing_artifact_is_stale(tmp_path) -> None:
    fixture = build_fixture_repo(tmp_path, "go")
    store, _ = _store(fixture)
    assert await store.is_stale("does-not-exist", fixture.revision)
