"""Read-only access to a revision of a bare git repository.

The context manager never checks out or executes repository code.  All data is
read through plumbing commands (``git ls-tree``, ``git show``, ``git grep`` and
``git diff``) against the canonical bare repository.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..gitmgr.command import GitCommandError, run_git

# Directories that never contribute to structure/language analysis.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "bin",
        "build",
        "coverage",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "obj",
        "out",
        "Pods",
        "site-packages",
        "target",
        "vendor",
        "venv",
    }
)

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".7z",
        ".a",
        ".avi",
        ".bmp",
        ".class",
        ".dll",
        ".dylib",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".lockb",
        ".mov",
        ".mp3",
        ".mp4",
        ".o",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".so",
        ".tar",
        ".tgz",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".zip",
    }
)

_SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "credentials",
    "credentials.*",
    "secrets.*",
)


@dataclass(frozen=True)
class TreeEntry:
    """One tracked file in a revision."""

    path: str
    mode: str
    size: int


@dataclass(frozen=True)
class RevisionDiff:
    """Changed / structural paths between two revisions."""

    changed: tuple[str, ...]
    added_or_deleted: tuple[str, ...]


def is_binary_path(path: str) -> bool:
    """Return True when ``path`` has a commonly-binary extension."""
    return PurePosixPath(path).suffix.lower() in _BINARY_EXTENSIONS


def is_secret_path(path: str) -> bool:
    """Return True for paths that may contain credentials/secrets."""
    name = PurePosixPath(path).name
    if name == ".env":
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_PATH_PATTERNS)


def is_ignored_path(path: str) -> bool:
    """Return True when any path component is an ignored build/vendor directory."""
    return any(part in IGNORED_DIRS for part in PurePosixPath(path).parts)


def _parse_ls_tree(output: str) -> tuple[TreeEntry, ...]:
    entries: list[TreeEntry] = []
    for record in output.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) < 4:
            continue
        mode, kind, _sha, size_text = fields[0], fields[1], fields[2], fields[3]
        if kind != "blob":
            continue
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        entries.append(TreeEntry(path=path, mode=mode, size=size))
    entries.sort(key=lambda entry: entry.path)
    return tuple(entries)


class GitSnapshot:
    """A read-only snapshot of one revision in a repository."""

    def __init__(self, repo_path: str, revision: str, entries: tuple[TreeEntry, ...]) -> None:
        self._repo_path = repo_path
        self._revision = revision
        self._entries = entries

    @classmethod
    async def open(cls, repo_path: str, revision: str) -> GitSnapshot:
        """List the tree for ``revision`` (no checkout, no repository code)."""
        result = await run_git(
            ["-c", "core.quotepath=false", "ls-tree", "-r", "-l", "-z", revision],
            cwd=repo_path,
        )
        return cls(repo_path, revision, _parse_ls_tree(result.stdout))

    @property
    def revision(self) -> str:
        """The stamped revision."""
        return self._revision

    @property
    def entries(self) -> tuple[TreeEntry, ...]:
        """All tracked blobs, sorted by path."""
        return self._entries

    @property
    def files(self) -> tuple[str, ...]:
        """All tracked file paths, sorted."""
        return tuple(entry.path for entry in self._entries)

    def size_of(self, path: str) -> int | None:
        """Return the blob size for ``path`` if it exists."""
        for entry in self._entries:
            if entry.path == path:
                return entry.size
        return None

    def exists(self, path: str) -> bool:
        """Return True when ``path`` is tracked in this revision."""
        return self.size_of(path) is not None

    def glob(self, pattern: str) -> tuple[str, ...]:
        """Match tracked paths using :mod:`fnmatch` semantics."""
        return tuple(path for path in self.files if fnmatch.fnmatch(path, pattern))

    def under(self, directory: str) -> tuple[str, ...]:
        """Return tracked paths below ``directory`` (``""`` means everything)."""
        prefix = directory.rstrip("/") + "/" if directory else ""
        if not prefix:
            return self.files
        return tuple(path for path in self.files if path.startswith(prefix))

    async def read_text(self, path: str, max_chars: int = 60_000) -> str | None:
        """Read a text blob, returning ``None`` for binaries/missing/large files."""
        if not self.exists(path) or is_binary_path(path):
            return None
        size = self.size_of(path) or 0
        if size > 2_000_000:
            return None
        try:
            result = await run_git(["show", f"{self._revision}:{path}"], cwd=self._repo_path)
        except GitCommandError:
            return None
        text = result.stdout
        if "\0" in text:
            return None
        if len(text) > max_chars:
            return text[:max_chars]
        return text

    async def read_texts(
        self, paths: list[str] | tuple[str, ...], *, max_chars_per_file: int = 60_000
    ) -> dict[str, str]:
        """Read several text blobs, skipping binaries/missing/secret-bearing files."""
        contents: dict[str, str] = {}
        for path in sorted(set(paths)):
            if is_secret_path(path):
                continue
            text = await self.read_text(path, max_chars=max_chars_per_file)
            if text is not None:
                contents[path] = text
        return contents

    async def line_counts(self) -> dict[str, int]:
        """Return text line counts via ``git grep -I -c`` (binaries ignored)."""
        try:
            result = await run_git(
                ["grep", "-I", "-c", "", self._revision],
                cwd=self._repo_path,
                check=False,
            )
        except GitCommandError:  # pragma: no cover - defensive
            return {}
        if result.returncode not in (0, 1):
            return {}
        counts: dict[str, int] = {}
        prefix = f"{self._revision}:"
        for line in result.stdout.splitlines():
            if not line.startswith(prefix):
                continue
            body = line[len(prefix) :]
            path, separator, count_text = body.rpartition(":")
            if not separator:
                continue
            try:
                counts[path] = int(count_text)
            except ValueError:
                continue
        return counts


async def diff_revisions(repo_path: str, old_revision: str, new_revision: str) -> RevisionDiff:
    """Return changed paths and added/deleted paths between two revisions."""
    try:
        changed_result = await run_git(
            ["diff", "--name-only", "-z", old_revision, new_revision],
            cwd=repo_path,
        )
        structural_result = await run_git(
            ["diff", "--name-only", "-z", "--diff-filter=AD", old_revision, new_revision],
            cwd=repo_path,
        )
    except GitCommandError:
        return RevisionDiff(changed=(new_revision,), added_or_deleted=(new_revision,))
    changed = tuple(sorted(part for part in changed_result.stdout.split("\0") if part))
    structural = tuple(sorted(part for part in structural_result.stdout.split("\0") if part))
    return RevisionDiff(changed=changed, added_or_deleted=structural)


__all__ = [
    "IGNORED_DIRS",
    "GitSnapshot",
    "RevisionDiff",
    "TreeEntry",
    "diff_revisions",
    "is_binary_path",
    "is_ignored_path",
    "is_secret_path",
]
