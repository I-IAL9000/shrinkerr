"""Plex integration — trigger partial library scans after conversion."""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx

from backend.database import DB_PATH


async def _get_plex_settings() -> tuple[str, str, str]:
    """Read Plex URL, token, and path mapping from the settings DB. Returns (url, token, path_mapping)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token', 'plex_path_mapping')"
        ) as cur:
            rows = await cur.fetchall()
            for r in rows:
                settings[r["key"]] = r["value"]
        return settings.get("plex_url", ""), settings.get("plex_token", ""), settings.get("plex_path_mapping", "")
    finally:
        await db.close()


def _translate_path(file_path: str, path_mapping: str) -> str:
    """Translate a container path to a Plex-visible path using the mapping.

    path_mapping format: "/media=/srv/media" (container_path=host_path)
    Multiple mappings separated by semicolons.
    """
    if not path_mapping:
        return file_path
    for mapping in path_mapping.split(";"):
        mapping = mapping.strip()
        if "=" not in mapping:
            continue
        container_prefix, host_prefix = mapping.split("=", 1)
        container_prefix = container_prefix.rstrip("/")
        host_prefix = host_prefix.rstrip("/")
        if file_path.startswith(container_prefix + "/") or file_path == container_prefix:
            translated = host_prefix + file_path[len(container_prefix):]
            return translated
    return file_path


async def get_plex_libraries(url: str, token: str) -> list[dict]:
    """Fetch all Plex library sections with their paths."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{url.rstrip('/')}/library/sections",
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
        )
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    libraries = []
    for directory in root.findall(".//Directory"):
        section_id = directory.get("key")
        title = directory.get("title", "")
        section_type = directory.get("type", "")
        paths = []
        for location in directory.findall("Location"):
            path = location.get("path")
            if path:
                paths.append(path)
        libraries.append({
            "id": section_id,
            "title": title,
            "type": section_type,
            "paths": paths,
        })
    return libraries


def find_section_for_path(file_path: str, libraries: list[dict]) -> Optional[tuple[str, str]]:
    """Find which Plex library section contains the given file path.

    Returns (section_id, matched_library_path) or None.
    """
    file_path = str(Path(file_path).resolve())
    best_match = None
    best_len = 0

    for lib in libraries:
        for lib_path in lib["paths"]:
            lib_path_str = str(Path(lib_path).resolve())
            if file_path.startswith(lib_path_str + "/") or file_path.startswith(lib_path_str):
                if len(lib_path_str) > best_len:
                    best_match = (lib["id"], lib_path_str)
                    best_len = len(lib_path_str)
    return best_match


async def trigger_plex_scan(file_path: str) -> str | None:
    """Trigger a partial Plex library scan for the folder containing the given file.

    Returns the section_id if scan was triggered, None otherwise.
    """
    url, token, path_mapping = await _get_plex_settings()
    if not url or not token:
        return None

    # Translate container path to Plex-visible path
    file_path = _translate_path(file_path, path_mapping)

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception as exc:
        print(f"[PLEX] Failed to fetch libraries: {exc}", flush=True)
        return None

    match = find_section_for_path(file_path, libraries)
    if not match:
        print(f"[PLEX] No library section found for: {file_path}", flush=True)
        return None

    section_id, _ = match
    folder_path = str(Path(file_path).parent)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/library/sections/{section_id}/refresh",
                params={"path": folder_path},
                headers={"X-Plex-Token": token},
            )
            resp.raise_for_status()
        print(f"[PLEX] Triggered scan for section {section_id}, folder: {folder_path}", flush=True)
        return section_id
    except Exception as exc:
        print(f"[PLEX] Scan trigger failed: {exc}", flush=True)
        return None


async def empty_plex_trash(section_id: str) -> bool:
    """Send 'Empty Trash' to a specific Plex library section.

    Returns True if trash was emptied, False if Plex isn't configured or request failed.
    """
    url, token, _ = await _get_plex_settings()
    if not url or not token:
        return False

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"{url.rstrip('/')}/library/sections/{section_id}/emptyTrash",
                headers={"X-Plex-Token": token},
            )
            resp.raise_for_status()
        print(f"[PLEX] Emptied trash for section {section_id}", flush=True)
        return True
    except Exception as exc:
        print(f"[PLEX] Empty trash failed for section {section_id}: {exc}", flush=True)
        return False


