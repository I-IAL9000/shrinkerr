"""Sonarr and Radarr integration — trigger rescans after conversion."""

import time
from pathlib import Path

import httpx

from backend.database import connect_db

# Deduplication cache: path -> timestamp of last rescan trigger
_recent_rescans: dict[str, float] = {}
_RESCAN_COOLDOWN = 30  # seconds — skip duplicate rescans within this window

# Cache series/movie lists to avoid fetching 7000+ items on every job
_sonarr_cache: dict = {}  # {data: list, fetched_at: float}
_radarr_cache: dict = {}
_LIST_CACHE_TTL = 300  # 5 minutes


async def _get_arr_settings() -> dict:
    """Read Sonarr/Radarr settings from DB."""
    db = await connect_db()
    try:
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('sonarr_url', 'sonarr_api_key', 'sonarr_path_mapping', "
            " 'radarr_url', 'radarr_api_key', 'radarr_path_mapping')"
        ) as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
        return settings
    finally:
        await db.close()


def _translate_path(file_path: str, path_mapping: str) -> str:
    """Translate container path to Sonarr/Radarr-visible path."""
    # Normalize double slashes (e.g. /media//Movies -> /media/Movies)
    import posixpath
    file_path = posixpath.normpath(file_path)
    if not path_mapping:
        return file_path
    for mapping in path_mapping.split(";"):
        mapping = mapping.strip()
        if "=" not in mapping:
            continue
        container_prefix, arr_prefix = mapping.split("=", 1)
        container_prefix = container_prefix.rstrip("/")
        arr_prefix = arr_prefix.rstrip("/")
        if file_path.startswith(container_prefix + "/") or file_path == container_prefix:
            return arr_prefix + file_path[len(container_prefix):]
    return file_path


def _is_recently_rescanned(key: str) -> bool:
    """Check if this path was rescanned recently (deduplication for parallel jobs)."""
    now = time.monotonic()
    # Clean old entries
    stale = [k for k, t in _recent_rescans.items() if now - t > _RESCAN_COOLDOWN * 2]
    for k in stale:
        del _recent_rescans[k]
    return key in _recent_rescans and (now - _recent_rescans[key]) < _RESCAN_COOLDOWN


def _mark_rescanned(key: str):
    _recent_rescans[key] = time.monotonic()


async def trigger_sonarr_rescan(file_path: str) -> bool:
    """Look up the series in Sonarr by folder path and trigger a rescan."""
    settings = await _get_arr_settings()
    url = settings.get("sonarr_url", "").rstrip("/")
    api_key = settings.get("sonarr_api_key", "")
    path_mapping = settings.get("sonarr_path_mapping", "")

    if not url or not api_key:
        print(f"[SONARR] Not configured (url={bool(url)}, key={bool(api_key)})", flush=True)
        return False

    arr_path = _translate_path(file_path, path_mapping)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {"X-Api-Key": api_key}

            # Use cached series list (refreshes every 5 min)
            now = time.monotonic()
            if _sonarr_cache.get("data") and (now - _sonarr_cache.get("fetched_at", 0)) < _LIST_CACHE_TTL:
                series_list = _sonarr_cache["data"]
            else:
                resp = await client.get(f"{url}/api/v3/series", headers=headers)
                resp.raise_for_status()
                series_list = resp.json()
                _sonarr_cache["data"] = series_list
                _sonarr_cache["fetched_at"] = now
                print(f"[SONARR] Fetched {len(series_list)} series from Sonarr", flush=True)

            # Walk up the path hierarchy to find a matching series
            series_id = None
            check_path = str(Path(arr_path).parent)
            checked_paths = []
            while check_path and check_path != "/":
                checked_paths.append(check_path.rstrip("/"))
                for s in series_list:
                    s_path = s.get("path", "").rstrip("/")
                    if check_path.rstrip("/") == s_path:
                        series_id = s.get("id")
                        break
                if series_id is not None:
                    break
                check_path = str(Path(check_path).parent)

            if series_id is None:
                return False

            # Dedup check
            dedup_key = f"sonarr:{series_id}"
            if _is_recently_rescanned(dedup_key):
                print(f"[SONARR] Skipping duplicate rescan for series {series_id}", flush=True)
                return True

            resp = await client.post(
                f"{url}/api/v3/command",
                headers=headers,
                json={"name": "RescanSeries", "seriesId": series_id},
            )
            resp.raise_for_status()
            _mark_rescanned(dedup_key)
            print(f"[SONARR] Triggered rescan for series {series_id}", flush=True)
            return True
    except Exception as exc:
        print(f"[SONARR] Rescan failed: {exc}", flush=True)
        return False


