"""Jellyfin API integration — library sync, watched status, metadata, stream detection."""

import json
from typing import Optional

import aiosqlite
import httpx

from backend.database import DB_PATH


# ── Settings ──

async def _get_jellyfin_settings() -> dict:
    """Read Jellyfin connection settings from DB."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('jellyfin_url', 'jellyfin_api_key', 'jellyfin_user_id', 'jellyfin_path_mapping', "
            " 'jellyfin_empty_trash', 'jellyfin_pause_on_stream', "
            " 'jellyfin_pause_stream_threshold', 'jellyfin_pause_transcode_only')"
        ) as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
        return settings
    finally:
        await db.close()


def _headers(api_key: str) -> dict:
    """Build Jellyfin API auth headers."""
    return {
        "Authorization": f'MediaBrowser Token="{api_key}"',
        "Content-Type": "application/json",
    }


def _translate_path(file_path: str, mapping: str) -> str:
    """Translate container path → Jellyfin path using path mapping."""
    if not mapping:
        return file_path
    for pair in mapping.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        container, jellyfin = pair.split("=", 1)
        container = container.strip().rstrip("/")
        jellyfin = jellyfin.strip().rstrip("/")
        if file_path.startswith(container):
            return jellyfin + file_path[len(container):]
    return file_path


def _reverse_translate_path(jellyfin_path: str, mapping: str) -> str:
    """Translate Jellyfin path → container path."""
    if not mapping:
        return jellyfin_path
    for pair in mapping.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        container, jellyfin = pair.split("=", 1)
        container = container.strip().rstrip("/")
        jellyfin = jellyfin.strip().rstrip("/")
        if jellyfin_path.startswith(jellyfin):
            return container + jellyfin_path[len(jellyfin):]
    return jellyfin_path


# ── Connection Test ──

async def test_jellyfin_connection() -> dict:
    """Test Jellyfin connectivity and return server info."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    if not url or not api_key:
        return {"success": False, "error": "URL and API key required"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Get server info
            resp = await client.get(f"{url}/System/Info", headers=_headers(api_key))
            if resp.status_code != 200:
                return {"success": False, "error": f"Server returned {resp.status_code}"}
            info = resp.json()

            # Get libraries
            libs = await get_jellyfin_libraries(url, api_key)

            return {
                "success": True,
                "server_name": info.get("ServerName", "Jellyfin"),
                "version": info.get("Version", ""),
                "library_count": len(libs),
                "libraries": libs,
            }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── Libraries ──

async def get_jellyfin_libraries(url: str = None, api_key: str = None) -> list:
    """Fetch all Jellyfin library folders."""
    if not url or not api_key:
        settings = await _get_jellyfin_settings()
        url = settings.get("jellyfin_url", "").rstrip("/")
        api_key = settings.get("jellyfin_api_key", "")
    if not url or not api_key:
        return []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/Library/VirtualFolders", headers=_headers(api_key))
            if resp.status_code != 200:
                return []
            folders = resp.json()
            libs = []
            for f in folders:
                libs.append({
                    "id": f.get("ItemId", ""),
                    "title": f.get("Name", ""),
                    "type": f.get("CollectionType", "unknown"),
                    "paths": f.get("Locations", []),
                })
            return libs
    except Exception:
        return []


async def _get_user_id(url: str, api_key: str, stored_user_id: str = "") -> str:
    """Get the Jellyfin user ID. Uses stored value or fetches first admin user."""
    if stored_user_id:
        return stored_user_id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/Users", headers=_headers(api_key))
            if resp.status_code == 200:
                users = resp.json()
                # Prefer admin user, fall back to first user
                for u in users:
                    if u.get("Policy", {}).get("IsAdministrator"):
                        return u["Id"]
                if users:
                    return users[0]["Id"]
    except Exception:
        pass
    return ""


# ── Library Scan Trigger ──

async def trigger_jellyfin_scan(file_path: str) -> bool:
    """Trigger a library scan for the folder containing the converted file."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    mapping = settings.get("jellyfin_path_mapping", "")
    if not url or not api_key:
        return False

    import os
    folder = os.path.dirname(file_path)
    jellyfin_folder = _translate_path(folder, mapping)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{url}/Library/Refresh",
                headers=_headers(api_key),
            )
            print(f"[JELLYFIN] Library refresh triggered (status {resp.status_code})", flush=True)
            return resp.status_code in (200, 204)
    except Exception as exc:
        print(f"[JELLYFIN] Library refresh failed: {exc}", flush=True)
        return False


# ── Watch Status ──

async def get_watch_status_folders() -> dict:
    """Get watched/unwatched folder paths from Jellyfin.

    Returns: {"watched": [folder_paths], "unwatched": [folder_paths]}
    """
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    user_id = await _get_user_id(url, api_key, settings.get("jellyfin_user_id", ""))
    mapping = settings.get("jellyfin_path_mapping", "")
    if not url or not api_key or not user_id:
        return {"watched": [], "unwatched": []}

    watched_paths = []
    unwatched_paths = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get all items with played status
            params = {
                "userId": user_id,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "Path,MediaSources",
                "Limit": "100000",
            }

            # Watched items
            resp = await client.get(
                f"{url}/Users/{user_id}/Items",
                params={**params, "IsPlayed": "true"},
                headers=_headers(api_key),
            )
            if resp.status_code == 200:
                for item in resp.json().get("Items", []):
                    path = item.get("Path", "")
                    if path:
                        container_path = _reverse_translate_path(path, mapping)
                        import os
                        folder = os.path.dirname(container_path) + "/"
                        if folder not in watched_paths:
                            watched_paths.append(folder)

            # Unwatched items
            resp = await client.get(
                f"{url}/Users/{user_id}/Items",
                params={**params, "IsPlayed": "false"},
                headers=_headers(api_key),
            )
            if resp.status_code == 200:
                for item in resp.json().get("Items", []):
                    path = item.get("Path", "")
                    if path:
                        container_path = _reverse_translate_path(path, mapping)
                        import os
                        folder = os.path.dirname(container_path) + "/"
                        if folder not in unwatched_paths:
                            unwatched_paths.append(folder)

    except Exception as exc:
        print(f"[JELLYFIN] Watch status fetch failed: {exc}", flush=True)

    return {"watched": watched_paths, "unwatched": unwatched_paths}


# ── Metadata (Labels/Collections/Genres) ──

async def get_available_jellyfin_options() -> dict:
    """Fetch available genres, tags, and libraries from Jellyfin for rule autocomplete."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    user_id = await _get_user_id(url, api_key, settings.get("jellyfin_user_id", ""))
    if not url or not api_key:
        return {"genres": [], "tags": [], "libraries": []}

    genres = set()
    tags = set()
    libraries = await get_jellyfin_libraries(url, api_key)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Fetch genres
            resp = await client.get(
                f"{url}/Genres",
                params={"userId": user_id} if user_id else {},
                headers=_headers(api_key),
            )
            if resp.status_code == 200:
                for item in resp.json().get("Items", []):
                    genres.add(item.get("Name", ""))

            # Fetch tags (Jellyfin's equivalent of Plex labels)
            resp = await client.get(
                f"{url}/Tags",
                headers=_headers(api_key),
            )
            if resp.status_code == 200:
                for item in resp.json().get("Items", []):
                    tags.add(item.get("Name", ""))

    except Exception as exc:
        print(f"[JELLYFIN] Options fetch failed: {exc}", flush=True)

    return {
        "genres": sorted(g for g in genres if g),
        "tags": sorted(t for t in tags if t),
        "libraries": [{"title": lib["title"], "type": lib["type"], "paths": lib["paths"]} for lib in libraries],
    }


async def get_folders_by_genre(genre: str) -> list:
    """Get container folder paths for items with a specific genre."""
    return await _get_folders_by_filter("Genres", genre)


async def get_folders_by_tag(tag: str) -> list:
    """Get container folder paths for items with a specific tag (Jellyfin's equivalent of labels)."""
    return await _get_folders_by_filter("Tags", tag)


async def get_folders_by_library(library_name: str) -> list:
    """Get all folder paths in a specific library."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    mapping = settings.get("jellyfin_path_mapping", "")
    if not url or not api_key:
        return []

    libs = await get_jellyfin_libraries(url, api_key)
    for lib in libs:
        if lib["title"].lower() == library_name.lower():
            return [_reverse_translate_path(p, mapping) + "/" for p in lib.get("paths", [])]
    return []


async def _get_folders_by_filter(filter_type: str, filter_value: str) -> list:
    """Generic folder fetcher — get container paths for items matching a Jellyfin filter."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    user_id = await _get_user_id(url, api_key, settings.get("jellyfin_user_id", ""))
    mapping = settings.get("jellyfin_path_mapping", "")
    if not url or not api_key:
        return []

    folders = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "Path",
                filter_type: filter_value,
                "Limit": "100000",
            }
            if user_id:
                params["userId"] = user_id

            resp = await client.get(
                f"{url}/Items",
                params=params,
                headers=_headers(api_key),
            )
            if resp.status_code == 200:
                for item in resp.json().get("Items", []):
                    path = item.get("Path", "")
                    if path:
                        import os
                        container_path = _reverse_translate_path(path, mapping)
                        folder = os.path.dirname(container_path) + "/"
                        if folder not in folders:
                            folders.append(folder)
    except Exception as exc:
        print(f"[JELLYFIN] Filter fetch ({filter_type}={filter_value}) failed: {exc}", flush=True)

    return folders