def _reverse_translate_path(plex_path: str, path_mapping: str) -> str:
    """Translate a Plex-visible path back to a container path (reverse of _translate_path)."""
    if not path_mapping:
        return plex_path
    for mapping in path_mapping.split(";"):
        mapping = mapping.strip()
        if "=" not in mapping:
            continue
        container_prefix, host_prefix = mapping.split("=", 1)
        container_prefix = container_prefix.rstrip("/")
        host_prefix = host_prefix.rstrip("/")
        if plex_path.startswith(host_prefix + "/") or plex_path == host_prefix:
            return container_prefix + plex_path[len(host_prefix):]
    return plex_path


async def _extract_folder_paths(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    path_mapping: str,
    lib: dict,
    root: ET.Element,
    context_label: str,
) -> list[str]:
    """Extract container folder paths from a Plex XML response. Shared by label and collection lookups."""
    folders: list[str] = []
    needs_detail_lookup: list[str] = []
    items_found = 0

    for item in list(root):
        folder_path = None
        for loc in item.findall("Location"):
            path = loc.get("path")
            if path:
                folder_path = path
                break
        if not folder_path:
            for media in item.findall("Media"):
                for part in media.findall("Part"):
                    fp = part.get("file")
                    if fp:
                        folder_path = str(Path(fp).parent)
                        break
                if folder_path:
                    break
        if folder_path:
            container_path = _reverse_translate_path(folder_path, path_mapping)
            folders.append(container_path)
            items_found += 1
        else:
            rating_key = item.get("ratingKey")
            if rating_key:
                needs_detail_lookup.append(rating_key)

    if needs_detail_lookup:
        for rk in needs_detail_lookup:
            try:
                detail_resp = await client.get(
                    f"{url.rstrip('/')}/library/metadata/{rk}",
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                detail_resp.raise_for_status()
                detail_root = ET.fromstring(detail_resp.text)
                for detail_item in list(detail_root):
                    for loc in detail_item.findall("Location"):
                        path = loc.get("path")
                        if path:
                            container_path = _reverse_translate_path(path, path_mapping)
                            folders.append(container_path)
                            items_found += 1
                            break
            except Exception as exc:
                print(f"[PLEX]   Failed to fetch metadata for {rk}: {exc}", flush=True)

    return folders


def _plex_type_for_lib(lib: dict) -> str | None:
    if lib["type"] == "show":
        return "2"
    if lib["type"] == "movie":
        return "1"
    return None


async def get_folders_by_label(label: str) -> list[str]:
    """Return container folder paths for items with a specific Plex label."""
    url, token, path_mapping = await _get_plex_settings()
    if not url or not token:
        return []

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception as exc:
        print(f"[PLEX] Failed to fetch libraries for label '{label}': {exc}", flush=True)
        return []

    folders: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for lib in libraries:
            params: dict = {"label": label, "includeLocations": "1"}
            plex_type = _plex_type_for_lib(lib)
            if plex_type:
                params["type"] = plex_type
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                    params=params,
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
            except Exception as exc:
                print(f"[PLEX] Failed to query label '{label}' in {lib['title']}: {exc}", flush=True)
                continue
            root = ET.fromstring(resp.text)
            folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, f"label={label}"))

    return list(set(folders))


async def get_folders_to_ignore_by_label(labels_to_match: list[str]) -> list[str]:
    """Query Plex for items with specific labels and return their container folder paths."""
    labels = [l.strip() for l in labels_to_match if l.strip()]
    if not labels:
        return []

    all_folders: list[str] = []
    for label in labels:
        all_folders.extend(await get_folders_by_label(label))

    matched = list(set(all_folders))
    if matched:
        print(f"[PLEX] Found {len(matched)} folders matching labels: {', '.join(labels)}", flush=True)
    return matched