async def trigger_radarr_rescan(file_path: str) -> bool:
    """Look up the movie in Radarr by folder path and trigger a rescan."""
    settings = await _get_arr_settings()
    url = settings.get("radarr_url", "").rstrip("/")
    api_key = settings.get("radarr_api_key", "")
    path_mapping = settings.get("radarr_path_mapping", "")

    if not url or not api_key:
        return False

    arr_path = _translate_path(file_path, path_mapping)
    print(f"[RADARR] Translated path: {file_path} -> {arr_path}", flush=True)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {"X-Api-Key": api_key}

            # Use cached movie list (refreshes every 5 min)
            now = time.monotonic()
            if _radarr_cache.get("data") and (now - _radarr_cache.get("fetched_at", 0)) < _LIST_CACHE_TTL:
                movie_list = _radarr_cache["data"]
            else:
                resp = await client.get(f"{url}/api/v3/movie", headers=headers)
                resp.raise_for_status()
                movie_list = resp.json()
                _radarr_cache["data"] = movie_list
                _radarr_cache["fetched_at"] = now
                print(f"[RADARR] Fetched {len(movie_list)} movies from Radarr", flush=True)

            # Walk up the path to find matching movie folder
            movie_id = None
            check_path = str(Path(arr_path).parent)
            while check_path and check_path != "/":
                for m in movie_list:
                    m_path = m.get("path", "").rstrip("/")
                    if check_path.rstrip("/") == m_path:
                        movie_id = m.get("id")
                        break
                if movie_id is not None:
                    break
                check_path = str(Path(check_path).parent)

            if movie_id is None:
                print(f"[RADARR] No match for folder: {str(Path(arr_path).parent)}", flush=True)
                return False

            # Dedup check
            dedup_key = f"radarr:{movie_id}"
            if _is_recently_rescanned(dedup_key):
                print(f"[RADARR] Skipping duplicate rescan for movie {movie_id}", flush=True)
                return True

            resp = await client.post(
                f"{url}/api/v3/command",
                headers=headers,
                json={"name": "RescanMovie", "movieId": movie_id},
            )
            resp.raise_for_status()
            _mark_rescanned(dedup_key)
            print(f"[RADARR] Triggered rescan for movie {movie_id}", flush=True)
            return True
    except Exception as exc:
        print(f"[RADARR] Rescan failed: {exc}", flush=True)
        return False


def _detect_media_type(file_path: str) -> str:
    """Detect if a file is a TV show or movie based on path and naming conventions.

    Returns 'tv', 'movie', or 'unknown'.
    """
    import re
    p = file_path.lower()

    # Explicit metadata tags in Sonarr/Radarr folder names
    if re.search(r'\[tvdb-\d+\]', file_path):
        return "tv"
    if re.search(r'\[tt\d+\]', file_path):
        return "movie"

    # Season/episode patterns (S01E01, S01, 1x01) — strong TV indicator
    if re.search(r'[/\\].*[Ss]\d{1,2}[Ee]\d{1,2}', file_path):
        return "tv"
    if re.search(r'[/\\].*\b\d{1,2}x\d{2}\b', file_path):
        return "tv"

    # Common TV path segments
    if re.search(r'[/\\](tv|tv\d|series|shows?)[/\\]', p):
        return "tv"

    # Season folder in path (e.g. /Season 01/ or /Season 1/)
    if re.search(r'[/\\]season\s*\d+[/\\]', p):
        return "tv"

    # Common movie path segments
    if re.search(r'[/\\](movies?|films?)[/\\]', p):
        return "movie"

    return "unknown"


