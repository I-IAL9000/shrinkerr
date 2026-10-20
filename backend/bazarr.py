"""Bazarr (v0.10.0): after Shrinkerr rewrites a file — sidecar subtitles
merged in (and maybe deleted), image subtitles read into SRT, the file
renamed — Bazarr still has the old picture, and would download the
"missing" subtitles again. Tell it to re-sync the series / movie from
Sonarr / Radarr and scan its subtitles on disk.

Bazarr's API: PATCH /api/series?seriesid=<Sonarr id>&action=sync|scan-disk,
PATCH /api/movies?radarrid=<Radarr id>&action=…, with the X-API-KEY header.
The Scanner's "Search for subtitles" is action=search-missing.
"""
from __future__ import annotations

import asyncio

import httpx

from backend.database import connect_db
from backend.error_text import describe_error

# Sonarr / Radarr rescan the file first (Shrinkerr asks them right after a
# job); Bazarr's sync reads what they found, so give them a moment.
RESCAN_DELAY = 60
_ACTIONS = ("sync", "scan-disk")


async def _settings() -> tuple[str, str]:
    db = await connect_db()
    try:
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('bazarr_url', 'bazarr_api_key')") as cur:
            s = {r["key"]: r["value"] for r in await cur.fetchall()}
    finally:
        await db.close()
    return (s.get("bazarr_url") or "").strip().rstrip("/"), (s.get("bazarr_api_key") or "").strip()


async def test_bazarr(url: str, api_key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/system/status", headers={"X-API-KEY": api_key})
        resp.raise_for_status()
        return {"success": True, "version": ((resp.json() or {}).get("data") or {}).get("bazarr_version")}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _owner(client: httpx.AsyncClient, file_path: str) -> tuple[str, int] | None:
    """("series", Sonarr id) or ("movies", Radarr id) for the file."""
    from backend import arr
    settings = await arr._get_arr_settings()
    services = {"tv": ("sonarr",), "movie": ("radarr",)}.get(arr._detect_media_type(file_path), ("sonarr", "radarr"))
    for service in services:
        url = (settings.get(f"{service}_url") or "").rstrip("/")
        key = settings.get(f"{service}_api_key") or ""
        if not url or not key:
            continue
        path = arr._translate_path(file_path, settings.get(f"{service}_path_mapping", ""))
        find = arr._find_sonarr_series_for_path if service == "sonarr" else arr._find_radarr_movie_for_path
        item = await find(client, url, key, path)
        if item:
            return ("series" if service == "sonarr" else "movies", item["id"])
    return None


async def rescan_for_file(file_path: str) -> dict | None:
    """Have Bazarr re-sync and scan the file's series / movie. None when
    Bazarr isn't set up or the file isn't in Sonarr / Radarr."""
    url, api_key = await _settings()
    if not url or not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            owner = await _owner(client, file_path)
        if owner is None:
            return None
        kind, item_id = owner
        id_param = "seriesid" if kind == "series" else "radarrid"
        ok = True
        # Bazarr answers once it has scanned the series — longer than 30 s
        # for one with many seasons — so no read timeout (v0.10.0); this
        # runs in the background (schedule_rescan).
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client:
            for action in _ACTIONS:
                resp = await client.patch(f"{url}/api/{kind}", params={id_param: item_id, "action": action},
                                          headers={"X-API-KEY": api_key})
                ok = ok and resp.status_code < 300
    except Exception as exc:
        error = describe_error(exc)
        print(f"[BAZARR] Rescan failed for {file_path}: {error}", flush=True)
        return {"ok": False, "error": error}
    print(f"[BAZARR] Rescan of {kind} {item_id} {'requested' if ok else 'failed'}", flush=True)
    return {"ok": ok, "kind": kind, "id": item_id}


def _reason(resp: httpx.Response) -> str:
    """Bazarr's error: its message (a JSON string or {"message"}), or the status."""
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if isinstance(body, dict):
        body = body.get("message") or body.get("error") or ""
    text = str(body or "").strip()
    return f"HTTP {resp.status_code}: {text}" if text else f"HTTP {resp.status_code}"


async def _search_missing(url: str, api_key: str, kind: str, item_id: int) -> dict:
    """Bazarr's search for the movie's / series' missing subtitles. Bazarr
    answers once it has searched every provider — minutes for a series —
    so no read timeout; the outcome goes to the log."""
    id_param = "seriesid" if kind == "series" else "radarrid"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client:
            resp = await client.patch(f"{url}/api/{kind}", params={id_param: item_id, "action": "search-missing"},
                                      headers={"X-API-KEY": api_key})
        error = _reason(resp) if resp.status_code >= 300 else None
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    if error:
        print(f"[BAZARR] Subtitle search of {kind} {item_id} failed: {error}", flush=True)
        return {"ok": False, "error": error}
    print(f"[BAZARR] Searched missing subtitles of {kind} {item_id}", flush=True)
    return {"ok": True}


# The searches Bazarr is running, by (kind, id): kept so a task isn't
# garbage-collected mid-search, and so a second click doesn't start another.
_searches: dict[tuple[str, int], asyncio.Task] = {}


async def search_missing_subtitles(file_path: str) -> dict:
    """Start Bazarr's search for the subtitles the file's movie / series is
    missing (a whole series: Bazarr has no such action for one episode), in
    the background. {"ok": True, "kind": "movies" | "series", "id",
    "already_running"}, or {"ok": False, "reason": "not_configured" |
    "not_found" | "lookup_failed", "error"}."""
    url, api_key = await _settings()
    if not url or not api_key:
        return {"ok": False, "reason": "not_configured", "error": "Bazarr isn't set up"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            owner = await _owner(client, file_path)
    except Exception as exc:
        print(f"[BAZARR] Sonarr / Radarr lookup failed for {file_path}: {exc!r}", flush=True)
        return {"ok": False, "reason": "lookup_failed", "error": str(exc) or type(exc).__name__}
    if owner is None:
        return {"ok": False, "reason": "not_found", "error": "not in Sonarr / Radarr"}
    running = owner in _searches
    if not running:
        task = asyncio.get_running_loop().create_task(_search_missing(url, api_key, *owner))
        _searches[owner] = task
        task.add_done_callback(lambda _t: _searches.pop(owner, None))
    return {"ok": True, "kind": owner[0], "id": owner[1], "already_running": running}


def schedule_rescan(file_path: str, delay: float = RESCAN_DELAY) -> None:
    """rescan_for_file in the background, after `delay` seconds."""
    async def later():
        await asyncio.sleep(delay)
        await rescan_for_file(file_path)
    try:
        asyncio.get_running_loop().create_task(later())
    except RuntimeError:
        pass