async def get_folders_by_collection(collection_name: str) -> list[str]:
    """Return container folder paths for items in a specific Plex collection."""
    url, token, path_mapping = await _get_plex_settings()
    if not url or not token:
        return []

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception as exc:
        print(f"[PLEX] Failed to fetch libraries for collection '{collection_name}': {exc}", flush=True)
        return []

    folders: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for lib in libraries:
            params: dict = {"collection": collection_name, "includeLocations": "1"}
            plex_type = _plex_type_for_lib(lib)
            if plex_type:
                params["type"] = plex_type
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                    params=params,
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
            except Exception as exc:
                print(f"[PLEX] Failed to query collection '{collection_name}' in {lib['title']}: {exc}", flush=True)
                continue
            root = ET.fromstring(resp.text)
            folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, f"collection={collection_name}"))

    return list(set(folders))


async def get_folders_by_genre(genre_name: str) -> list[str]:
    """Return container folder paths for items with a specific Plex genre."""
    url, token, path_mapping = await _get_plex_settings()
    if not url or not token:
        return []

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception as exc:
        print(f"[PLEX] Failed to fetch libraries for genre '{genre_name}': {exc}", flush=True)
        return []

    folders: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for lib in libraries:
            params: dict = {"genre": genre_name, "includeLocations": "1"}
            plex_type = _plex_type_for_lib(lib)
            if plex_type:
                params["type"] = plex_type
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                    params=params,
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
            except Exception as exc:
                print(f"[PLEX] Failed to query genre '{genre_name}' in {lib['title']}: {exc}", flush=True)
                continue
            root = ET.fromstring(resp.text)
            folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, f"genre={genre_name}"))

    return list(set(folders))


async def get_watch_status_folders() -> dict[str, list[str]]:
    """Get folder paths grouped by watch status from Plex.

    Returns {"watched": [...], "unwatched": [...]} with container folder paths.
    """
    url, token, path_mapping = await _get_plex_settings()
    if not url or not token:
        return {"watched": [], "unwatched": []}

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception:
        return {"watched": [], "unwatched": []}

    watched_folders: list[str] = []
    unwatched_folders: list[str] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for lib in libraries:
            plex_type = _plex_type_for_lib(lib)
            headers = {"X-Plex-Token": token, "Accept": "application/xml"}

            # Fetch unwatched items
            try:
                params: dict = {"includeLocations": "1"}
                if plex_type:
                    params["type"] = plex_type
                if lib["type"] == "show":
                    # For TV shows, get shows with any unwatched episodes
                    params["unwatchedLeaves"] = "1"
                else:
                    params["unwatched"] = "1"

                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/unwatched",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                unwatched_folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, "unwatched"))
            except Exception:
                # Fallback: try with viewCount filter
                try:
                    params2: dict = {"viewCount": "0", "includeLocations": "1"}
                    if plex_type:
                        params2["type"] = plex_type
                    resp = await client.get(
                        f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                        params=params2,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    root = ET.fromstring(resp.text)
                    unwatched_folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, "unwatched"))
                except Exception:
                    pass

            # Fetch watched items
            try:
                params3: dict = {"includeLocations": "1"}
                if plex_type:
                    params3["type"] = plex_type
                if lib["type"] == "show":
                    # For TV: get fully watched shows (all episodes watched)
                    params3["unwatchedLeaves"] = "0"
                    resp = await client.get(
                        f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                        params=params3,
                        headers=headers,
                    )
                else:
                    # For movies: viewCount > 0 means watched
                    params3["viewCount>>"] = "0"
                    resp = await client.get(
                        f"{url.rstrip('/')}/library/sections/{lib['id']}/all",
                        params=params3,
                        headers=headers,
                    )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                watched_folders.extend(await _extract_folder_paths(client, url, token, path_mapping, lib, root, "watched"))
            except Exception:
                pass

    return {
        "watched": list(set(watched_folders)),
        "unwatched": list(set(unwatched_folders)),
    }