async def trigger_arr_rescan(file_path: str) -> dict:
    """Trigger rescan in the appropriate *arr based on folder naming.

    TV shows ([tvdb-*]) -> Sonarr only
    Movies ([tt*]) -> Radarr only
    Unknown -> try Sonarr first, then Radarr
    """
    media_type = _detect_media_type(file_path)

    if media_type == "tv":
        sonarr = await trigger_sonarr_rescan(file_path)
        return {"sonarr": sonarr, "radarr": False}
    elif media_type == "movie":
        radarr = await trigger_radarr_rescan(file_path)
        return {"sonarr": False, "radarr": radarr}
    else:
        # Unknown — try both, Sonarr first then Radarr
        sonarr = await trigger_sonarr_rescan(file_path)
        if sonarr:
            return {"sonarr": True, "radarr": False}
        radarr = await trigger_radarr_rescan(file_path)
        return {"sonarr": False, "radarr": radarr}


# ────────────────────────────────────────────────────────────────────────────
# Re-search (research): blocklist the current release + trigger a new download.
# Used when the downloaded file is corrupt, or when the user just wants a
# different release. Works in two phases:
#   1. Mark the most recent history record as "failed" → this adds the release
#      to the blocklist so the same NZB/torrent won't be re-grabbed, and in
#      most *arr versions it automatically triggers a fresh search.
#   2. Explicitly trigger a search command to be safe across versions.
# ────────────────────────────────────────────────────────────────────────────


async def _find_sonarr_series_for_path(client: httpx.AsyncClient, url: str, api_key: str,
                                        arr_path: str) -> dict | None:
    """Locate the Sonarr series record whose folder contains (or equals) the given path.

    Handles both file paths (walks up the parent chain to find the containing
    series folder) and directory paths (matches directly when the selection
    IS the series folder, e.g. "/TV/Bluey/" from a folder-selection in the UI).
    """
    headers = {"X-Api-Key": api_key}
    now = time.monotonic()
    if _sonarr_cache.get("data") and (now - _sonarr_cache.get("fetched_at", 0)) < _LIST_CACHE_TTL:
        series_list = _sonarr_cache["data"]
    else:
        resp = await client.get(f"{url}/api/v3/series", headers=headers)
        resp.raise_for_status()
        series_list = resp.json()
        _sonarr_cache["data"] = series_list
        _sonarr_cache["fetched_at"] = now

    # Start from the path itself (without trailing slash) rather than its
    # parent — so a selected series folder matches on the first iteration.
    # For file paths, the first iteration misses and we walk up into the
    # containing series folder naturally.
    check_path = arr_path.rstrip("/")
    while check_path and check_path != "/":
        for s in series_list:
            s_path = s.get("path", "").rstrip("/")
            if check_path == s_path:
                return s
        check_path = str(Path(check_path).parent)
    return None


async def _find_radarr_movie_for_path(client: httpx.AsyncClient, url: str, api_key: str,
                                       arr_path: str) -> dict | None:
    """Locate the Radarr movie record whose folder contains (or equals) the given path.

    Handles both file paths (walks up) and directory paths (direct match
    when the selection IS the movie folder).
    """
    headers = {"X-Api-Key": api_key}
    now = time.monotonic()
    if _radarr_cache.get("data") and (now - _radarr_cache.get("fetched_at", 0)) < _LIST_CACHE_TTL:
        movie_list = _radarr_cache["data"]
    else:
        resp = await client.get(f"{url}/api/v3/movie", headers=headers)
        resp.raise_for_status()
        movie_list = resp.json()
        _radarr_cache["data"] = movie_list
        _radarr_cache["fetched_at"] = now

    check_path = arr_path.rstrip("/")
    while check_path and check_path != "/":
        for m in movie_list:
            m_path = m.get("path", "").rstrip("/")
            if check_path == m_path:
                return m
        check_path = str(Path(check_path).parent)
    return None


