"""Maintainerr (v0.10.0): the titles it is about to remove — its collections
("Leaving soon") — so Shrinkerr doesn't spend hours converting them.

Maintainerr's API (no auth): GET /api/collections lists the collections,
GET /api/collections/media?collectionId=N each one's items. An item's
mediaServerId (plexId before Maintainerr 3) is a Plex rating key or a
Jellyfin item id — resolved here to the title's folder through Plex /
Jellyfin, and stored in maintainerr_media."""
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import aiosqlite
import httpx

from backend.database import DB_PATH

_BATCH = 50


async def _settings(keys: tuple[str, ...]) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(keys))})",
                              keys) as cur:
            return {k: v for k, v in await cur.fetchall()}


async def fetch_collections(client: httpx.AsyncClient, url: str) -> list[tuple[str, str, list[str]]]:
    """(collection title, media server, item ids) for each active collection."""
    resp = await client.get(f"{url}/api/collections")
    resp.raise_for_status()
    out = []
    for collection in resp.json() or []:
        if collection.get("isActive") is False:
            continue
        media = await client.get(f"{url}/api/collections/media", params={"collectionId": collection["id"]})
        media.raise_for_status()
        ids = [str(m.get("mediaServerId") or m.get("plexId")) for m in media.json() or []
               if m.get("mediaServerId") or m.get("plexId")]
        out.append((collection.get("title") or "", (collection.get("mediaServerType") or "plex").lower(), ids))
    return out


async def _plex_folders(client: httpx.AsyncClient, ids: list[str]) -> list[str]:
    """Each Plex rating key's folders, as this container sees them."""
    from backend.plex import _get_plex_settings, _item_folders, _reverse_translate_path
    url, token, mapping = await _get_plex_settings()
    if not url or not token:
        raise RuntimeError("Plex isn't set up")
    folders: list[str] = []
    for i in range(0, len(ids), _BATCH):
        resp = await client.get(f"{url.rstrip('/')}/library/metadata/{','.join(ids[i:i + _BATCH])}",
                                headers={"X-Plex-Token": token, "Accept": "application/xml"})
        resp.raise_for_status()
        for item in ET.fromstring(resp.text):
            folders += [_reverse_translate_path(f, mapping) for f in _item_folders(item)]
    return folders


async def _jellyfin_folders(client: httpx.AsyncClient, ids: list[str]) -> list[str]:
    """Each Jellyfin item's folder: a show's own, a movie file's parent."""
    from backend import jellyfin
    s = await jellyfin._get_jellyfin_settings()
    url, key = (s.get("jellyfin_url") or "").rstrip("/"), s.get("jellyfin_api_key") or ""
    user = await jellyfin._get_user_id(url, key, s.get("jellyfin_user_id", "")) if url and key else ""
    if not user:
        raise RuntimeError("Jellyfin isn't set up")
    folders: list[str] = []
    for i in range(0, len(ids), _BATCH):
        resp = await client.get(f"{url}/Users/{user}/Items",
                                params={"Ids": ",".join(ids[i:i + _BATCH]), "Fields": "Path"},
                                headers=jellyfin._headers(key))
        resp.raise_for_status()
        for item in resp.json().get("Items", []):
            path = jellyfin._reverse_translate_path(item.get("Path") or "", s.get("jellyfin_path_mapping", ""))
            if path:
                folders.append(path if item.get("Type") in ("Series", "Season", "Folder", "BoxSet")
                               or not os.path.splitext(path)[1] else os.path.dirname(path))
    return folders


async def sync_maintainerr() -> int | None:
    """Store the folders of everything in Maintainerr's collections. None
    when Maintainerr isn't set up (its rows are cleared); raises when it or
    the media server can't be read (the stored rows are kept)."""
    url = ((await _settings(("maintainerr_url",))).get("maintainerr_url") or "").strip().rstrip("/")
    rows: list[tuple[str, str, str]] = []
    if url:
        now = datetime.now(timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=30) as client:
            for title, server, ids in await fetch_collections(client, url):
                if not ids:
                    continue
                resolve = _jellyfin_folders if server == "jellyfin" else _plex_folders
                for folder in await resolve(client, ids):
                    rows.append((folder.rstrip("/") + "/", title, now))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("DELETE FROM maintainerr_media")
        await db.executemany("INSERT OR REPLACE INTO maintainerr_media (folder_path, collection, synced_at) "
                             "VALUES (?, ?, ?)", rows)
        await db.commit()
    if url:
        print(f"[MAINTAINERR] {len(rows)} folder(s) in its collections", flush=True)
    return len(rows) if url else None


async def leaving_folders(db) -> list[str]:
    """The stored folders (trailing slash)."""
    async with db.execute("SELECT folder_path FROM maintainerr_media") as cur:
        return [r[0] for r in await cur.fetchall()]


def in_folders(path: str, folders: list[str]) -> bool:
    return any(path.startswith(f) for f in folders)
