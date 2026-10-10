"""When each title was last watched (v0.10.0), for the Scanner's "Not
watched in N months" filters.

Plex, Jellyfin and Emby give each movie's and show's (or episode's) last play
and when it was added; each server's sync replaces its rows. A title counts
as not watched in N months when nothing was played for N months and it has
been in the library at least that long."""
from datetime import datetime, timezone

import aiosqlite

from backend.database import DB_PATH


def merge(activity: dict, folder: str, last_viewed: int | None, added: int | None) -> None:
    """Fold one item into `activity` (folder → (last viewed, added), epoch
    seconds): the latest play, the earliest add."""
    folder = folder.rstrip("/") + "/"
    old_last, old_added = activity.get(folder, (None, None))
    activity[folder] = (max(filter(None, (old_last, last_viewed)), default=None),
                        min(filter(None, (old_added, added)), default=None))


def iso_epoch(text) -> int | None:
    """A Jellyfin / Emby date ("2024-05-01T20:15:00.0000000Z") in epoch seconds."""
    try:
        return int(datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


async def store(server: str, activity: dict) -> int:
    """Replace `server`'s rows with `activity` (none: the server isn't set up)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("DELETE FROM watch_activity WHERE server = ?", (server,))
        await db.executemany(
            "INSERT INTO watch_activity (folder_path, server, last_viewed, added_at) VALUES (?, ?, ?, ?)",
            [(folder, server, last, added) for folder, (last, added) in activity.items()])
        await db.commit()
    return len(activity)


async def load(db) -> dict:
    """Folder → (last viewed, added) across the servers."""
    activity: dict = {}
    async with db.execute("SELECT folder_path, last_viewed, added_at FROM watch_activity") as cur:
        for folder, last, added in await cur.fetchall():
            merge(activity, folder, last, added)
    return activity