async def research_sonarr_file(file_path: str, delete_file: bool = True) -> dict:
    """Blocklist the current release and trigger Sonarr to grab a replacement."""
    settings = await _get_arr_settings()
    url = settings.get("sonarr_url", "").rstrip("/")
    api_key = settings.get("sonarr_api_key", "")
    path_mapping = settings.get("sonarr_path_mapping", "")

    if not url or not api_key:
        return {"success": False, "error": "Sonarr not configured"}

    arr_path = _translate_path(file_path, path_mapping)
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Locate the series
            series = await _find_sonarr_series_for_path(client, url, api_key, arr_path)
            if not series:
                return {"success": False, "error": f"No matching Sonarr series for path: {arr_path}"}
            series_id = series["id"]

            # 2. Find the episodefile whose path matches
            resp = await client.get(
                f"{url}/api/v3/episodefile",
                headers=headers,
                params={"seriesId": series_id},
            )
            resp.raise_for_status()
            files = resp.json()
            match = next((f for f in files if f.get("path", "").rstrip("/") == arr_path.rstrip("/")), None)
            if not match:
                # Fall back to basename match (path mapping discrepancies)
                target_name = Path(arr_path).name
                match = next((f for f in files if Path(f.get("path", "")).name == target_name), None)
            if not match:
                return {"success": False, "error": f"No Sonarr episodefile matching path"}
            episodefile_id = match["id"]

            # 3. Find the episodes associated with this file (needed for search command)
            resp = await client.get(
                f"{url}/api/v3/episode",
                headers=headers,
                params={"seriesId": series_id},
            )
            resp.raise_for_status()
            episodes = resp.json()
            episode_ids = [e["id"] for e in episodes if e.get("episodeFileId") == episodefile_id]

            # 4. Find the most recent import history record for these episodes
            blocklisted = False
            blocklist_error = None
            if episode_ids:
                try:
                    # Look for the most recent grabbed/imported record and blocklist it.
                    # eventType 1 = grabbed, 3 = downloadFolderImported
                    for ep_id in episode_ids:
                        resp = await client.get(
                            f"{url}/api/v3/history",
                            headers=headers,
                            params={"episodeId": ep_id, "pageSize": 20, "sortKey": "date", "sortDirection": "descending"},
                        )
                        if resp.status_code != 200:
                            continue
                        records = resp.json().get("records", [])
                        # markAsFailed needs a history record id for a *grabbed* event
                        grab_rec = next((r for r in records if r.get("eventType") == "grabbed"), None)
                        if grab_rec:
                            resp2 = await client.post(
                                f"{url}/api/v3/history/failed/{grab_rec['id']}",
                                headers=headers,
                            )
                            if resp2.status_code in (200, 201):
                                blocklisted = True
                                print(f"[SONARR-RESEARCH] Marked history {grab_rec['id']} as failed (blocklisted)", flush=True)
                                break
                            else:
                                blocklist_error = f"HTTP {resp2.status_code}: {resp2.text[:200]}"
                except Exception as exc:
                    blocklist_error = str(exc)
                    print(f"[SONARR-RESEARCH] Blocklist step failed: {exc}", flush=True)

            # 5. Delete the episodefile (removes DB record + physical file if requested)
            # Sonarr's DELETE /api/v3/episodefile/{id} always deletes the physical file.
            # If the user doesn't want the file deleted, we skip this step.
            deleted = False
            if delete_file:
                resp = await client.delete(
                    f"{url}/api/v3/episodefile/{episodefile_id}",
                    headers=headers,
                )
                deleted = resp.status_code in (200, 204)
                if not deleted:
                    print(f"[SONARR-RESEARCH] Delete episodefile failed: {resp.status_code} {resp.text[:200]}", flush=True)

            # 6. Trigger a fresh search. markAsFailed usually does this implicitly,
            #    but call it explicitly to be safe across versions.
            searched = False
            if episode_ids:
                resp = await client.post(
                    f"{url}/api/v3/command",
                    headers=headers,
                    json={"name": "EpisodeSearch", "episodeIds": episode_ids},
                )
                searched = resp.status_code in (200, 201)
                if searched:
                    print(f"[SONARR-RESEARCH] Triggered EpisodeSearch for episodes {episode_ids}", flush=True)

            return {
                "success": True,
                "service": "sonarr",
                "series": series.get("title"),
                "episode_ids": episode_ids,
                "blocklisted": blocklisted,
                "blocklist_error": blocklist_error,
                "deleted": deleted,
                "searched": searched,
            }
    except Exception as exc:
        print(f"[SONARR-RESEARCH] Failed: {exc}", flush=True)
        return {"success": False, "error": str(exc)}


