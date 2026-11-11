"""Helpers for sharing host directories with sandbox containers.

Sandbox containers run as uid 1000 (see the sandbox manager), which is usually
not the uid of the orchestrator process (for example ``runner`` uid 1001 on
GitHub Actions, or the developer's own uid).  Workspaces are created on the
host, so they must explicitly be made writable for the container before they
are bind-mounted; otherwise a bind mount on Linux preserves host ownership and
the agent/validator cannot write anything.
"""

from __future__ import annotations

import os
from pathlib import Path


def make_writable(root: str | Path) -> None:
    """Make ``root`` writable by the container's uid 1000.

    Executable bits are preserved; only write bits are added.  Symlinks are not
    followed (the git manager already rejects escaping symlinks, but callers
    must not create a new escape via ``chmod``).
    """
    root_path = Path(root)
    if not root_path.exists():
        return
    for current, dirs, files in os.walk(root_path, followlinks=False):
        current_path = Path(current)
        try:
            mode = current_path.stat().st_mode
            os.chmod(current_path, mode | 0o777)
        except OSError:
            pass
        for name in dirs:
            target = current_path / name
            if target.is_symlink():
                continue
            try:
                mode = target.stat().st_mode
                os.chmod(target, mode | 0o777)
            except OSError:
                pass
        for name in files:
            target = current_path / name
            if target.is_symlink():
                continue
            try:
                mode = target.stat().st_mode
                os.chmod(target, mode | 0o666)
            except OSError:
                pass


__all__ = ["make_writable"]
