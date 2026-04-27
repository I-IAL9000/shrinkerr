"""Poster metadata resolution, image caching, and Plex image proxy."""

import re
import base64
import asyncio
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from backend.database import DB_PATH
from backend.metadata import resolve_tmdb_key_sync

router = APIRouter(prefix="/api/posters")

CACHE_TTL_HOURS = 168  # 7 days


def parse_folder_name(folder_path: str) -> dict:
    """Extract title, year, IMDb ID, TVDB ID from a media folder path.

    Primary: looks for bracketed IDs like [tt1234567] and [tvdb-123456].
    Fallback: uses scene-style parser to extract title/year from folder names
    like 'Movie.Name.2024.1080p.BluRay' or plain 'Movie Name (2024)'.
    """
    parts = folder_path.rstrip("/").split("/")
    folder_name = parts[-1] if parts else ""
    if re.match(r"^(Season|Series|Specials)\b", folder_name, re.IGNORECASE):
        folder_name = parts[-2] if len(parts) > 1 else folder_name

    imdb_match = re.search(r"\[(tt\d+)\]", folder_name)
    tvdb_match = re.search(r"\[tvdb-(\d+)\]", folder_name)
    year_match = re.search(r"\((\d{4})\)", folder_name)
    title = folder_name
    title = re.sub(r"\s*\[(?:tt\d+|tvdb-\d+|imdb-\w+)\]", "", title)
    title = re.sub(r"\s*\(\d{4}\)", "", title)
    title = title.strip().rstrip(" -")

    # Fallback: if no media IDs found, use scene parser for cleaner title/year
    if not imdb_match and not tvdb_match:
        from backend.media_parser import parse_media_name
        parsed = parse_media_name(folder_name)
        if parsed.title:
            title = parsed.title
        if parsed.year and not year_match:
            year_match = None  # clear the match object
            return {
                "title": title or folder_name,
                "year": parsed.year,
                "imdb_id": None,
                "tvdb_id": None,
            }

    return {
        "title": title or folder_name,
        "year": year_match.group(1) if year_match else None,
        "imdb_id": imdb_match.group(1) if imdb_match else None,
        "tvdb_id": tvdb_match.group(1) if tvdb_match else None,
    }


async def _download_image(url: str, plex_url: str = "", plex_token: str = "") -> str | None:
    """Download an image and return base64-encoded data. Returns None on failure."""
    import httpx
    try:
        if url.startswith("/api/posters/image"):
            # Plex proxy URL — resolve to actual Plex URL
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(url)
            path = parse_qs(parsed.query).get("path", [""])[0]
            if not path or not plex_url:
                return None
            from urllib.parse import unquote
            actual_url = f"{plex_url}{unquote(path)}?X-Plex-Token={plex_token}"
        elif url.startswith("http"):
            actual_url = url
        else:
            return None

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(actual_url)
            if resp.status_code == 200 and len(resp.content) > 100:
                return base64.b64encode(resp.content).decode("ascii")
    except Exception:
        pass
    return None


def _get_imdb_rating(parsed: dict) -> float | None:
    """Get IMDb rating from the dataset."""
    from backend.imdb_ratings import get_rating
    imdb_id = parsed.get("imdb_id")
    if not imdb_id:
        return None
    r = get_rating(imdb_id)
    return r["rating"] if r else None


def _get_imdb_votes(parsed: dict) -> int | None:
    """Get IMDb vote count from the dataset."""
    from backend.imdb_ratings import get_rating
    imdb_id = parsed.get("imdb_id")
    if not imdb_id:
        return None
    r = get_rating(imdb_id)
    return r["votes"] if r else None


class ResolveRequest(BaseModel):
    paths: list[str]


