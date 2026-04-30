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


# Pre-compiled regexes for media-ID extraction. We accept three families
# of formatting users have in the wild, with brackets-or-braces + bare.
# v0.3.85+ broadened from the bracket-only patterns we used to ship
# (e.g. `[tt1234567]`) to also catch:
#   * Curly-brace style — Plex's `{tmdb-12345}` convention
#   * "id" suffix — Jellyfin's `[tmdbid-12345]` / `[tvdbid-12345]`
#   * Bare style — `tt1234567` / `tmdb-12345` / `tvdb-12345` without
#     enclosing brackets, common when users mark individual filenames
#     rather than folders.
#
# Bracketed/braced patterns are tried first because they're the most
# specific (false-positive proof). Bare patterns fall through with
# additional guards to avoid matching inside other tokens — IMDb bare
# requires 7+ digits (the canonical IMDb ID width), TVDB/TMDB bare
# require an explicit `-` or `=` separator after the prefix.
_RE_IMDB_BRACKETED = re.compile(r'[\[\{(](tt\d+)[\]\})]', re.IGNORECASE)
_RE_TVDB_BRACKETED = re.compile(r'[\[\{(]tvdb(?:id)?[-=:]?(\d+)[\]\})]', re.IGNORECASE)
_RE_TMDB_BRACKETED = re.compile(r'[\[\{(]tmdb(?:id)?[-=:]?(\d+)[\]\})]', re.IGNORECASE)
_RE_IMDB_BARE = re.compile(r'(?<![A-Za-z0-9])(tt\d{7,})(?![A-Za-z0-9])')
_RE_TVDB_BARE = re.compile(r'(?<![A-Za-z0-9])tvdb(?:id)?[-=](\d+)(?![A-Za-z0-9])', re.IGNORECASE)
_RE_TMDB_BARE = re.compile(r'(?<![A-Za-z0-9])tmdb(?:id)?[-=](\d+)(?![A-Za-z0-9])', re.IGNORECASE)


def _extract_ids(text: str) -> tuple[str | None, str | None, str | None]:
    """Find the first IMDb/TVDB/TMDB ID in `text`. Returns (imdb, tvdb, tmdb).

    Bracketed/braced forms are tried before bare forms because they're
    less false-positive prone. Each ID is independent: a string with
    both `[tt1234567]` and `tmdb-99999` returns both.
    """
    imdb = tvdb = tmdb = None
    for rx in (_RE_IMDB_BRACKETED, _RE_IMDB_BARE):
        m = rx.search(text)
        if m:
            imdb = m.group(1)
            break
    for rx in (_RE_TVDB_BRACKETED, _RE_TVDB_BARE):
        m = rx.search(text)
        if m:
            tvdb = m.group(1)
            break
    for rx in (_RE_TMDB_BRACKETED, _RE_TMDB_BARE):
        m = rx.search(text)
        if m:
            tmdb = m.group(1)
            break
    return imdb, tvdb, tmdb