async def research_radarr_file(file_path: str, delete_file: bool = True) -> dict:
    """Blocklist the current release and trigger Radarr to grab a replacement."""
    settings = await _get_arr_settings()
    url = settings.get("radarr_url", "").rstrip("/")
    api_key = settings.get("radarr_api_key", "")
    path_mapping = settings.get("radarr_path_mapping", "")

    if not url or not api_key:
        return {"success": False, "error": "Radarr not configured"}

    arr_path = _translate_path(file_path, path_mapping)
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Locate the movie
            movie = await _find_radarr_movie_for_path(client, url, api_key, arr_path)
            if not movie:
                return {"success": False, "error": f"No matching Radarr movie for path: {arr_path}"}
            movie_id = movie["id"]

            # 2. Refresh movie to make sure we have current movieFile info
            resp = await client.get(f"{url}/api/v3/movie/{movie_id}", headers=headers)
            resp.raise_for_status()
            movie_full = resp.json()
            movie_file = movie_full.get("movieFile") or {}
            movie_file_id = movie_file.get("id")

            # 3. Blocklist the most recent grabbed release
            blocklisted = False
            blocklist_error = None
            try:
                resp = await client.get(
                    f"{url}/api/v3/history/movie",
                    headers=headers,
                    params={"movieId": movie_id, "eventType": 1},  # 1 = grabbed
                )
                if resp.status_code == 200:
                    records = resp.json()
                    # sort by date desc
                    records.sort(key=lambda r: r.get("date", ""), reverse=True)
                    if records:
                        grab_rec = records[0]
                        resp2 = await client.post(
                            f"{url}/api/v3/history/failed/{grab_rec['id']}",
                            headers=headers,
                        )
                        if resp2.status_code in (200, 201):
                            blocklisted = True
                            print(f"[RADARR-RESEARCH] Marked history {grab_rec['id']} as failed (blocklisted)", flush=True)
                        else:
                            blocklist_error = f"HTTP {resp2.status_code}: {resp2.text[:200]}"
            except Exception as exc:
                blocklist_error = str(exc)
                print(f"[RADARR-RESEARCH] Blocklist step failed: {exc}", flush=True)

            # 4. Delete moviefile
            deleted = False
            if delete_file and movie_file_id:
                resp = await client.delete(
                    f"{url}/api/v3/moviefile/{movie_file_id}",
                    headers=headers,
                )
                deleted = resp.status_code in (200, 204)
                if not deleted:
                    print(f"[RADARR-RESEARCH] Delete moviefile failed: {resp.status_code} {resp.text[:200]}", flush=True)

            # 5. Trigger search
            resp = await client.post(
                f"{url}/api/v3/command",
                headers=headers,
                json={"name": "MoviesSearch", "movieIds": [movie_id]},
            )
            searched = resp.status_code in (200, 201)
            if searched:
                print(f"[RADARR-RESEARCH] Triggered MoviesSearch for movie {movie_id}", flush=True)

            return {
                "success": True,
                "service": "radarr",
                "movie": movie_full.get("title"),
                "movie_id": movie_id,
                "blocklisted": blocklisted,
                "blocklist_error": blocklist_error,
                "deleted": deleted,
                "searched": searched,
            }
    except Exception as exc:
        print(f"[RADARR-RESEARCH] Failed: {exc}", flush=True)
        return {"success": False, "error": str(exc)}