# ── Metadata Cache Sync ──

async def sync_jellyfin_metadata_cache() -> dict:
    """Sync Jellyfin metadata (tags, genres, libraries, watch status) into plex_metadata_cache.

    Reuses the same cache table as Plex for unified rule resolution.
    Uses metadata_type values: 'jellyfin_tag', 'genre', 'library', 'watch_status'
    """
    from datetime import datetime, timezone

    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    if not url or not api_key:
        return {"tags_synced": 0, "genres_synced": 0, "libraries_synced": 0, "watch_synced": 0}

    # Load enabled rules to find which metadata types are needed
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM encoding_rules WHERE enabled = 1") as cur:
            rules = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    needed_tags = set()
    needed_genres = set()
    needed_libraries = set()
    need_watch = False

    for rule in rules:
        raw = rule.get("match_conditions")
        if not raw:
            continue
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            conditions = parsed.get("conditions", []) if isinstance(parsed, dict) else parsed
        except Exception:
            continue
        for cond in conditions:
            ct = cond.get("type", "")
            cv = cond.get("value", "")
            if ct == "jellyfin_tag" and cv:
                needed_tags.add(cv)
            elif ct == "genre" and cv:
                needed_genres.add(cv)
            elif ct == "library" and cv:
                needed_libraries.add(cv)
            elif ct == "jellyfin_watched":
                need_watch = True

    now = datetime.now(timezone.utc).isoformat()
    rows_to_insert = []
    tags_synced = 0
    genres_synced = 0
    libraries_synced = 0
    watch_synced = 0

    # Fetch tag folders
    for tag in needed_tags:
        folders = await get_folders_by_tag(tag)
        for folder in folders:
            rows_to_insert.append((folder, "jellyfin_tag", tag, now))
            tags_synced += 1

    # Fetch genre folders
    for genre in needed_genres:
        folders = await get_folders_by_genre(genre)
        for folder in folders:
            rows_to_insert.append((folder, "genre", genre, now))
            genres_synced += 1

    # Fetch library folders
    for lib_name in needed_libraries:
        folders = await get_folders_by_library(lib_name)
        for folder in folders:
            rows_to_insert.append((folder, "library", lib_name, now))
            libraries_synced += 1

    # Fetch watch status
    if need_watch:
        watch = await get_watch_status_folders()
        for folder in watch.get("watched", []):
            rows_to_insert.append((folder, "watch_status", "watched", now))
            watch_synced += 1
        for folder in watch.get("unwatched", []):
            rows_to_insert.append((folder, "watch_status", "unwatched", now))
            watch_synced += 1

    # Write to cache (append to existing Plex cache — don't delete Plex entries)
    db = await aiosqlite.connect(DB_PATH)
    try:
        # Clear only Jellyfin-sourced entries
        await db.execute("DELETE FROM plex_metadata_cache WHERE metadata_type = 'jellyfin_tag'")
        # Clear genre/library/watch entries that will be re-synced
        # (Plex and Jellyfin both contribute to these — we append both)
        for row in rows_to_insert:
            await db.execute(
                "INSERT OR REPLACE INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                "VALUES (?, ?, ?, ?)",
                row,
            )
        await db.commit()
    finally:
        await db.close()

    print(f"[JELLYFIN] Metadata sync: {tags_synced} tags, {genres_synced} genres, {libraries_synced} libraries, {watch_synced} watch", flush=True)
    return {
        "tags_synced": tags_synced,
        "genres_synced": genres_synced,
        "libraries_synced": libraries_synced,
        "watch_synced": watch_synced,
    }