async def get_available_plex_options() -> dict:
    """Return available labels, collections, and libraries from Plex for rule autocomplete."""
    url, token, _ = await _get_plex_settings()
    if not url or not token:
        return {"labels": [], "collections": [], "libraries": []}

    try:
        libraries = await get_plex_libraries(url, token)
    except Exception:
        return {"labels": [], "collections": [], "libraries": []}

    labels_set: set[str] = set()
    collections_list: list[str] = []
    genres_set: set[str] = set()

    async with httpx.AsyncClient(timeout=30) as client:
        for lib in libraries:
            # Fetch labels via /library/sections/{id}/label
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/label",
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for d in root.findall(".//Directory"):
                    tag = d.get("title")
                    if tag:
                        labels_set.add(tag)
            except Exception:
                pass

            # Fetch collections via /library/sections/{id}/collections
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/collections",
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for d in root.findall(".//Directory"):
                    title = d.get("title")
                    if title:
                        collections_list.append(title)
            except Exception:
                pass

            # Fetch genres via /library/sections/{id}/genre
            try:
                resp = await client.get(
                    f"{url.rstrip('/')}/library/sections/{lib['id']}/genre",
                    headers={"X-Plex-Token": token, "Accept": "application/xml"},
                )
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for d in root.findall(".//Directory"):
                    title = d.get("title")
                    if title:
                        genres_set.add(title)
            except Exception:
                pass

    return {
        "labels": sorted(labels_set),
        "collections": sorted(set(collections_list)),
        "genres": sorted(genres_set),
        "libraries": [{"title": l["title"], "type": l["type"], "paths": l["paths"]} for l in libraries],
    }


async def sync_plex_metadata_cache() -> dict:
    """Sync Plex metadata (labels, collections, libraries) into plex_metadata_cache table.

    Only syncs values referenced by enabled encoding rules.
    Collects all Plex data first, then writes to DB in short transactions to avoid locking.
    """
    from datetime import datetime, timezone
    from backend.database import connect_db

    # Step 1: Read rules from DB (short connection)
    db = await connect_db()
    try:
        import json as _json
        async with db.execute(
            "SELECT match_type, match_value, match_conditions FROM encoding_rules WHERE enabled = 1"
        ) as cur:
            rules = await cur.fetchall()
    finally:
        await db.close()

    needed_labels: set[str] = set()
    needed_collections: set[str] = set()
    needed_libraries: set[str] = set()
    needed_genres: set[str] = set()
    for r in rules:
        conditions = []
        raw = r["match_conditions"]
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict) and "conditions" in parsed:
                    conditions = parsed.get("conditions", [])
                elif isinstance(parsed, list):
                    conditions = parsed
            except (ValueError, _json.JSONDecodeError):
                pass
        if not conditions and r["match_type"] and r["match_value"]:
            conditions = [{"type": r["match_type"], "value": r["match_value"]}]
        for cond in conditions:
            ct = cond.get("type", "")
            cv = cond.get("value", "")
            if ct == "label":
                needed_labels.add(cv)
            elif ct == "collection":
                needed_collections.add(cv)
            elif ct == "library":
                needed_libraries.add(cv)
            elif ct == "genre":
                needed_genres.add(cv)

    # Step 2: Fetch all data from Plex APIs (no DB held)
    now = datetime.now(timezone.utc).isoformat()
    cache_rows: list[tuple] = []  # (folder_path, metadata_type, metadata_value, synced_at)
    labels_count = 0
    collections_count = 0
    genres_count = 0
    libraries_count = 0

    for label in needed_labels:
        folders = await get_folders_by_label(label)
        for folder in folders:
            cache_rows.append((folder.rstrip("/") + "/", "label", label, now))
            labels_count += 1

    for coll in needed_collections:
        folders = await get_folders_by_collection(coll)
        for folder in folders:
            cache_rows.append((folder.rstrip("/") + "/", "collection", coll, now))
            collections_count += 1

    for genre in needed_genres:
        folders = await get_folders_by_genre(genre)
        for folder in folders:
            cache_rows.append((folder.rstrip("/") + "/", "genre", genre, now))
            genres_count += 1

    url, token, path_mapping = await _get_plex_settings()
    if url and token and needed_libraries:
        try:
            libs = await get_plex_libraries(url, token)
            for lib in libs:
                if lib["title"] in needed_libraries:
                    for lib_path in lib["paths"]:
                        container_path = _reverse_translate_path(lib_path, path_mapping)
                        cache_rows.append((container_path.rstrip("/") + "/", "library", lib["title"], now))
                        libraries_count += 1
        except Exception as exc:
            print(f"[PLEX] Failed to sync library paths: {exc}", flush=True)

    # Fetch watch status (can be slow — separate from DB)
    watch_count = 0
    try:
        watch_data = await get_watch_status_folders()
        for folder in watch_data.get("watched", []):
            cache_rows.append((folder.rstrip("/") + "/", "watch_status", "watched", now))
            watch_count += 1
        for folder in watch_data.get("unwatched", []):
            cache_rows.append((folder.rstrip("/") + "/", "watch_status", "unwatched", now))
            watch_count += 1
    except Exception as exc:
        print(f"[PLEX SYNC] Watch status sync failed: {exc}", flush=True)

    # Step 3: Write all collected data to DB in one short transaction
    db = await connect_db()
    try:
        await db.execute("DELETE FROM plex_metadata_cache")
        await db.execute("DELETE FROM ignored_files WHERE reason LIKE 'encoding_rule:%'")
        for row in cache_rows:
            await db.execute(
                "INSERT OR IGNORE INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) VALUES (?, ?, ?, ?)",
                row,
            )

        await db.commit()
        return {"labels_synced": labels_count, "collections_synced": collections_count, "genres_synced": genres_count, "libraries_synced": libraries_count, "watch_synced": watch_count}
    finally:
        await db.close()


