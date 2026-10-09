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

import asyncio
import os
import time
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


def _ancestor_that_is(path: str, target: str) -> Path | None:
    """`path` itself, or its ancestor, that is the folder `target` — None
    when `path` isn't inside `target`. Folders are compared by device and
    inode as well as by name: on a case-insensitive filesystem or SMB share
    one folder has many spellings, and "/media/movies" slipped past a
    "/Media/Movies" check (v0.10.0)."""
    resolved = Path(_resolve(path))
    if is_within(path, target):
        return Path(_resolve(target))
    try:
        target_stat = os.stat(target)
    except OSError:
        return None
    for candidate in (resolved, *resolved.parents):
        try:
            if os.path.samestat(os.stat(candidate), target_stat):
                return candidate
        except OSError:
            continue
    return None


def backup_folder_conflict(folder: str, media_dirs: list[str]) -> str | None:
    """The media folder a custom backup folder overlaps, if any (v0.10.0).

    Backup expiry and "Delete backups" remove old files from every folder
    inside the backup folder: pointed at (or above) the library, that's the
    media itself. A hidden folder inside a media folder is fine — scans
    skip it, so only backups end up there."""
    for media_dir in media_dirs:
        if _ancestor_that_is(media_dir, folder) is not None:  # the same folder, or the library is inside it
            return media_dir
        inside = _ancestor_that_is(folder, media_dir)
        if inside is not None:
            rel = Path(_resolve(folder)).relative_to(inside)
            if not any(part.startswith(".") for part in rel.parts):
                return media_dir
    return None


# SC-25 / SC-26 (v0.10.0): media_dir_label_for opened a database connection
# and resolved the path and every media folder (realpath: syscalls on the
# NAS) on the event loop — for every file of a scan, every watcher file and
# every poster. The folders are now loaded once (cached 15 s, and dropped
# when one is added, edited or removed), each root resolved once in a
# thread, and paths matched by string prefix.
_LABEL_CACHE_TTL = 15.0
_label_cache: dict = {"db": None, "at": 0.0, "index": []}


def _label_index(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(prefix ending in "/", label) for every media folder, as configured
    and with symlinks resolved, longest first. Blocking: run in a thread."""
    index = []
    for path, label in rows:
        for form in {path.rstrip("/"), _resolve(path).rstrip("/")}:
            if form:
                index.append((form + "/", label or ""))
    index.sort(key=lambda pair: len(pair[0]), reverse=True)
    return index


def invalidate_media_dir_cache() -> None:
    """Call after adding, editing or removing a media folder."""
    _label_cache["at"] = 0.0


async def _media_dir_label_index() -> list[tuple[str, str]]:
    if _label_cache["db"] == DB_PATH and time.monotonic() - _label_cache["at"] < _LABEL_CACHE_TTL:
        return _label_cache["index"]
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path, label FROM media_dirs") as cur:
            rows = [(r["path"], r["label"] or "") for r in await cur.fetchall()]
    finally:
        await db.close()
    index = await asyncio.to_thread(_label_index, rows)
    _label_cache.update(db=DB_PATH, at=time.monotonic(), index=index)
    return index


def label_in_index(path: str, index: list[tuple[str, str]]) -> str | None:
    """The label of the media folder `path` is in (the deepest one when
    folders are nested), or None."""
    candidate = path if path.endswith("/") else path + "/"
    for prefix, label in index:
        if candidate.startswith(prefix):
            return label or None
    return None


async def media_dir_label_for(path: str) -> str | None:
    """Return the `media_dirs.label` for the directory `path` lives inside,
    or None if it isn't under any configured root.

    Used to gate metadata lookups: directories the user marked "Other"
    contain non-tmdb content (home videos, music, lectures, miscellaneous
    rips) and shouldn't be matched against TMDB's movie/TV catalogue —
    the matches would be spurious and pollute scan_results with wrong
    posters / wrong original-language tags. v0.3.33+.
    """
    return label_in_index(path, await _media_dir_label_index())


async def is_other_typed_dir(path: str) -> bool:
    """Convenience for the common case: True iff `path` lives inside a
    media dir whose label resolves to "Other" (case-insensitive)."""
    label = await media_dir_label_for(path)
    return bool(label and label.strip().lower() == "other")


async def require_in_media_dirs(path: str, *, label: str = "Path") -> str:
    """Raise `HTTPException(403)` if `path` is not under a configured media dir.

    Returns the resolved (canonical) form of `path` so the caller can use
    the safe version everywhere downstream — avoids accidentally doing a
    second DB lookup or subprocess call with the pre-resolution string
    that still contains traversal components.
    """
    # Local import keeps this module FastAPI-agnostic (we'd like to be able
    # to reuse the helpers in CLI/test contexts without pulling fastapi in).
    from backend.api_errors import ApiError

    dirs = await load_media_dirs()
    if not dirs:
        raise ApiError(
            status_code=400,
            detail="No media directories configured — refusing to operate on arbitrary paths",
            code="media.noMediaDirsRefusing",
        )
    resolved = _resolve(path)
    if not is_in_any(resolved, dirs):
        raise ApiError(
            status_code=403,
            detail=f"{label} is not under a configured media directory",
            code="media.pathNotUnderMediaDir",
            params={"label": label},
        )
    return resolved
