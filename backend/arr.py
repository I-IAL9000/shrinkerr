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
    """Detect if a file is a TV show or movie based on folder naming conventions.

    TV shows have [tvdb-XXXXX] in the path.
    Movies have [ttXXXXXXX] (IMDB) in the path.
    Returns 'tv', 'movie', or 'unknown'.
    """
    import re
    if re.search(r'\[tvdb-\d+\]', file_path):
        return "tv"
    if re.search(r'\[tt\d+\]', file_path):
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
        # Unknown — try Sonarr first, fall back to Radarr
        sonarr = await trigger_sonarr_rescan(file_path)
        radarr = False if sonarr else await trigger_radarr_rescan(file_path)
        return {"sonarr": sonarr, "radarr": radarr}


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