@router.post("/resolve")
async def resolve_posters(req: ResolveRequest):
    """Batch-resolve poster metadata. Returns cached image data as base64 data URIs."""
    if not req.paths:
        return {}

    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = aiosqlite.Row
    try:
        result = {}
        uncached = []

        # Batch query for all requested paths at once
        placeholders = ",".join("?" for _ in req.paths)
        async with db.execute(
            f"SELECT folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type FROM poster_cache WHERE folder_path IN ({placeholders})",
            req.paths,
        ) as cur:
            cached_rows = {r["folder_path"]: r for r in await cur.fetchall()}

        needs_media_type = []  # cached but missing media_type
        for path in req.paths:
            row = cached_rows.get(path)
            if row:
                poster = None
                if row["image_data"]:
                    poster = f"data:image/jpeg;base64,{row['image_data']}"
                elif row["poster_url"]:
                    poster = row["poster_url"]
                result[path] = {
                    "title": row["title"],
                    "year": row["year"],
                    "poster_url": poster,
                    "source": row["source"],
                    "rating": row["rating"],
                    "genres": row["genres"],
                    "country": row["country"],
                    "media_type": row["media_type"],
                    "rating_source": "imdb" if row["rating"] else None,
                }
                if not row["media_type"]:
                    needs_media_type.append(path)
            else:
                uncached.append(path)

        if not uncached and not needs_media_type:
            return result

        # Skip TMDB / Plex lookups for paths inside "Other"-typed media dirs
        # (v0.3.33+). Those folders contain non-cataloguable content (home
        # videos, music, lectures, misc rips) where TMDB matches would be
        # spurious — we just write a placeholder result instead.
        from backend.media_paths import is_other_typed_dir
        other_paths: set[str] = set()
        for p in (uncached + needs_media_type):
            try:
                if await is_other_typed_dir(p):
                    other_paths.add(p)
            except Exception:
                pass
        if other_paths:
            for p in list(other_paths):
                # Mark in cache so we don't re-check on every refresh.
                parsed = parse_folder_name(p)
                entry = {
                    "title": parsed["title"], "year": parsed.get("year"),
                    "poster_url": None, "source": "other-skipped",
                    "rating": None, "votes": None,
                    "genres": None, "country": None,
                    "media_type": "other", "rating_source": None,
                }
                result[p] = entry
                await db.execute(
                    """INSERT OR REPLACE INTO poster_cache
                       (folder_path, title, year, poster_url, source, image_data,
                        rating, genres, country, media_type, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (p, parsed["title"], parsed.get("year"), None, "other-skipped", None,
                     None, None, None, "other", datetime.now(timezone.utc).isoformat()),
                )
            await db.commit()
            uncached = [p for p in uncached if p not in other_paths]
            needs_media_type = [p for p in needs_media_type if p not in other_paths]
            if not uncached and not needs_media_type:
                return result

        # Load API settings
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token', 'tmdb_api_key', 'plex_path_mapping')"
        ) as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]

        # Backfill media_type for cached entries missing it
        tmdb_key = resolve_tmdb_key_sync(settings.get("tmdb_api_key"))
        plex_url_bf = settings.get("plex_url", "").rstrip("/")
        plex_token_bf = settings.get("plex_token", "")
        path_mapping_bf = settings.get("plex_path_mapping", "")
        if needs_media_type:
            sem = asyncio.Semaphore(3)
            async def _backfill_one(path: str):
                async with sem:
                    parsed = parse_folder_name(path)
                    # Try IMDb/TVDB ID first (most reliable)
                    if tmdb_key and parsed.get("imdb_id"):
                        try:
                            _, _, meta = await _resolve_tmdb(parsed["imdb_id"], tmdb_key)
                            if meta.get("media_type"):
                                return path, meta["media_type"]
                        except Exception:
                            pass
                    if tmdb_key and parsed.get("tvdb_id"):
                        try:
                            _, _, meta = await _resolve_tmdb_tvdb(parsed["tvdb_id"], tmdb_key)
                            if meta.get("media_type"):
                                return path, meta["media_type"]
                        except Exception:
                            pass
                    # TMDB title search — only when no explicit ID was parsed.
                    # If [tvdb-N]/[ttN] is in the folder name and TMDB-find
                    # returned nothing, a title fallback could cross-contaminate
                    # media_type with a wrong-show match. v0.3.56+.
                    has_explicit_id = bool(parsed.get("imdb_id") or parsed.get("tvdb_id"))
                    if tmdb_key and not has_explicit_id:
                        try:
                            _, _, tmdb_meta = await _resolve_tmdb_search(parsed["title"], parsed.get("year"), tmdb_key)
                            if tmdb_meta.get("media_type"):
                                return path, tmdb_meta["media_type"]
                        except Exception:
                            pass
                    # Explicit-ID, no TMDB record → fall back to TVDB-implies-TV.
                    # TVDB IDs are TV-show-specific in the wild (TheTVDB
                    # historically; even after movies were added, [tvdb-N] in
                    # the folder name almost always means TV). Better than
                    # leaving media_type unset. v0.3.56+.
                    if has_explicit_id and parsed.get("tvdb_id"):
                        return path, "tv"
                    # Fall back to Plex (knows the type from the library)
                    if plex_url_bf and plex_token_bf:
                        try:
                            _, _, plex_meta = await _resolve_plex(path, parsed, plex_url_bf, plex_token_bf, path_mapping_bf)
                            if plex_meta.get("media_type"):
                                return path, plex_meta["media_type"]
                        except Exception:
                            pass
                    return path, None
            results_mt = await asyncio.gather(*[_backfill_one(p) for p in needs_media_type])
            for path, mt in results_mt:
                if mt:
                    result[path]["media_type"] = mt
                    await db.execute("UPDATE poster_cache SET media_type = ? WHERE folder_path = ?", (mt, path))
            await db.commit()

        plex_url = settings.get("plex_url", "").rstrip("/")
        plex_token = settings.get("plex_token", "")
        tmdb_key = resolve_tmdb_key_sync(settings.get("tmdb_api_key"))

        for path in uncached:
            parsed = parse_folder_name(path)
            poster_url = None
            source = "placeholder"
            image_data = None
            tmdb_meta = {}

            # 1. Try Plex
            if plex_url and plex_token:
                try:
                    poster_url, source, tmdb_meta = await _resolve_plex(
                        path, parsed, plex_url, plex_token, settings.get("plex_path_mapping", "")
                    )
                except Exception as exc:
                    print(f"[POSTER] Plex failed for '{parsed['title']}': {exc}", flush=True)

            # 2. Try TMDB by IMDb ID (exact match)
            # Gate on `source == "placeholder"` rather than `not poster_url`:
            # a TMDB ID hit without a poster is still an authoritative match
            # (correct title/year/genres/media_type) and should preempt the
            # fuzzy title-search fallback that on common titles
            # ("Titanic", "Vanity Fair", "The Watch") will happily resolve
            # to the wrong year or wrong medium. v0.3.56+.
            if source == "placeholder" and tmdb_key and parsed.get("imdb_id"):
                try:
                    poster_url, source, tmdb_meta = await _resolve_tmdb(parsed["imdb_id"], tmdb_key)
                except Exception:
                    pass

            # 3. Try TMDB by TVDB ID (exact match for TV shows)
            if source == "placeholder" and tmdb_key and parsed.get("tvdb_id"):
                try:
                    poster_url, source, tmdb_meta = await _resolve_tmdb_tvdb(parsed["tvdb_id"], tmdb_key)
                except Exception:
                    pass

            # 4. Try TMDB search by title+year (fallback) — but ONLY when no
            # explicit ID was present. If the user's folder name embedded a
            # tvdb-/tt- ID and TMDB simply doesn't have the title yet
            # (brand-new shows, obscure regional series), guessing via title
            # is worse than honestly showing a placeholder with the parsed
            # title, since the guess can mismatch a same-titled different
            # show. v0.3.56+.
            has_explicit_id = bool(parsed.get("imdb_id") or parsed.get("tvdb_id"))
            if source == "placeholder" and not has_explicit_id and tmdb_key and parsed.get("title"):
                try:
                    poster_url, source, tmdb_meta = await _resolve_tmdb_search(parsed["title"], parsed.get("year"), tmdb_key)
                except Exception:
                    pass

            # Download and cache the image
            if poster_url:
                image_data = await _download_image(poster_url, plex_url, plex_token)

            entry = {
                "title": parsed["title"],
                "year": parsed.get("year"),
                "poster_url": f"data:image/jpeg;base64,{image_data}" if image_data else poster_url,
                "source": source,
                "rating": _get_imdb_rating(parsed) or tmdb_meta.get("rating"),
                "votes": _get_imdb_votes(parsed),
                "genres": tmdb_meta.get("genres"),
                "country": tmdb_meta.get("country"),
                "media_type": tmdb_meta.get("media_type"),
                "rating_source": "imdb" if _get_imdb_rating(parsed) else ("tmdb" if tmdb_meta.get("rating") else None),
            }
            result[path] = entry

            # Cache
            await db.execute(
                """INSERT OR REPLACE INTO poster_cache
                   (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (path, parsed["title"], parsed.get("year"), poster_url, source, image_data,
                 tmdb_meta.get("rating"), tmdb_meta.get("genres"), tmdb_meta.get("country"), tmdb_meta.get("media_type"),
                 datetime.now(timezone.utc).isoformat()),
            )

        await db.commit()
        return result
    finally:
        await db.close()


# --- Prefetch endpoint (called after scan or manually) ---

_prefetch_task: asyncio.Task | None = None


@router.post("/prefetch")
async def start_prefetch():
    """Start bulk poster prefetch for all scanned titles. Returns immediately."""
    global _prefetch_task
    if _prefetch_task and not _prefetch_task.done():
        return {"status": "already_running"}
    _prefetch_task = asyncio.create_task(_run_prefetch())
    return {"status": "started"}


@router.get("/prefetch-status")
async def prefetch_status():
    """Get prefetch progress."""
    return _prefetch_progress.copy()


_prefetch_progress = {"status": "idle", "total": 0, "resolved": 0, "cached": 0}


async def _run_prefetch():
    """Prefetch posters for all title-level folders. Uses short DB connections to avoid locking."""
    global _prefetch_progress
    _prefetch_progress = {"status": "running", "total": 0, "resolved": 0, "cached": 0}
    print("[POSTER] Prefetch starting...", flush=True)

    try:
        # Step 1: Get all file paths (short DB connection)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=10000")
        try:
            async with db.execute(
                "SELECT DISTINCT file_path FROM scan_results WHERE removed_from_list = 0"
            ) as cur:
                all_paths = [r["file_path"] for r in await cur.fetchall()]

            # Load settings
            settings = {}
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token', 'tmdb_api_key', 'plex_path_mapping')"
            ) as cur:
                for row in await cur.fetchall():
                    settings[row["key"]] = row["value"]

            # Check which already have cached images
            already_cached = set()
            async with db.execute(
                "SELECT folder_path FROM poster_cache WHERE image_data IS NOT NULL"
            ) as cur:
                already_cached = {r["folder_path"] for r in await cur.fetchall()}
        finally:
            await db.close()

        # Step 2: Extract unique title folders
        title_folders = set()
        for fp in all_paths:
            parts = fp.split("/")
            for i, part in enumerate(parts):
                if re.search(r"\[(?:tvdb-\d+|tt\d+)\]", part):
                    title_folders.add("/".join(parts[:i + 1]))
                    break
            else:
                parent = "/".join(parts[:-1])
                if parent:
                    title_folders.add(parent)

        folder_list = sorted(title_folders)
        to_resolve = [p for p in folder_list if p not in already_cached]
        _prefetch_progress["total"] = len(folder_list)
        _prefetch_progress["cached"] = len(already_cached & title_folders)
        _prefetch_progress["resolved"] = _prefetch_progress["cached"]

        print(f"[POSTER] {len(folder_list)} titles total, {len(already_cached & title_folders)} cached, {len(to_resolve)} to fetch", flush=True)

        plex_url = settings.get("plex_url", "").rstrip("/")
        plex_token = settings.get("plex_token", "")
        tmdb_key = resolve_tmdb_key_sync(settings.get("tmdb_api_key"))
        path_mapping = settings.get("plex_path_mapping", "")

        # Step 3: Sequential resolution with rate limiting + batch DB writes
        # TMDB rate limit: 40 requests/10 seconds. We pace at ~3 req/sec to stay under.
        now_str = datetime.now(timezone.utc).isoformat()
        sem = asyncio.Semaphore(3)
        _INSERT_SQL = """INSERT OR REPLACE INTO poster_cache
            (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

        async def _do_one(path: str) -> tuple | None:
            async with sem:
                await asyncio.sleep(0.35)  # ~3 req/sec to respect TMDB rate limits
                parsed = parse_folder_name(path)
                poster_url = None
                source = "placeholder"
                tmdb_meta = {}
                image_data = None

                # Gate on `source == "placeholder"` not `not poster_url` so an
                # ID match without a poster image still counts as definitive
                # and skips the title-search fuzzy fallback. Title search only
                # runs when no explicit ID was parsed — see the resolve_posters
                # caller for the full rationale. v0.3.56+.
                if tmdb_key and parsed.get("imdb_id"):
                    try: poster_url, source, tmdb_meta = await _resolve_tmdb(parsed["imdb_id"], tmdb_key)
                    except Exception as e:
                        if "429" in str(e):
                            await asyncio.sleep(5)
                if source == "placeholder" and tmdb_key and parsed.get("tvdb_id"):
                    try: poster_url, source, tmdb_meta = await _resolve_tmdb_tvdb(parsed["tvdb_id"], tmdb_key)
                    except Exception as e:
                        if "429" in str(e):
                            await asyncio.sleep(5)
                _has_explicit_id = bool(parsed.get("imdb_id") or parsed.get("tvdb_id"))
                if source == "placeholder" and not _has_explicit_id and tmdb_key and parsed.get("title") and len(parsed["title"]) >= 3:
                    try: poster_url, source, tmdb_meta = await _resolve_tmdb_search(parsed["title"], parsed.get("year"), tmdb_key)
                    except Exception as e:
                        if "429" in str(e):
                            await asyncio.sleep(5)
                # Skip Plex in bulk prefetch (too slow, causes DB lock contention with queue)

                if poster_url:
                    image_data = await _download_image(poster_url, plex_url, plex_token)

                # Prefer IMDb rating over TMDB
                rating = _get_imdb_rating(parsed) or tmdb_meta.get("rating")

                return (path, parsed["title"], parsed.get("year"), poster_url, source, image_data,
                        rating, tmdb_meta.get("genres"), tmdb_meta.get("country"), tmdb_meta.get("media_type"),
                        now_str)

        # Process in chunks of 20 (rate-limited HTTP, batch DB write)
        for ci in range(0, len(to_resolve), 20):
            chunk = to_resolve[ci:ci + 20]
            results = await asyncio.gather(*[_do_one(p) for p in chunk], return_exceptions=True)
            batch = [r for r in results if isinstance(r, tuple)]

            if batch:
                for attempt in range(3):
                    try:
                        db_w = await aiosqlite.connect(DB_PATH)
                        await db_w.execute("PRAGMA journal_mode=WAL")
                        await db_w.execute("PRAGMA busy_timeout=30000")
                        try:
                            await db_w.executemany(_INSERT_SQL, batch)
                            await db_w.commit()
                        finally:
                            await db_w.close()
                        break
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(2)

            _prefetch_progress["resolved"] = _prefetch_progress["cached"] + ci + len(chunk)
            if (ci + len(chunk)) % 100 < 20:
                print(f"[POSTER] Progress: {ci + len(chunk)}/{len(to_resolve)}", flush=True)

        _prefetch_progress["status"] = "done"
        print(f"[POSTER] Prefetch complete: {_prefetch_progress['resolved']}/{_prefetch_progress['total']}", flush=True)

    except Exception as exc:
        _prefetch_progress["status"] = f"error: {exc}"
        print(f"[POSTER] Prefetch failed: {exc}", flush=True)
        import traceback
        traceback.print_exc()


# --- Plex image proxy (for direct image loading) ---

@router.get("/image")
async def proxy_plex_image(path: str):
    """Proxy Plex thumbnail images, injecting the token server-side."""
    import httpx
    from urllib.parse import unquote

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token')"
        ) as cur:
            settings = {r["key"]: r["value"] async for r in cur}
    finally:
        await db.close()

    plex_url = settings.get("plex_url", "").rstrip("/")
    plex_token = settings.get("plex_token", "")
    if not plex_url or not plex_token:
        raise HTTPException(status_code=503, detail="Plex not configured")

    decoded_path = unquote(path)
    image_url = f"{plex_url}{decoded_path}?X-Plex-Token={plex_token}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(image_url)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Plex returned {resp.status_code}")
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# --- Resolution helpers ---

async def _resolve_plex(folder_path, parsed, plex_url, plex_token, path_mapping):
    import httpx
    from backend.plex import _translate_path, get_plex_libraries
    from urllib.parse import quote

    headers = {"X-Plex-Token": plex_token, "Accept": "application/json"}
    title = parsed["title"]
    year = parsed.get("year")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            libraries = await get_plex_libraries(plex_url, plex_token)
        except Exception:
            libraries = []

        search_path = _translate_path(folder_path, path_mapping) if path_mapping else folder_path

        for lib in libraries:
            section_id = lib.get("id")
            if not section_id:
                continue
            lib_paths = lib.get("paths", [])
            if not any(search_path.startswith(lp.rstrip("/") + "/") or search_path.startswith(lp) for lp in lib_paths):
                continue

            lib_media_type = "movie" if lib.get("type") == "movie" else "tv"
            try:
                resp = await client.get(
                    f"{plex_url}/library/sections/{section_id}/search",
                    params={"type": 1 if lib.get("type") == "movie" else 2, "query": title},
                    headers=headers,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("MediaContainer", {}).get("Metadata", []):
                        item_title = (item.get("title") or "").lower().strip()
                        if item_title == title.lower().strip():
                            thumb = item.get("thumb")
                            if thumb:
                                return f"/api/posters/image?path={quote(thumb, safe='')}", "plex", {"media_type": lib_media_type}
            except Exception:
                pass

        # Global fallback — derive media_type from Plex item type
        try:
            resp = await client.get(f"{plex_url}/search", params={"query": title}, headers=headers)
            if resp.status_code == 200:
                for item in resp.json().get("MediaContainer", {}).get("Metadata", []):
                    if (item.get("title") or "").lower().strip() == title.lower().strip():
                        thumb = item.get("thumb")
                        if thumb:
                            plex_type = item.get("type")  # "movie" | "show" | etc.
                            mt = "movie" if plex_type == "movie" else "tv" if plex_type == "show" else None
                            return f"/api/posters/image?path={quote(thumb, safe='')}", "plex", ({"media_type": mt} if mt else {})
        except Exception:
            pass

    return None, "placeholder", {}


async def _resolve_tmdb(imdb_id, api_key):
    """Resolve by IMDb ID — exact match, most reliable.

    Returns (poster_url_or_None, source, meta). When TMDB has the title
    registered but no poster image, source is still "tmdb" and meta is
    populated — the caller treats this as an authoritative match and
    skips the fuzzy title-search fallback (which on an obscure or
    brand-new title would happily return the wrong year's "Vanity Fair"
    or similar). v0.3.56+.
    """
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": api_key, "external_source": "imdb_id"},
        )
        if resp.status_code == 200:
            for key in ["movie_results", "tv_results"]:
                items = resp.json().get(key, [])
                if not items:
                    continue
                item = items[0]  # /find returns at most one for a given external ID
                media_type = "movie" if key == "movie_results" else "tv"
                meta = _extract_tmdb_meta(item, media_type, api_key)
                poster = item.get("poster_path")
                poster_url = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None
                return poster_url, "tmdb", meta
    return None, "placeholder", {}


async def _resolve_tmdb_tvdb(tvdb_id, api_key):
    """Resolve by TVDB ID — exact match for TV shows.

    See _resolve_tmdb for the no-poster handling rationale. TVDB IDs
    are TV-show specific in the wild, so a match here pins media_type
    to "tv" reliably and the caller will not run the multi-search
    fallback that could otherwise return a same-titled movie.
    v0.3.56+.
    """
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{tvdb_id}",
            params={"api_key": api_key, "external_source": "tvdb_id"},
        )
        if resp.status_code == 200:
            for key in ["tv_results", "movie_results"]:
                items = resp.json().get(key, [])
                if not items:
                    continue
                item = items[0]
                media_type = "tv" if key == "tv_results" else "movie"
                meta = _extract_tmdb_meta(item, media_type, api_key)
                poster = item.get("poster_path")
                poster_url = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None
                return poster_url, "tmdb", meta
    return None, "placeholder", {}


# Country code to full name
_COUNTRY_NAMES = {
    "US": "United States", "GB": "Great Britain", "UK": "Great Britain",
    "CA": "Canada", "AU": "Australia", "NZ": "New Zealand",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "IN": "India",
    "BR": "Brazil", "MX": "Mexico", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "IS": "Iceland", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "IE": "Ireland",
    "PT": "Portugal", "PL": "Poland", "CZ": "Czech Republic", "RU": "Russia",
    "TR": "Turkey", "ZA": "South Africa", "AR": "Argentina", "CO": "Colombia",
    "TH": "Thailand", "PH": "Philippines", "IL": "Israel", "EG": "Egypt",
    "TW": "Taiwan", "HK": "Hong Kong", "SG": "Singapore", "MY": "Malaysia",
}

# TMDB genre ID mapping (common genres)
_TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics",
}


def _extract_tmdb_meta(item: dict, media_type: str, api_key: str) -> dict:
    """Extract metadata from a TMDB result item."""
    genre_ids = item.get("genre_ids", [])
    genres = [_TMDB_GENRES.get(g, "") for g in genre_ids]
    genres = [g for g in genres if g]

    return {
        "rating": round(item.get("vote_average", 0), 1) if item.get("vote_average") else None,
        "genres": ", ".join(genres[:3]) if genres else None,
        "country": ", ".join(_COUNTRY_NAMES.get(c, c) for c in item.get("origin_country", [])[:2]) if item.get("origin_country") else None,
        "media_type": media_type,
    }


async def _resolve_tmdb_search(title, year, api_key):
    """Search by title+year — requires strict matching to avoid mismatches."""
    import httpx
    if len(title) < 3:
        return None, "placeholder", {}

    async with httpx.AsyncClient(timeout=10) as client:
        params = {"api_key": api_key, "query": title}
        if year:
            params["year"] = year
        resp = await client.get("https://api.themoviedb.org/3/search/multi", params=params)
        if resp.status_code == 200:
            title_lower = title.lower().strip()
            results = resp.json().get("results", [])

            def _match_return(item):
                mt = item.get("media_type", "movie")
                meta = _extract_tmdb_meta(item, mt, api_key)
                # Poster is optional — a title with no TMDB poster is still a
                # better match than falling through to a wrong-year/wrong-show
                # fuzzy guess. Render placeholder, keep the meta. v0.3.56+.
                poster = item.get("poster_path")
                poster_url = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None
                return poster_url, "tmdb", meta

            # Pass 1: exact title + year match
            if year:
                for item in results:
                    item_title = (item.get("title") or item.get("name") or "").lower().strip()
                    item_year = str(item.get("release_date", item.get("first_air_date", ""))[:4])
                    if item_title == title_lower and item_year == year:
                        return _match_return(item)

            # Pass 2: exact title match (any year)
            for item in results:
                item_title = (item.get("title") or item.get("name") or "").lower().strip()
                if item_title == title_lower:
                    return _match_return(item)

            # Pass 3: partial title + year validation
            for item in results:
                item_title = (item.get("title") or item.get("name") or "").lower().strip()
                if title_lower in item_title or item_title in title_lower:
                    if year:
                        item_year = str(item.get("release_date", item.get("first_air_date", ""))[:4])
                        if item_year == year:
                            return _match_return(item)
                    else:
                        return _match_return(item)
    return None, "placeholder", {}


# ---------------------------------------------------------------------------
# Manual TMDB search & override (user picks a match when auto-detection fails)
# ---------------------------------------------------------------------------

class TMDBSearchRequest(BaseModel):
    query: str
    year: str | None = None


@router.post("/search")
async def search_tmdb(req: TMDBSearchRequest):
    """Search TMDB for possible matches. Returns up to 10 candidates for user selection."""
    if not req.query or len(req.query.strip()) < 2:
        return {"results": []}

    from backend.config import settings as app_settings
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        tmdb_key = ""
        async with db.execute("SELECT value FROM settings WHERE key = 'tmdb_api_key'") as cur:
            row = await cur.fetchone()
            if row:
                tmdb_key = row["value"]
    finally:
        await db.close()

    if not tmdb_key:
        raise HTTPException(400, "TMDB API key not configured")

    import httpx
    params = {"api_key": tmdb_key, "query": req.query.strip()}
    if req.year:
        params["year"] = req.year

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.themoviedb.org/3/search/multi", params=params)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"TMDB returned {resp.status_code}")
        results = resp.json().get("results", [])
    except httpx.RequestError as exc:
        raise HTTPException(502, f"TMDB request failed: {exc}")

    # Filter to movie/tv only and normalize
    out = []
    for item in results:
        mt = item.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name") or "Unknown"
        release_date = item.get("release_date") or item.get("first_air_date") or ""
        year = release_date[:4] if release_date else None
        poster_path = item.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None
        out.append({
            "tmdb_id": item.get("id"),
            "media_type": mt,
            "title": title,
            "year": year,
            "poster_url": poster_url,
            "overview": (item.get("overview") or "")[:200],
            "rating": round(item.get("vote_average", 0), 1) if item.get("vote_average") else None,
        })
        if len(out) >= 10:
            break

    return {"results": out}


class OverrideRequest(BaseModel):
    folder_path: str
    tmdb_id: int
    media_type: str   # 'movie' or 'tv'


@router.post("/override")
async def override_poster(req: OverrideRequest):
    """Replace the cached poster metadata for a folder with a user-selected TMDB match."""
    if req.media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be 'movie' or 'tv'")

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'tmdb_api_key'") as cur:
            row = await cur.fetchone()
            tmdb_key = row["value"] if row else ""
    finally:
        await db.close()

    if not tmdb_key:
        raise HTTPException(400, "TMDB API key not configured")

    # Fetch the specific TMDB item details
    import httpx
    detail_url = f"https://api.themoviedb.org/3/{req.media_type}/{req.tmdb_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(detail_url, params={"api_key": tmdb_key})
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"TMDB returned {resp.status_code}")
        data = resp.json()
    except httpx.RequestError as exc:
        raise HTTPException(502, f"TMDB request failed: {exc}")

    title = data.get("title") or data.get("name") or "Unknown"
    release_date = data.get("release_date") or data.get("first_air_date") or ""
    year = release_date[:4] if release_date else None
    poster_path = data.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w300{poster_path}" if poster_path else None

    genres = data.get("genres") or []
    genre_names = ", ".join(g.get("name", "") for g in genres[:3] if g.get("name"))
    countries = data.get("origin_country") or []
    country_names = ", ".join(_COUNTRY_NAMES.get(c, c) for c in countries[:2])
    rating = round(data.get("vote_average", 0), 1) if data.get("vote_average") else None

    # Download poster image
    image_data = None
    if poster_url:
        image_data = await _download_image(poster_url, "", "")

    # Upsert into poster_cache
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            """INSERT OR REPLACE INTO poster_cache
               (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.folder_path, title, year, poster_url, "tmdb-manual", image_data,
             rating, genre_names or None, country_names or None, req.media_type,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    return {
        "title": title,
        "year": year,
        "poster_url": f"data:image/jpeg;base64,{image_data}" if image_data else poster_url,
        "media_type": req.media_type,
        "rating": rating,
        "genres": genre_names or None,
        "country": country_names or None,
        "source": "tmdb-manual",
    }