async def get_active_streams() -> dict:
    """Check Plex for active streaming sessions.

    Returns {total: int, transcoding: int, direct: int, sessions: [...]}
    """
    url, token, _ = await _get_plex_settings()
    if not url or not token:
        return {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/status/sessions",
                headers={"X-Plex-Token": token, "Accept": "application/xml"},
            )
            resp.raise_for_status()

        root = ET.fromstring(resp.text)
        sessions = []
        transcoding = 0
        direct = 0

        for item in list(root):
            # Each child is a Video/Track element with Session and TranscodeSession children
            session_info: dict = {
                "title": item.get("title", "Unknown"),
                "type": item.get("type", ""),
                "user": "",
                "is_transcoding": False,
            }

            # Get user info
            user_elem = item.find("User")
            if user_elem is not None:
                session_info["user"] = user_elem.get("title", "")

            # Check if transcoding
            transcode_elem = item.find("TranscodeSession")
            if transcode_elem is not None:
                video_decision = transcode_elem.get("videoDecision", "")
                audio_decision = transcode_elem.get("audioDecision", "")
                # "transcode" means active transcoding, "copy" or "directplay" means direct
                if video_decision == "transcode" or audio_decision == "transcode":
                    session_info["is_transcoding"] = True
                    transcoding += 1
                else:
                    direct += 1
            else:
                # No transcode session = direct play
                direct += 1

            sessions.append(session_info)

        total = len(sessions)
        return {"total": total, "transcoding": transcoding, "direct": direct, "sessions": sessions}
    except Exception as exc:
        print(f"[PLEX] Failed to check sessions: {exc}", flush=True)
        return {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}


async def test_plex_connection(url: str, token: str) -> dict:
    """Test Plex connection. Returns dict with success, server_name, library_count."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Test basic connectivity
            resp = await client.get(
                f"{url.rstrip('/')}/",
                headers={"X-Plex-Token": token, "Accept": "application/xml"},
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            server_name = root.get("friendlyName", "Unknown")

            # Count libraries
            libraries = await get_plex_libraries(url, token)

        return {
            "success": True,
            "server_name": server_name,
            "library_count": len(libraries),
            "libraries": [{"title": l["title"], "type": l["type"], "paths": l["paths"]} for l in libraries],
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"success": False, "error": "Invalid Plex token (401 Unauthorized)"}
        return {"success": False, "error": f"HTTP {exc.response.status_code}"}
    except httpx.ConnectError:
        return {"success": False, "error": f"Cannot connect to {url}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