# ── Active Streams ──

async def get_active_streams() -> dict:
    """Check for active Jellyfin streams (for stream-aware encoding pausing)."""
    settings = await _get_jellyfin_settings()
    url = settings.get("jellyfin_url", "").rstrip("/")
    api_key = settings.get("jellyfin_api_key", "")
    if not url or not api_key:
        return {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/Sessions", headers=_headers(api_key))
            if resp.status_code != 200:
                return {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}

            sessions_data = resp.json()
            active = []
            transcoding = 0
            direct = 0

            for session in sessions_data:
                now_playing = session.get("NowPlayingItem")
                if not now_playing:
                    continue

                play_state = session.get("PlayState", {})
                transcode_info = session.get("TranscodingInfo", {})
                is_transcoding = bool(transcode_info and transcode_info.get("IsVideoDirect") is False)

                if is_transcoding:
                    transcoding += 1
                else:
                    direct += 1

                active.append({
                    "title": now_playing.get("Name", ""),
                    "type": now_playing.get("Type", ""),
                    "user": session.get("UserName", ""),
                    "is_transcoding": is_transcoding,
                })

            return {
                "total": len(active),
                "transcoding": transcoding,
                "direct": direct,
                "sessions": active,
            }
    except Exception as exc:
        print(f"[JELLYFIN] Stream check failed: {exc}", flush=True)
        return {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}