def parse_folder_name(folder_path: str, *, walk_files: bool = True) -> dict:
    """Extract title, year, IMDb ID, TVDB ID, TMDB ID from a media folder path.

    Resolution priority:
      1. Bracketed/braced IDs in the FOLDER NAME (`[tt1234567]`,
         `{tmdb-12345}`, `[tvdbid-99999]`, …) — most specific.
      2. Same patterns in any FILENAME inside the folder (when
         `walk_files=True` — the default). Lets users who tag
         individual files rather than the parent folder still get
         exact-ID resolution. v0.3.85+.
      3. Bare ID forms (`tt1234567`, `tmdb-12345`) in folder name or
         filenames, with conservative regex guards.
      4. Scene-style title/year parser as a final fallback.

    `walk_files=False` skips disk access — used by callers that
    already iterate filenames or where the folder isn't expected to
    exist on this host's filesystem.
    """
    parts = folder_path.rstrip("/").split("/")
    folder_name = parts[-1] if parts else ""
    if re.match(r"^(Season|Series|Specials)\b", folder_name, re.IGNORECASE):
        folder_name = parts[-2] if len(parts) > 1 else folder_name

    imdb_id, tvdb_id, tmdb_id = _extract_ids(folder_name)
    year_match = re.search(r"\((\d{4})\)", folder_name)

    # Strip ID brackets/braces and `(YYYY)` from the title text.
    title = folder_name
    title = re.sub(r"\s*[\[\{(](?:tt\d+|tvdb(?:id)?[-=:]?\d+|tmdb(?:id)?[-=:]?\d+|imdb-\w+)[\]\})]", "", title)
    title = re.sub(r"\s*\b(?:tt\d{7,}|tvdb(?:id)?[-=]\d+|tmdb(?:id)?[-=]\d+)\b", "", title)
    title = re.sub(r"\s*\(\d{4}\)", "", title)
    title = title.strip().rstrip(" -")

    # Fallback path 1: walk individual filenames for IDs the folder name
    # doesn't carry. Common for users whose Sonarr/Radarr config writes
    # the ID into the file name only. Best-effort — listdir silently
    # skipped on permission / not-found errors. v0.3.85+.
    if walk_files and not (imdb_id or tvdb_id or tmdb_id):
        try:
            import os as _os
            for name in _os.listdir(folder_path):
                f_imdb, f_tvdb, f_tmdb = _extract_ids(name)
                if f_imdb or f_tvdb or f_tmdb:
                    imdb_id = imdb_id or f_imdb
                    tvdb_id = tvdb_id or f_tvdb
                    tmdb_id = tmdb_id or f_tmdb
                    break  # First file with any ID wins
        except OSError:
            pass

    # Fallback path 2: scene-style parser for title/year.
    if not (imdb_id or tvdb_id or tmdb_id):
        from backend.media_parser import parse_media_name
        parsed = parse_media_name(folder_name)
        if parsed.title:
            title = parsed.title
        if parsed.year and not year_match:
            return {
                "title": title or folder_name,
                "year": parsed.year,
                "imdb_id": None,
                "tvdb_id": None,
                "tmdb_id": None,
            }

    return {
        "title": title or folder_name,
        "year": year_match.group(1) if year_match else None,
        "imdb_id": imdb_id,
        "tvdb_id": tvdb_id,
        "tmdb_id": tmdb_id,
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

        # Media-dir label index for type inference when a folder lacks a
        # bracket ID. Loaded once for the whole batch and prefix-matched
        # in-memory per-folder. Same data structure as scan.py's
        # _build_dir_label_index. Defined here (above the backfill block)
        # so both _backfill_one and the main resolve loop below can use
        # the same hint via the closure. v0.3.82+.
        dir_label_pairs: list[tuple[str, str]] = []
        try:
            async with db.execute("SELECT path, label FROM media_dirs WHERE enabled = 1") as cur:
                for r in await cur.fetchall():
                    p = (r["path"] or "").rstrip("/") + "/"
                    lbl = (r["label"] or "").strip().lower()
                    if p and lbl:
                        dir_label_pairs.append((p, lbl))
            dir_label_pairs.sort(key=lambda t: len(t[0]), reverse=True)
        except Exception:
            pass

        def _media_type_hint_for(path: str) -> str | None:
            """Return movie/tv/None based on which media-dir contains `path`."""
            for prefix, label in dir_label_pairs:
                if path.startswith(prefix):
                    return _label_to_media_type(label)
            return None

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
                    # Title-search fallback runs whether or not an ID was
                    # parsed (v0.3.91+ — see resolve_posters above for
                    # rationale). Type hint priority: bracket family,
                    # then dir label.
                    if tmdb_key:
                        hint = _media_type_hint_from_parsed(parsed) or _media_type_hint_for(path)
                        try:
                            _, _, tmdb_meta = await _resolve_tmdb_search(
                                parsed["title"], parsed.get("year"), tmdb_key,
                                media_type_hint=hint,
                            )
                            if tmdb_meta.get("media_type"):
                                return path, tmdb_meta["media_type"]
                        except Exception:
                            pass
                    # If the title search returned nothing but the dir-label
                    # tells us the type, propagate that as a final fallback
                    # so movies / TV shows in user-labelled dirs get the
                    # right `media_type` even with no TMDB hit. v0.3.82+.
                    label_hint = _media_type_hint_for(path)
                    if not has_explicit_id and label_hint:
                        return path, label_hint
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
        plex_path_mapping = settings.get("plex_path_mapping", "")
        # `dir_label_pairs` and `_media_type_hint_for` are defined above
        # (before `_backfill_one`) so both helpers can share the closure.

        # Resolve all uncached paths concurrently. Pre-v0.3.63 this was a
        # plain `for path in uncached:` loop, which meant N sequential
        # round-trips to Plex/TMDB/the image CDN — for 30 new items that's
        # ~30s before the user sees anything. With a bounded semaphore
        # we run several resolutions in parallel; once the cache fills,
        # subsequent loads are instant. Concurrency=8 keeps us comfortably
        # below TMDB's 50 req/sec rate limit even if every path takes the
        # full chain (Plex → IMDb find → TVDB find → title search +
        # image download). v0.3.63.
        async def _resolve_one(path: str):
            parsed = parse_folder_name(path)
            poster_url = None
            source = "placeholder"
            image_data = None
            tmdb_meta: dict = {}

            # 1. Try Plex
            if plex_url and plex_token:
                try:
                    poster_url, source, tmdb_meta = await _resolve_plex(
                        path, parsed, plex_url, plex_token, plex_path_mapping
                    )
                except Exception as exc:
                    print(f"[POSTER] Plex failed for '{parsed['title']}': {exc}", flush=True)

            # 2. Try TMDB by IMDb ID (exact match)
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

            # 4. TMDB title search — runs as a last-resort fallback when
            # the upstream ID lookups failed AND we still have a title.
            # v0.3.91 dropped the v0.3.56 "skip title-search when an ID
            # is present" gate: the original concern was that title-
            # search would fuzzy-guess and pick a same-titled wrong-
            # medium / wrong-year entry, but since v0.3.82's media_type
            # hint and v0.3.83's year-strict pass ordering, title-
            # search is constrained enough that falling through is
            # safer than returning placeholder. Specifically: a
            # `[tvdb-N]` folder where TMDB doesn't have that TVDB ID
            # cross-referenced (common for brand-new shows) will now
            # match by title+year+TV-type instead of going unmatched.
            #
            # Hint priority: bracket family (most specific) →
            # containing media-dir's label → no constraint.
            if source == "placeholder" and tmdb_key and parsed.get("title"):
                hint = _media_type_hint_from_parsed(parsed) or _media_type_hint_for(path)
                try:
                    poster_url, source, tmdb_meta = await _resolve_tmdb_search(
                        parsed["title"], parsed.get("year"), tmdb_key,
                        media_type_hint=hint,
                    )
                except Exception:
                    pass

            if poster_url:
                image_data = await _download_image(poster_url, plex_url, plex_token)

            return path, parsed, poster_url, source, image_data, tmdb_meta

        sem = asyncio.Semaphore(8)
        async def _bounded(path: str):
            async with sem:
                return await _resolve_one(path)

        resolved = await asyncio.gather(*[_bounded(p) for p in uncached])

        # All network work done — now write all results to cache + result
        # in a single transaction so the per-row INSERT chain isn't N more
        # commits. v0.3.63.
        now_iso = datetime.now(timezone.utc).isoformat()
        for path, parsed, poster_url, source, image_data, tmdb_meta in resolved:
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

            await db.execute(
                """INSERT OR REPLACE INTO poster_cache
                   (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (path, parsed["title"], parsed.get("year"), poster_url, source, image_data,
                 tmdb_meta.get("rating"), tmdb_meta.get("genres"), tmdb_meta.get("country"), tmdb_meta.get("media_type"),
                 now_iso),
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


class ReResolveRequest(BaseModel):
    # "placeholder" — only entries that previously failed to resolve
    #                 (source = 'placeholder'). Cheap retry.
    # "auto"        — every auto-resolved entry. Destructive — wipes
    #                 working cached posters too. User-confirmed
    #                 from the UI. Always preserves manual fixes
    #                 (source = 'tmdb-manual') and Type=Other-skipped
    #                 entries (source = 'other-skipped').
    mode: str  # "placeholder" | "auto"


@router.post("/re-resolve")
async def re_resolve_posters(req: ReResolveRequest):
    """Evict matching poster_cache rows and re-trigger the prefetch.

    The bulk prefetch task (`_run_prefetch`) already iterates folders
    that don't have cached posters. By DELETE-ing the targeted rows
    first, we make them look uncached, and the existing pipeline
    handles re-resolution end-to-end (including the bracket-ID,
    dir-label, file-walking, and pass-ordering logic from
    v0.3.81–v0.3.85).

    Returns `{targeted, started}` so the UI can toast a count + show
    progress via the existing /posters/prefetch-status polling.
    """
    if req.mode == "placeholder":
        where = "source = 'placeholder'"
    elif req.mode == "auto":
        # 'tmdb' = auto via TMDB; 'plex' = auto via Plex.
        # Excludes:
        #   'tmdb-manual'   (user explicitly fixed → preserve)
        #   'other-skipped' (Type=Other dirs — intentional skip)
        where = "source IN ('tmdb', 'plex')"
    else:
        raise HTTPException(400, "mode must be 'placeholder' or 'auto'")

    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute(f"SELECT COUNT(*) FROM poster_cache WHERE {where}") as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
        if count > 0:
            await db.execute(f"DELETE FROM poster_cache WHERE {where}")
            await db.commit()
    finally:
        await db.close()

    global _prefetch_task
    started = False
    if count > 0 and (_prefetch_task is None or _prefetch_task.done()):
        _prefetch_task = asyncio.create_task(_run_prefetch())
        started = True
    return {"targeted": count, "started": started}


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

            # Media-dir label index for type inference when a folder lacks
            # a bracket ID. Same shape as the resolve_posters one above.
            # v0.3.82+.
            dir_label_pairs: list[tuple[str, str]] = []
            try:
                async with db.execute(
                    "SELECT path, label FROM media_dirs WHERE enabled = 1"
                ) as cur:
                    for row in await cur.fetchall():
                        p = (row["path"] or "").rstrip("/") + "/"
                        lbl = (row["label"] or "").strip().lower()
                        if p and lbl:
                            dir_label_pairs.append((p, lbl))
                dir_label_pairs.sort(key=lambda t: len(t[0]), reverse=True)
            except Exception:
                pass
        finally:
            await db.close()

        def _media_type_hint_for(path: str) -> str | None:
            for prefix, label in dir_label_pairs:
                if path.startswith(prefix):
                    return _label_to_media_type(label)
            return None

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
                # Title-search fallback runs whether or not an ID was
                # parsed (v0.3.91+ — see resolve_posters above).
                if source == "placeholder" and tmdb_key and parsed.get("title") and len(parsed["title"]) >= 3:
                    _hint = _media_type_hint_from_parsed(parsed) or _media_type_hint_for(path)
                    try: poster_url, source, tmdb_meta = await _resolve_tmdb_search(
                        parsed["title"], parsed.get("year"), tmdb_key,
                        media_type_hint=_hint,
                    )
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


def _media_type_hint_from_parsed(parsed: dict | None) -> str | None:
    """Infer the media_type from the bracket family in a parse_folder_name
    result. `[tvdb-N]` → tv; `[ttN]` or `[tmdb-N]` → movie; otherwise None.
    Used to narrow the title-search fallback when an explicit bracket ID
    was given but TMDB couldn't resolve it. v0.3.91+.
    """
    if not parsed:
        return None
    if parsed.get("tvdb_id"):
        return "tv"
    if parsed.get("imdb_id") or parsed.get("tmdb_id"):
        return "movie"
    return None


def _label_to_media_type(label: str | None) -> str | None:
    """Map a media-dir label to a TMDB media_type, or None if undetermined.

    "Movies" / "Movie" → "movie"; "TV Shows" / "TV Show" / "TV" → "tv";
    "Other" / "" / unknown → None (no constraint). Mirrors the same
    label vocabulary used by the Scanner's type filter (v0.3.76+).
    """
    if not label:
        return None
    norm = label.strip().lower()
    if norm in ("movies", "movie"):
        return "movie"
    if norm in ("tv shows", "tv show", "tv"):
        return "tv"
    return None


async def _resolve_tmdb_search(title, year, api_key, media_type_hint: str | None = None):
    """Search by title+year — requires strict matching to avoid mismatches.

    `media_type_hint` (v0.3.82+): when set to "movie" or "tv", filters
    /search/multi results to that type before the three matching passes.
    Lets users without bracket-ID folder naming still get type-correct
    matches by labelling their dirs in Settings → Directories: a movie
    titled the same as a popular TV show no longer mis-resolves to the
    show, and vice versa.
    """
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
            if media_type_hint in ("movie", "tv"):
                results = [r for r in results if r.get("media_type") == media_type_hint]

            def _match_return(item):
                mt = item.get("media_type", "movie")
                meta = _extract_tmdb_meta(item, mt, api_key)
                # Poster is optional — a title with no TMDB poster is still a
                # better match than falling through to a wrong-year/wrong-show
                # fuzzy guess. Render placeholder, keep the meta. v0.3.56+.
                poster = item.get("poster_path")
                poster_url = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None
                return poster_url, "tmdb", meta

            def _item_year(item: dict) -> str:
                return str(item.get("release_date", item.get("first_air_date", ""))[:4])

            # Pass ordering (v0.3.83+):
            #
            # When the user provided a year, run year-AWARE passes only.
            # Pre-v0.3.83 the order was:
            #   1) exact title + year  →  2) exact title any year  →
            #   3) partial title + year
            # which silently mis-resolved e.g. "See no evil (2014)" to the
            # 2006 movie of that title (the 2014 sequel is titled "See No
            # Evil 2", so step 1 missed; step 2's any-year exact match
            # picked the 2006 entry). Year-aware partial matching now
            # runs BEFORE year-blind exact matching, and we no longer fall
            # back to year-blind passes at all when a year was given —
            # better to return no match than the wrong year.
            #
            # When no year was given, pure title-based passes apply
            # (preserves the long-standing behaviour for users without
            # year-tagged folders).
            if year:
                # 1. Exact title + exact year
                for item in results:
                    it = (item.get("title") or item.get("name") or "").lower().strip()
                    if it == title_lower and _item_year(item) == year:
                        return _match_return(item)
                # 2. Partial title + exact year — catches "See no evil" → "See No
                #    Evil 2" (2014) and "Odyssey" → "The Odyssey" (2025).
                for item in results:
                    it = (item.get("title") or item.get("name") or "").lower().strip()
                    if (title_lower in it or it in title_lower) and _item_year(item) == year:
                        return _match_return(item)
                # 3. Exact title + ±1 year — covers the "TMDB has 2014, folder
                #    has 2013" metadata-drift case without admitting the
                #    "off by 8 years" wrong-movie cases that pre-v0.3.83
                #    Pass 2 was admitting.
                try:
                    target = int(year)
                    for item in results:
                        it = (item.get("title") or item.get("name") or "").lower().strip()
                        iy = _item_year(item)
                        if it != title_lower or not iy.isdigit():
                            continue
                        if abs(int(iy) - target) <= 1:
                            return _match_return(item)
                except ValueError:
                    pass
                # No year-aware match — return placeholder rather than
                # silently picking a wrong-year title-match.
                return None, "placeholder", {}

            # No-year path: original behaviour kept.
            # A. Exact title (any year)
            for item in results:
                it = (item.get("title") or item.get("name") or "").lower().strip()
                if it == title_lower:
                    return _match_return(item)
            # B. Partial title (any year)
            for item in results:
                it = (item.get("title") or item.get("name") or "").lower().strip()
                if title_lower in it or it in title_lower:
                    return _match_return(item)
    return None, "placeholder", {}


# ---------------------------------------------------------------------------
# Manual TMDB search & override (user picks a match when auto-detection fails)
# ---------------------------------------------------------------------------

class TMDBSearchRequest(BaseModel):
    query: str
    year: str | None = None
    # Optional folder path the search is for. When set, the backend parses
    # bracket IDs from it and uses them to (1) prepend the exact TMDB
    # record as the first result and (2) filter title-search candidates
    # to the matching media_type. Pre-v0.3.81 the modal only did a title
    # search, so a movie folder tagged `[tt4426738]` (Animals 2019) would
    # show 10 unrelated TV/movie results and the right answer was unreachable.
    folder_path: str | None = None


def _normalise_tmdb_item(item: dict, force_media_type: str | None = None) -> dict:
    """Convert a TMDB API item (search or find result) into our wire format."""
    mt = force_media_type or item.get("media_type") or "movie"
    title = item.get("title") or item.get("name") or "Unknown"
    release_date = item.get("release_date") or item.get("first_air_date") or ""
    year = release_date[:4] if release_date else None
    poster_path = item.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else None
    return {
        "tmdb_id": item.get("id"),
        "media_type": mt,
        "title": title,
        "year": year,
        "poster_url": poster_url,
        "overview": (item.get("overview") or "")[:200],
        "rating": round(item.get("vote_average", 0), 1) if item.get("vote_average") else None,
    }


@router.post("/search")
async def search_tmdb(req: TMDBSearchRequest):
    """Search TMDB for possible matches. Returns candidates for user selection.

    v0.3.81 changes:
      * `folder_path` (optional) — when set, parsed for bracket IDs:
        - `[ttN]` (IMDb) → /find with external_source=imdb_id, prepended
          as a movie match, AND title-search filtered to movies only.
        - `[tvdb-N]` → /find with external_source=tvdb_id, prepended as a
          TV match, title-search filtered to TV only.
        - `[tmdb-N]` → /movie/{id} (fall back to /tv/{id}) directly,
          prepended.
      * Title-search results re-ranked: exact title + year matches first.
    """
    if not req.query or len(req.query.strip()) < 2:
        return {"results": []}

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

    # Bracket-ID-driven exact lookup. If the folder name carries a Sonarr/
    # Radarr ID, the user has already told us exactly which title this is
    # — surface that as the first option regardless of what /search/multi
    # thinks is most popular for the title text.
    parsed = parse_folder_name(req.folder_path) if req.folder_path else {}
    imdb_id = parsed.get("imdb_id") if parsed else None
    tvdb_id = parsed.get("tvdb_id") if parsed else None
    tmdb_id = parsed.get("tmdb_id") if parsed else None
    # Inferred type from the bracket family.
    #   [tt..]     → IMDb. Almost always movie when paired with [tt] alone
    #                (Sonarr writes [tvdb-] for TV; if only [tt] is present
    #                in a Radarr-managed folder it's a movie).
    #   [tvdb-..]  → TV.
    #   [tmdb-..]  → No type info (could be either); we'll try /movie first.
    inferred_type: str | None = None
    if tvdb_id:
        inferred_type = "tv"
    elif imdb_id or tmdb_id:
        inferred_type = "movie"

    # Fallback: when the folder has no bracket ID at all, derive the type
    # from the containing media-dir's label. Lets users without bracket-
    # ID folder naming still get type-correct manual search filtering.
    # v0.3.82+.
    if not inferred_type and req.folder_path:
        try:
            from backend.media_paths import media_dir_label_for
            label = await media_dir_label_for(req.folder_path)
            inferred_type = _label_to_media_type(label)
        except Exception:
            pass

    import httpx
    pinned: list[dict] = []  # results from bracket-ID lookup, prepended
    pinned_ids: set[tuple[str, int]] = set()  # (media_type, tmdb_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if imdb_id:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/find/{imdb_id}",
                    params={"api_key": tmdb_key, "external_source": "imdb_id"},
                )
                if resp.status_code == 200:
                    body = resp.json()
                    # Prefer movie_results since [ttN] alone is a Radarr signal.
                    for key, mt in (("movie_results", "movie"), ("tv_results", "tv")):
                        for item in (body.get(key) or [])[:1]:
                            entry = _normalise_tmdb_item(item, force_media_type=mt)
                            pinned.append(entry)
                            pinned_ids.add((mt, entry["tmdb_id"]))
            if tvdb_id:
                resp = await client.get(
                    f"https://api.themoviedb.org/3/find/{tvdb_id}",
                    params={"api_key": tmdb_key, "external_source": "tvdb_id"},
                )
                if resp.status_code == 200:
                    body = resp.json()
                    for key, mt in (("tv_results", "tv"), ("movie_results", "movie")):
                        for item in (body.get(key) or [])[:1]:
                            entry = _normalise_tmdb_item(item, force_media_type=mt)
                            if (mt, entry["tmdb_id"]) not in pinned_ids:
                                pinned.append(entry)
                                pinned_ids.add((mt, entry["tmdb_id"]))
            if tmdb_id:
                # No external-source mapping for tmdb_id; fetch the entity
                # directly. Try movie first (most common for [tmdb-]), fall
                # back to tv if that 404s.
                for mt in ("movie", "tv"):
                    try:
                        resp = await client.get(
                            f"https://api.themoviedb.org/3/{mt}/{tmdb_id}",
                            params={"api_key": tmdb_key},
                        )
                        if resp.status_code == 200:
                            entry = _normalise_tmdb_item(resp.json(), force_media_type=mt)
                            if (mt, entry["tmdb_id"]) not in pinned_ids:
                                pinned.append(entry)
                                pinned_ids.add((mt, entry["tmdb_id"]))
                            break
                    except httpx.RequestError:
                        continue
    except httpx.RequestError as exc:
        # Bracket-ID lookup is best-effort; fall through to title search.
        print(f"[POSTER] Bracket-ID lookup failed: {exc}", flush=True)

    # Title search for additional candidates.
    params = {"api_key": tmdb_key, "query": req.query.strip()}
    if req.year:
        params["year"] = req.year

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.themoviedb.org/3/search/multi", params=params)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"TMDB returned {resp.status_code}")
        raw_results = resp.json().get("results", [])
    except httpx.RequestError as exc:
        raise HTTPException(502, f"TMDB request failed: {exc}")

    # Normalise + filter by inferred type (when bracket told us). Skip the
    # ones we already pinned above so the user doesn't see duplicates.
    title_results: list[dict] = []
    for item in raw_results:
        mt = item.get("media_type")
        if mt not in ("movie", "tv"):
            continue
        if inferred_type and mt != inferred_type:
            continue
        entry = _normalise_tmdb_item(item)
        if (mt, entry["tmdb_id"]) in pinned_ids:
            continue
        title_results.append(entry)

    # Re-rank title-search results so the auto-resolver's pick (first
    # year-aware match) lines up with the modal's first card. Tiers:
    #   0 — exact title  + exact year   (best, "See No Evil 2" 2014)
    #   1 — partial title + exact year   ("Odyssey" → "The Odyssey" 2025,
    #                                     "See no evil" → "See No Evil 2" 2014)
    #   2 — exact title  + ±1 year       (metadata drift)
    #   3 — exact title  + any year      (year-blind fallback when none given)
    #   4 — partial title + any year
    #   5 — everything else (TMDB popularity order preserved)
    # v0.3.83+.
    query_lower = req.query.strip().lower()
    target_year_int: int | None = None
    if req.year:
        try:
            target_year_int = int(req.year)
        except ValueError:
            target_year_int = None

    def _rank(entry: dict) -> tuple[int, int]:
        et = (entry.get("title") or "").strip().lower()
        ey = entry.get("year")
        same_title = et == query_lower
        partial_title = bool(et and (query_lower in et or et in query_lower))
        same_year = bool(req.year) and ey == req.year
        near_year = False
        if target_year_int is not None and ey and ey.isdigit():
            try:
                near_year = abs(int(ey) - target_year_int) <= 1 and not same_year
            except ValueError:
                pass
        if same_title and same_year:
            return (0, 0)
        if partial_title and same_year:
            return (1, 0)
        if same_title and near_year:
            return (2, 0)
        if same_title:
            return (3, 0)
        if partial_title:
            return (4, 0)
        return (5, 0)
    title_results.sort(key=_rank)

    # Cap total at 12 to leave room for pinned entries on top of the
    # original 10-result budget.
    out = pinned + title_results
    return {"results": out[:12]}


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

    # Pull the authoritative original language while we have the TMDB
    # detail in hand. Pre-v0.3.81 a manual poster fix only updated
    # `poster_cache` — the `scan_results.native_language` column for
    # files in this folder kept whatever the auto-resolution had
    # written earlier, so audio-cleanup rules (which key off
    # native_language) kept treating the wrongly-matched film's
    # language as canonical. Now writing both at the same time.
    original_lang = (data.get("original_language") or "").strip().lower() or None

    # Upsert into poster_cache + propagate native_language to scan_results.
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
        if original_lang:
            # Match every file under this folder. The folder_path stored
            # in poster_cache is the parent dir without trailing slash;
            # scan_results.file_path includes the full file path. Use
            # LIKE with a `<folder>/%` pattern to catch them all.
            folder_prefix = req.folder_path.rstrip("/") + "/"
            await db.execute(
                "UPDATE scan_results SET native_language = ?, language_source = 'tmdb-manual' "
                "WHERE file_path LIKE ?",
                (original_lang, folder_prefix + "%"),
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
        # Surface the language update so the UI can refresh the file
        # tree's native-language display without a hard reload. v0.3.81+.
        "native_language": original_lang,
    }
