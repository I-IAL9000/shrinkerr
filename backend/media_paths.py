"""Shared path-containment helpers for user-supplied filesystem inputs.

Every endpoint that accepts a file/directory path from an HTTP request
MUST run the input through these helpers before using it. The previous
`startswith(media_dir + "/")` pattern used at call sites was trivially
bypassable (`"/media/../etc/hostname"` literally starts with `/media/`),
and path-separator-free strings (e.g. a symlink farm) would escape too.

The implementation is intentionally simple: resolve both sides (follow
symlinks, normalise `..`), then use `os.path.commonpath` which returns
the longest shared prefix and only counts something as an ancestor when
the component boundary matches. That correctly rejects
`/media/../etc/hostname` (resolves to `/etc/hostname`) and
`/media-other/file.mkv` (common path is `/` or the next-up, not
`/media`).
"""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

from backend.database import DB_PATH


async def load_media_dirs() -> list[str]:
    """Return the list of configured media directory paths.

    All DB call sites that need the current media-dir allowlist funnel
    through this so the resolution/validation behaviour stays consistent.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            rows = await cur.fetchall()
            return [r["path"] for r in rows]
    finally:
        await db.close()


def _resolve(path: str) -> str:
    """`Path.resolve(strict=False)` wrapper that doesn't raise on missing."""
    try:
        return str(Path(path).resolve(strict=False))
    except (OSError, RuntimeError):
        return str(Path(path).absolute())


def is_within(child_path: str, parent_path: str) -> bool:
    """True when `child_path` lives inside `parent_path` after resolution."""
    child = _resolve(child_path)
    parent = _resolve(parent_path)
    try:
        common = os.path.commonpath([child, parent])
    except ValueError:
        return False  # different drives / mount points on Windows
    return common == parent


def is_in_any(child_path: str, parents: list[str]) -> bool:
    """True when `child_path` is inside any of the supplied parent roots."""
    return any(is_within(child_path, p) for p in parents)


async def require_in_media_dirs(path: str, *, label: str = "Path") -> str:
    """Raise `HTTPException(403)` if `path` is not under a configured media dir.

    Returns the resolved (canonical) form of `path` so the caller can use
    the safe version everywhere downstream — avoids accidentally doing a
    second DB lookup or subprocess call with the pre-resolution string
    that still contains traversal components.
    """
    # Local import keeps this module FastAPI-agnostic (we'd like to be able
    # to reuse the helpers in CLI/test contexts without pulling fastapi in).
    from fastapi import HTTPException

    dirs = await load_media_dirs()
    if not dirs:
        raise HTTPException(
            status_code=400,
            detail="No media directories configured — refusing to operate on arbitrary paths",
        )
    resolved = _resolve(path)
    if not is_in_any(resolved, dirs):
        raise HTTPException(
            status_code=403,
            detail=f"{label} is not under a configured media directory",
        )
    return resolved