async def research_file(file_path: str, delete_file: bool = True) -> dict:
    """Request a fresh download of this file via the appropriate *arr.

    Routes based on folder conventions:
      * TV ([tvdb-*] / S##E## paths) → Sonarr
      * Movies ([tt*] / movies/films path) → Radarr
      * Unknown → try Sonarr first, then Radarr
    """
    media_type = _detect_media_type(file_path)
    if media_type == "tv":
        return await research_sonarr_file(file_path, delete_file=delete_file)
    if media_type == "movie":
        return await research_radarr_file(file_path, delete_file=delete_file)
    # Unknown — try Sonarr first
    result = await research_sonarr_file(file_path, delete_file=delete_file)
    if result.get("success"):
        return result
    return await research_radarr_file(file_path, delete_file=delete_file)


# ────────────────────────────────────────────────────────────────────────────
# Upgrade search (no blocklist / no delete): "find me a better release, per
# the configured quality profile cutoff."
# ────────────────────────────────────────────────────────────────────────────


async def upgrade_sonarr_file(file_path: str) -> dict:
    """Trigger Sonarr to search for a better release of this episode.

    Same as research except we DON'T blocklist the current release and DON'T
    delete the file. Sonarr will only download if it finds something that
    beats the current release per the series' quality profile.
    """
    settings = await _get_arr_settings()
    url = settings.get("sonarr_url", "").rstrip("/")
    api_key = settings.get("sonarr_api_key", "")
    path_mapping = settings.get("sonarr_path_mapping", "")
    if not url or not api_key:
        return {"success": False, "error": "Sonarr not configured"}

    arr_path = _translate_path(file_path, path_mapping)
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            series = await _find_sonarr_series_for_path(client, url, api_key, arr_path)
            if not series:
                return {"success": False, "error": f"No matching Sonarr series for path"}
            series_id = series["id"]

            # Find the episodefile by path to scope search to its episodes
            resp = await client.get(
                f"{url}/api/v3/episodefile", headers=headers,
                params={"seriesId": series_id},
            )
            resp.raise_for_status()
            files = resp.json()
            match = next((f for f in files if f.get("path", "").rstrip("/") == arr_path.rstrip("/")), None)
            if not match:
                target_name = Path(arr_path).name
                match = next((f for f in files if Path(f.get("path", "")).name == target_name), None)
            if not match:
                return {"success": False, "error": "No Sonarr episodefile matching path"}

            # Resolve episode IDs
            resp = await client.get(
                f"{url}/api/v3/episode", headers=headers,
                params={"seriesId": series_id},
            )
            resp.raise_for_status()
            episodes = resp.json()
            episode_ids = [e["id"] for e in episodes if e.get("episodeFileId") == match["id"]]

            if not episode_ids:
                return {"success": False, "error": "No episodes link to this file"}

            resp = await client.post(
                f"{url}/api/v3/command",
                headers=headers,
                json={"name": "EpisodeSearch", "episodeIds": episode_ids},
            )
            if resp.status_code not in (200, 201):
                return {"success": False, "error": f"EpisodeSearch failed: {resp.status_code}"}
            print(f"[SONARR-UPGRADE] Triggered EpisodeSearch for {episode_ids} (series={series.get('title')})", flush=True)
            return {
                "success": True, "service": "sonarr", "action": "upgrade",
                "series": series.get("title"), "episode_ids": episode_ids,
            }
    except Exception as exc:
        print(f"[SONARR-UPGRADE] Failed: {exc}", flush=True)
        return {"success": False, "error": str(exc)}


async def upgrade_radarr_file(file_path: str) -> dict:
    """Trigger Radarr to search for a better release of this movie."""
    settings = await _get_arr_settings()
    url = settings.get("radarr_url", "").rstrip("/")
    api_key = settings.get("radarr_api_key", "")
    path_mapping = settings.get("radarr_path_mapping", "")
    if not url or not api_key:
        return {"success": False, "error": "Radarr not configured"}

    arr_path = _translate_path(file_path, path_mapping)
    headers = {"X-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            movie = await _find_radarr_movie_for_path(client, url, api_key, arr_path)
            if not movie:
                return {"success": False, "error": f"No matching Radarr movie for path"}
            movie_id = movie["id"]

            resp = await client.post(
                f"{url}/api/v3/command",
                headers=headers,
                json={"name": "MoviesSearch", "movieIds": [movie_id]},
            )
            if resp.status_code not in (200, 201):
                return {"success": False, "error": f"MoviesSearch failed: {resp.status_code}"}
            print(f"[RADARR-UPGRADE] Triggered MoviesSearch for movie_id={movie_id} ({movie.get('title')})", flush=True)
            return {
                "success": True, "service": "radarr", "action": "upgrade",
                "movie": movie.get("title"), "movie_id": movie_id,
            }
    except Exception as exc:
        print(f"[RADARR-UPGRADE] Failed: {exc}", flush=True)
        return {"success": False, "error": str(exc)}


async def upgrade_file(file_path: str) -> dict:
    """Trigger a quality-upgrade search on the file's owning show/movie."""
    media_type = _detect_media_type(file_path)
    if media_type == "tv":
        return await upgrade_sonarr_file(file_path)
    if media_type == "movie":
        return await upgrade_radarr_file(file_path)
    result = await upgrade_sonarr_file(file_path)
    if result.get("success"):
        return result
    return await upgrade_radarr_file(file_path)


# ────────────────────────────────────────────────────────────────────────────
# Missing search: for a batch of paths, resolve to unique series (Sonarr),
# then search for each series' missing episodes. Movies in the batch are
# noted but not actioned (missing-search is a TV-level concept — a movie is
# either present or absent, there's no batch semantics that makes sense for
# already-present files the user selected).
# ────────────────────────────────────────────────────────────────────────────


async def search_missing_episodes(file_paths: list[str]) -> dict:
    """Resolve file_paths to unique Sonarr series, then search each series'
    missing episodes. Returns an aggregate summary.
    """
    if not file_paths:
        return {"success": False, "error": "No file paths provided"}

    settings = await _get_arr_settings()
    sonarr_url = settings.get("sonarr_url", "").rstrip("/")
    sonarr_key = settings.get("sonarr_api_key", "")
    sonarr_mapping = settings.get("sonarr_path_mapping", "")
    if not sonarr_url or not sonarr_key:
        return {"success": False, "error": "Sonarr not configured"}

    headers = {"X-Api-Key": sonarr_key}

    # Partition paths by media type; movies are counted but skipped
    tv_paths: list[str] = []
    skipped_movie = 0
    skipped_unknown = 0
    for p in file_paths:
        mt = _detect_media_type(p)
        if mt == "tv":
            tv_paths.append(p)
        elif mt == "movie":
            skipped_movie += 1
        else:
            # Treat unknown as TV candidates — path walking will drop
            # anything that doesn't actually resolve to a Sonarr series.
            tv_paths.append(p)
            skipped_unknown += 1

    if not tv_paths:
        return {
            "success": True, "service": "sonarr", "action": "missing",
            "series_searched": 0, "total_episode_ids": 0,
            "skipped_movie": skipped_movie, "skipped_unknown": skipped_unknown,
            "details": [],
        }

    # Resolve each TV path to its Sonarr series, dedup by series_id
    series_by_id: dict[int, dict] = {}  # series_id → series record
    unresolved = 0

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for p in tv_paths:
                arr_p = _translate_path(p, sonarr_mapping)
                series = await _find_sonarr_series_for_path(client, sonarr_url, sonarr_key, arr_p)
                if series:
                    series_by_id[series["id"]] = series
                else:
                    unresolved += 1

            if not series_by_id:
                return {
                    "success": False,
                    "error": "No matching Sonarr series for any of the provided paths",
                    "unresolved": unresolved,
                }

            # For each unique series, look up missing episodes and trigger
            # EpisodeSearch. We use the per-series /wanted/missing filter
            # rather than a library-wide MissingEpisodeSearch so the action
            # stays scoped to what the user actually selected.
            details: list[dict] = []
            total_episode_ids = 0

            for series_id, series in series_by_id.items():
                # Page through missing episodes for this series.
                missing_ids: list[int] = []
                page = 1
                page_size = 100
                while True:
                    resp = await client.get(
                        f"{sonarr_url}/api/v3/wanted/missing",
                        headers=headers,
                        params={
                            "seriesId": series_id,
                            "page": page,
                            "pageSize": page_size,
                            "sortKey": "airDateUtc",
                            "sortDirection": "descending",
                            "includeSeries": "false",
                            "monitored": "true",
                        },
                    )
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    records = data.get("records", [])
                    missing_ids.extend(r["id"] for r in records if "id" in r)
                    if len(records) < page_size:
                        break
                    page += 1
                    if page > 50:  # safety cap — 5000 episodes
                        break

                if not missing_ids:
                    details.append({
                        "series_id": series_id,
                        "series_title": series.get("title"),
                        "missing_count": 0,
                        "searched": False,
                        "note": "No missing monitored episodes",
                    })
                    continue

                # Fire the search command for this series' missing episodes.
                try:
                    resp = await client.post(
                        f"{sonarr_url}/api/v3/command",
                        headers=headers,
                        json={"name": "EpisodeSearch", "episodeIds": missing_ids},
                    )
                    ok = resp.status_code in (200, 201)
                    if ok:
                        total_episode_ids += len(missing_ids)
                        print(f"[SONARR-MISSING] {series.get('title')}: searching {len(missing_ids)} missing episodes", flush=True)
                    details.append({
                        "series_id": series_id,
                        "series_title": series.get("title"),
                        "missing_count": len(missing_ids),
                        "searched": ok,
                        "note": None if ok else f"Search command failed ({resp.status_code})",
                    })
                except Exception as exc:
                    details.append({
                        "series_id": series_id,
                        "series_title": series.get("title"),
                        "missing_count": len(missing_ids),
                        "searched": False,
                        "note": str(exc),
                    })

            return {
                "success": True,
                "service": "sonarr",
                "action": "missing",
                "series_searched": sum(1 for d in details if d["searched"]),
                "series_resolved": len(series_by_id),
                "total_episode_ids": total_episode_ids,
                "skipped_movie": skipped_movie,
                "skipped_unknown": skipped_unknown,
                "unresolved": unresolved,
                "details": details,
            }
    except Exception as exc:
        print(f"[SONARR-MISSING] Failed: {exc}", flush=True)
        return {"success": False, "error": str(exc)}


# ────────────────────────────────────────────────────────────────────────────
# Unified dispatch — the route layer calls this.
# ────────────────────────────────────────────────────────────────────────────


async def dispatch_action(action: str, file_path: str, delete_file: bool = True) -> dict:
    """Run a single-file *arr action. `action` is one of
    "replace" | "upgrade" | "missing".

    "missing" only really makes sense in bulk; we handle it as a single-series
    missing search by forwarding through search_missing_episodes.
    """
    a = (action or "").lower()
    if a == "replace":
        return await research_file(file_path, delete_file=delete_file)
    if a == "upgrade":
        return await upgrade_file(file_path)
    if a == "missing":
        return await search_missing_episodes([file_path])
    return {"success": False, "error": f"Unknown action: {action}"}


async def test_sonarr(url: str, api_key: str) -> dict:
    """Test Sonarr connection."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"success": True, "version": data.get("version", "?")}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"success": False, "error": "Invalid API key (401)"}
        return {"success": False, "error": f"HTTP {exc.response.status_code}"}
    except httpx.ConnectError:
        return {"success": False, "error": f"Cannot connect to {url}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def test_radarr(url: str, api_key: str) -> dict:
    """Test Radarr connection."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"success": True, "version": data.get("version", "?")}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"success": False, "error": "Invalid API key (401)"}
        return {"success": False, "error": f"HTTP {exc.response.status_code}"}
    except httpx.ConnectError:
        return {"success": False, "error": f"Cannot connect to {url}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
