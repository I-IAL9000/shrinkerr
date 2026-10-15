"""Poster metadata resolution, image caching, and Plex image proxy."""

import re
import json
import time
import base64
import asyncio
import difflib
import unicodedata
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import APIRouter, HTTPException
from backend.api_errors import ApiError
from fastapi.responses import Response
from pydantic import BaseModel

from backend.database import DB_PATH
from backend.metadata import resolve_tmdb_key_sync

router = APIRouter(prefix="/api/posters")

CACHE_TTL_HOURS = 168  # 7 days
# A title no match was found for is looked up again after this many days:
# by the Poster grid (Plex first) when it's shown, and by the prefetch.
PLACEHOLDER_TTL_DAYS = 7


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


# A folder named for a season, not the title — the whole name, with its
# number (and maybe its year: "Season 01 (2014)"): its show's name is
# (v0.10.0: in other languages too, and "S01"; as metadata._SEASON_DIR_RE).
# "Season of the Witch", "Special Forces" and "Temporada de patos" are titles.
_SEASON_DIR_RE = re.compile(
    r"(?:(?:season|series|staffel|saison|temporada|stagione|seizoen|sæson|säsong|sesong|kausi)[ ._-]*\d+"
    r"|s\d{1,3}|specials?)(?:\s*[(\[][^)\]]*[)\]])?", re.IGNORECASE)
# A season pack's release name ends with its seasons: "Show.Name.S01.1080p…"
# is "Show Name" ("Model S1 (2020)" isn't a release name: a title).
_SEASON_PACK_RE = re.compile(r"\s+(?:S\d{1,2}(?:\s*-\s*S?\d{1,2})?|Seasons?\s*\d+(?:\s*-\s*\d+)?)$", re.IGNORECASE)


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

    "tv" (v0.10.0): the name says it's a show's — a season folder, a
    season pack ("Show.Name.S01…"), an episode ("…S01E02…").
    """
    parts = folder_path.rstrip("/").split("/")
    # A disc rip's VIDEO_TS / BDMV folder is named after its parent (v0.10.0:
    # TMDB was searched for "VIDEO_TS"), a season folder after its show's.
    if len(parts) > 1 and parts[-1].upper() in ("VIDEO_TS", "BDMV"):
        parts = parts[:-1]
    folder_name = parts[-1] if parts else ""
    tv = False
    if _SEASON_DIR_RE.fullmatch(folder_name) and len(parts) > 1:
        folder_name, tv = parts[-2], True

    imdb_id, tvdb_id, tmdb_id = _extract_ids(folder_name)
    year_match = re.search(r"[\(\[]((?:18|19|20)\d{2})[\)\]]", folder_name)  # "(2019)", "[2019]"; not "[1080]"

    # Strip ID brackets/braces and `(YYYY)` from the title text.
    title = folder_name
    title = re.sub(r"\s*[\[\{(](?:tt\d+|tvdb(?:id)?[-=:]?\d+|tmdb(?:id)?[-=:]?\d+|imdb(?:id)?-\w+)[\]\})]", "", title)
    title = re.sub(r"\s*\b(?:tt\d{7,}|tvdb(?:id)?[-=]\d+|tmdb(?:id)?[-=]\d+)\b", "", title)
    title = re.sub(r"\s*[\(\[](?:18|19|20)\d{2}[\)\]]", "", title)
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
    year = year_match.group(1) if year_match else None
    release = False  # a release name: "….1080p.BluRay.x264-GRP"
    if not (imdb_id or tvdb_id or tmdb_id):
        from backend.media_parser import parse_media_name
        parsed = parse_media_name(folder_name)
        if parsed.title:
            title = parsed.title
        year = year or parsed.year
        tv = tv or parsed.media_type == "tv"
        release = bool(parsed.resolution or parsed.source or parsed.codec)
    pack = _SEASON_PACK_RE.search(title)
    if release and pack and pack.start() > 0:
        title, tv = title[:pack.start()], True

    return {
        "title": title or folder_name,
        "year": year,
        "imdb_id": imdb_id,
        "tvdb_id": tvdb_id,
        "tmdb_id": tmdb_id,
        "tv": tv,
    }


# v0.9.155: Plex image paths are only ever item artwork like
# /library/metadata/123/thumb/1699999999. Anything else is refused before the
# token is attached — "@evil.example/x" made the Plex host URL userinfo and
# sent the token to evil.example, and other paths reached any Plex endpoint.
_PLEX_IMAGE_PATH_RE = re.compile(r"/library/metadata/\d+/[A-Za-z]+(?:/\d+)?")


def plex_image_url(plex_url: str, path: str, plex_token: str) -> str | None:
    """Direct Plex URL for an artwork path, or None if `path` isn't one."""
    if not _PLEX_IMAGE_PATH_RE.fullmatch(path or ""):
        return None
    return f"{plex_url}{path}?X-Plex-Token={plex_token}"


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
            from urllib.parse import quote, unquote
            thumb = unquote(path)
            original = plex_image_url(plex_url, thumb, plex_token)
            if not original:
                return None
            # v0.9.151: a Plex thumb URL returns the original artwork (avg
            # ~0.6 MB, up to 10 MB). Ask Plex's photo transcoder for a
            # poster-sized JPEG like TMDB's w300; the original is only a
            # fallback for servers where the transcoder fails.
            urls = [
                f"{plex_url}/photo/:/transcode?width={PLEX_POSTER_WIDTH}&height={PLEX_POSTER_HEIGHT}"
                f"&minSize=1&upscale=0&url={quote(thumb, safe='')}&X-Plex-Token={plex_token}",
                original,
            ]
        elif url.startswith("http"):
            urls = [url]
        else:
            return None

        async with httpx.AsyncClient(timeout=15) as client:
            for actual_url in urls:
                resp = await client.get(actual_url)
                if resp.status_code == 200 and len(resp.content) > 100:
                    return base64.b64encode(resp.content).decode("ascii")
    except Exception:
        pass
    return None


PLEX_POSTER_WIDTH = 300
PLEX_POSTER_HEIGHT = 450
# base64 size above which a cached Plex poster is an un-resized original.
_OVERSIZED_PLEX_POSTER = 200 * 1024


async def shrink_plex_posters() -> int:
    """Re-fetch cached Plex posters stored at full size (pre-v0.9.151)
    through the transcoder. Downloads happen with no DB connection open;
    each batch is then written in one short transaction. A row is only
    replaced by a smaller image, so a failed transcode leaves it as is.
    One-shot (v0.9.154): posters Plex can't return smaller (e.g. items
    since removed from Plex) were re-fetched and re-logged on every
    startup, so a completed pass is recorded in settings.
    Returns the number of posters shrunk."""
    from backend.plex import _get_plex_settings

    plex_url, plex_token, _ = await _get_plex_settings()
    if not plex_url or not plex_token:
        return 0
    plex_url = plex_url.rstrip("/")

    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (_SHRINK_DONE_KEY,)) as cur:
            if await cur.fetchone():
                return 0
        async with db.execute(
            "SELECT folder_path, poster_url, LENGTH(image_data) FROM poster_cache "
            "WHERE source = 'plex' AND LENGTH(image_data) > ?", (_OVERSIZED_PLEX_POSTER,),
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    if not rows:
        await _mark_shrink_done()
        return 0
    print(f"[POSTERS] Shrinking {len(rows)} full-size Plex posters", flush=True)

    sem = asyncio.Semaphore(4)

    async def _fetch(row):
        async with sem:
            img = await _download_image(row[1] or "", plex_url, plex_token)
        return (row[0], img) if img and len(img) < row[2] else None

    shrunk = saved = 0
    for i in range(0, len(rows), 50):
        batch = rows[i:i + 50]
        done = [r for r in await asyncio.gather(*[_fetch(r) for r in batch]) if r]
        if not done:
            continue
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.executemany(
                "UPDATE poster_cache SET image_data = ? WHERE folder_path = ?",
                [(img, folder) for folder, img in done])
            await db.commit()
        finally:
            await db.close()
        shrunk += len(done)
        old = {r[0]: r[2] for r in batch}
        saved += sum(old[f] - len(img) for f, img in done)
    print(f"[POSTERS] Shrunk {shrunk}/{len(rows)} Plex posters, freed {saved / 1e9:.2f} GB"
          + (" (reclaimed from the database file on the next restart)" if shrunk else "")
          + (f"; {len(rows) - shrunk} not available smaller from Plex, kept as is" if shrunk < len(rows) else ""),
          flush=True)
    await _mark_shrink_done()
    return shrunk


_SHRINK_DONE_KEY = "plex_poster_shrink_done"


async def _mark_shrink_done() -> None:
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, 'true')", (_SHRINK_DONE_KEY,))
        await db.commit()
    finally:
        await db.close()


def _title_extras(network: str | None, status: str | None, credits: str | None) -> dict:
    """A title's network, show status and credits for the Poster grid's
    details (v0.10.0): credits as stored (_credits_of's JSON), parsed."""
    try:
        parsed = json.loads(credits) if credits else None
    except ValueError:
        parsed = None
    return {"network": network or None, "status": status or None,
            "credits": parsed if isinstance(parsed, dict) else None}


def _rating(imdb_id: str | None, tmdb_rating: float | None) -> dict:
    """The title's rating: IMDb's (the dataset) when its IMDb id is known and
    rated, else TMDB's — with which it is, for the label."""
    from backend.imdb_ratings import get_rating
    r = get_rating(imdb_id) if imdb_id else None
    if r:
        return {"rating": r["rating"], "votes": r["votes"], "rating_source": "imdb"}
    return {"rating": tmdb_rating, "votes": None, "rating_source": "tmdb" if tmdb_rating else None}


class ResolveRequest(BaseModel):
    paths: list[str]


_TVDB_CACHE_PATTERN = re.compile(r"\[tvdb-\d+\]")
_V116_PURGE_FLAG = "_v0_3_116_tvdb_cache_purged"


async def _maybe_purge_tvdb_cache_v116(db) -> None:
    """One-shot purge of every `[tvdb-N]` cache row, run on first
    resolve_posters call after the v0.3.116 upgrade. Tracked via a
    settings flag so the purge runs exactly once. v0.3.116+.

    Background: v0.3.111 + v0.3.112 made the resolver type-strict and
    fixed the year-blind TV title search, but rows already in
    poster_cache from prior versions stayed wrong. The narrower
    invalidation in resolve_posters caught some cases (`[tvdb-N]`
    cached as `media_type='movie'`) but missed:
      * placeholder rows that pre-v0.3.112 title-search couldn't resolve
      * TV→TV year mismatches (cached year ≠ folder year)
    Easier to nuke every TVDB row once and let the now-correct resolver
    repopulate from scratch.
    """
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (_V116_PURGE_FLAG,)
        ) as cur:
            if await cur.fetchone():
                return  # already purged on a previous call
        # Pull `source` alongside `folder_path` so we can spare manual
        # overrides (source='tmdb-manual'). Pre-v0.3.127 the migration
        # deleted those too — destroying users' explicit Fix-match
        # picks alongside the broken auto-resolved rows it was meant
        # to clean up.
        async with db.execute("SELECT folder_path, source FROM poster_cache") as cur:
            rows = await cur.fetchall()
        # Filter for `[tvdb-N]` paths in Python (SQLite LIKE bracket
        # matching is fragile) AND skip user-set manual overrides.
        targets = [
            r[0] for r in rows
            if _TVDB_CACHE_PATTERN.search(r[0])
            and (r[1] or "") != "tmdb-manual"
        ]
        if targets:
            CHUNK = 500
            for i in range(0, len(targets), CHUNK):
                batch = targets[i:i + CHUNK]
                ph = ",".join("?" * len(batch))
                await db.execute(
                    f"DELETE FROM poster_cache WHERE folder_path IN ({ph})",
                    batch,
                )
            print(
                f"[POSTER] v0.3.116 migration: purged {len(targets)} "
                f"`[tvdb-N]` cache row(s) for fresh re-resolution",
                flush=True,
            )
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
            (_V116_PURGE_FLAG,),
        )
        await db.commit()
    except Exception as exc:
        # Failed migrations shouldn't block the API. Worst case is the
        # user sees stale data until the next type-impossible check
        # (resolve_posters) or stale-placeholder TTL (v0.3.138) catches it.
        print(f"[POSTER] v0.3.116 migration skipped: {exc}", flush=True)


_KEYS_PURGE_FLAG = "_v0_10_0_misnamed_poster_rows_purged"


async def _maybe_purge_misnamed_rows(db) -> None:
    """One-shot (v0.10.0): rows resolved under the wrong name are deleted, so
    the Poster grid and the prefetch look them up again — a disc rip's,
    kept under its VIDEO_TS / BDMV folder (TMDB was searched for "VIDEO_TS"),
    and a media folder's own (the prefetch searched the library's name,
    "Movies", for the files loose in it; title_key) unless it's named with
    an id (a title). Never a Fix match. Tracked in settings, as
    _maybe_purge_tvdb_cache_v116."""
    from backend.scan_filters import _TITLE_ID_FOLDER
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (_KEYS_PURGE_FLAG,)) as cur:
            if await cur.fetchone():
                return
        async with db.execute("SELECT path FROM media_dirs") as cur:
            roots = {(r[0] or "").rstrip("/") for r in await cur.fetchall()}
        async with db.execute("SELECT folder_path, source FROM poster_cache") as cur:
            rows = await cur.fetchall()
        targets = [r[0] for r in rows if (r[1] or "") != "tmdb-manual" and (
            r[0].rstrip("/").rsplit("/", 1)[-1].upper() in ("VIDEO_TS", "BDMV")
            or (r[0].rstrip("/") in roots and not _TITLE_ID_FOLDER.search(r[0].rstrip("/").rsplit("/", 1)[-1])))]
        for i in range(0, len(targets), 500):
            batch = targets[i:i + 500]
            await db.execute(f"DELETE FROM poster_cache WHERE folder_path IN ({','.join('?' * len(batch))})", batch)
        if targets:
            print(f"[POSTER] {len(targets)} poster row(s) of discs and media folders to look up again", flush=True)
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (_KEYS_PURGE_FLAG,))
        await db.commit()
    except Exception as exc:
        print(f"[POSTER] Purging misnamed poster rows skipped: {exc}", flush=True)


_PLEX_PURGE_FLAG = "_v0_10_0_plex_poster_rows_purged"


async def _maybe_purge_plex_rows(db) -> int:
    """One-shot (v0.10.0): Plex matches are deleted, so the Poster grid and
    the prefetch look them up again — they were made by title alone
    (_resolve_plex took any namesake: a remake, another year's film). A Fix
    match is never a Plex match. Tracked in settings, as
    _maybe_purge_tvdb_cache_v116. Returns the rows deleted."""
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (_PLEX_PURGE_FLAG,)) as cur:
            if await cur.fetchone():
                return 0
        cur = await db.execute("DELETE FROM poster_cache WHERE source = 'plex'")
        deleted = max(cur.rowcount, 0)
        if deleted:
            print(f"[POSTER] {deleted} Plex match(es) to look up again", flush=True)
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (_PLEX_PURGE_FLAG,))
        await db.commit()
        return deleted
    except Exception as exc:
        print(f"[POSTER] Purging Plex matches skipped: {exc}", flush=True)
        return 0


@router.post("/resolve")
async def resolve_posters(req: ResolveRequest):
    """Batch-resolve poster metadata. Returns cached image data as base64 data URIs."""
    if not req.paths:
        return {}

    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = aiosqlite.Row
    try:
        # One-shot migration: invalidate every cached `[tvdb-N]` row so
        # the now-correct resolver (v0.3.111 type-strict + v0.3.112 typed
        # endpoints) re-populates them. Pre-fix rows ranged across:
        #   * TV folders cached as movies (v0.3.111 catches some of these)
        #   * Placeholders that title-search couldn't resolve (v0.3.112's
        #     /search/tv with first_air_date_year now finds them)
        #   * TV→TV year mismatches no other invalidation catches
        # Easier to nuke and re-resolve once than to chase every stale-row
        # pattern. Tracked via a settings flag so it runs exactly once
        # per upgrade. v0.3.116+.
        await _maybe_purge_tvdb_cache_v116(db)
        await _maybe_purge_misnamed_rows(db)
        if await _maybe_purge_plex_rows(db):
            # Their genres, country, rating and IMDb id went with them: the
            # prefetch finds them all again now, not in up to 3 hours.
            await start_prefetch()

        result = {}
        uncached = []

        # Batch query for all requested paths at once.
        # v0.5.24: chunked IN clause — the Poster Grid view loads visible
        # posters in batches, and `req.paths` from a "load all" / scroll
        # can be 1000+ on a big library. Older SQLite builds (variable
        # cap 999) would fail; modern is fine but smaller plans don't
        # hurt.
        CHUNK = 900
        cached_rows: dict = {}
        for i in range(0, len(req.paths), CHUNK):
            chunk = req.paths[i:i + CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            async with db.execute(
                f"SELECT folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type, imdb_id, resolved_at, network, status, credits FROM poster_cache WHERE folder_path IN ({placeholders})",
                chunk,
            ) as cur:
                cached_rows.update({r["folder_path"]: r for r in await cur.fetchall()})

        # Stale-placeholder TTL (v0.3.138+). A `source='placeholder'` row
        # means the resolver couldn't find a TMDB/Plex match the last time
        # it tried. Without re-trying, those rows stay placeholder forever
        # — even after TMDB later adds the title or after a resolver
        # improvement. Pre-v0.3.138 the user had to click a manual
        # "Re-resolve placeholders" button in Settings; now any placeholder
        # row older than 7 days re-resolves automatically on next read,
        # which removes the need for the button entirely. Manual overrides
        # and `other-skipped` rows are excluded from the TTL.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        _stale_cutoff = (_dt.now(_tz.utc) - _td(days=PLACEHOLDER_TTL_DAYS)).isoformat()

        # Media folder labels for every requested path, from one cached
        # index matched by string prefix (v0.10.0, SC-25: this opened a
        # connection and resolved paths on the NAS once per poster). Used
        # by the type-impossible cache invalidation and the "Other" skip.
        from backend.media_paths import _media_dir_label_index, label_in_index
        label_index = await _media_dir_label_index()
        path_label: dict[str, str | None] = {p: label_in_index(p, label_index) for p in req.paths}
        path_label_type: dict[str, str | None] = {p: _label_to_media_type(lbl) for p, lbl in path_label.items()}

        needs_media_type = []  # cached but missing media_type
        for path in req.paths:
            row = cached_rows.get(path)
            if row:
                # Type-impossible combination check (v0.3.111+, expanded
                # v0.3.112+). Cache rows are invalidated when the cached
                # media_type contradicts what the folder/label structure
                # promises:
                #   * `[tvdb-N]` bracket → MUST be tv
                #   * dir labelled "TV Shows" → MUST be tv
                #   * dir labelled "Movies"  → MUST be movie
                # Always-stale rows like "[tvdb-N] folder cached as
                # movie" or "TV-labelled folder cached as movie" get
                # deleted and re-resolved fresh through the now type-
                # strict resolver.
                cached_mt = row["media_type"]
                # User-set manual overrides (source='tmdb-manual') are
                # NEVER auto-invalidated. The user explicitly picked
                # this match — even if it disagrees with the bracket
                # family or dir label, we trust them. Pre-v0.3.127 the
                # type-impossible check wiped manual rows too,
                # producing the "I fixed Rush Hour and it un-fixed
                # itself" symptom.
                cached_source = (row["source"] or "")
                is_manual = cached_source == "tmdb-manual"
                # Stale-placeholder check (v0.3.138+): re-resolve any
                # row that previously failed to match (source='placeholder')
                # if it's older than the TTL. Replaces the old manual
                # "Re-resolve placeholders" Settings button.
                if cached_source == "placeholder" and (row["resolved_at"] or "") < _stale_cutoff:
                    await db.execute(
                        "DELETE FROM poster_cache WHERE folder_path = ?",
                        (path,),
                    )
                    uncached.append(path)
                    continue
                if not is_manual:
                    parsed_check = parse_folder_name(path, walk_files=False)
                    label_mt = path_label_type.get(path)
                    type_impossible = False
                    # `[tvdb-N]` folders MUST be tv. Pre-v0.3.133 we only
                    # caught wrong-type cached values (`movie` for tvdb);
                    # rows with `media_type=NULL` (Plex global fallback
                    # returning a season/episode entry — see
                    # `_resolve_plex`) escaped invalidation and stuck
                    # forever. Now also invalidate the NULL case.
                    if parsed_check.get("tvdb_id") and cached_mt != "tv":
                        type_impossible = True
                    elif cached_mt in ("movie", "tv") and label_mt and cached_mt != label_mt:
                        type_impossible = True
                    if type_impossible:
                        await db.execute(
                            "DELETE FROM poster_cache WHERE folder_path = ?",
                            (path,),
                        )
                        uncached.append(path)
                        continue
                poster = None
                if row["image_data"]:
                    poster = f"data:image/jpeg;base64,{row['image_data']}"
                elif row["poster_url"]:
                    poster = row["poster_url"]
                # Rows cached before the id was stored: the folder name's.
                imdb_id = row["imdb_id"] or parse_folder_name(path, walk_files=False)["imdb_id"]
                result[path] = {
                    "title": row["title"],
                    "year": row["year"],
                    "poster_url": poster,
                    "source": row["source"],
                    "genres": row["genres"],
                    "country": row["country"],
                    "media_type": row["media_type"],
                    "imdb_id": imdb_id or None,
                    **_rating(imdb_id, row["rating"]),
                    **_title_extras(row["network"], row["status"], row["credits"]),
                }
                if not row["media_type"]:
                    needs_media_type.append(path)
            else:
                uncached.append(path)
        # Commit any cache invalidations from the type-mismatch sweep
        # before we proceed to re-resolution.
        if uncached:
            await db.commit()

        # Lazy backfill: cache rows that were resolved during v0.3.121-
        # v0.3.127 stored only the TMDB CDN URL (image_data was
        # skipped). Now that v0.3.128 reverted that, every cache read
        # of such a row triggers a one-time backend download so the
        # next read returns base64 instantly. Bounded concurrency
        # keeps the user-visible latency reasonable on filtered views
        # with many URL-only rows. Source check excludes Plex (already
        # uses its own auth-proxy URL form) and placeholder/other-
        # skipped (no image to fetch). v0.3.128+.
        backfill_paths = [
            p for p, entry in result.items()
            if (entry.get("source") or "") in ("tmdb", "tmdb-manual")
            and entry.get("poster_url")
            and not str(entry["poster_url"]).startswith("data:")
        ]
        if backfill_paths:
            settings_for_bf = {}
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token')"
            ) as cur:
                for srow in await cur.fetchall():
                    settings_for_bf[srow["key"]] = srow["value"]
            plex_url_bf = settings_for_bf.get("plex_url", "").rstrip("/")
            plex_token_bf = settings_for_bf.get("plex_token", "")
            sem_bf = asyncio.Semaphore(8)

            async def _bf_one(p: str):
                async with sem_bf:
                    try:
                        return p, await _download_image(result[p]["poster_url"], plex_url_bf, plex_token_bf)
                    except Exception:
                        return p, None

            # Download everything first, then write in one short transaction
            # (v0.10.0, SC-25: each UPDATE ran between downloads with 15 s
            # timeouts, holding the write lock across the network and
            # blocking conversions' progress writes — "database is locked").
            downloaded = [(p, img) for p, img in await asyncio.gather(*[_bf_one(p) for p in backfill_paths]) if img]
            for p, img in downloaded:
                await db.execute("UPDATE poster_cache SET image_data = ? WHERE folder_path = ?", (img, p))
                result[p]["poster_url"] = f"data:image/jpeg;base64,{img}"
            if downloaded:
                await db.commit()

        if not uncached and not needs_media_type:
            return result

        # Skip TMDB / Plex lookups for paths inside "Other"-typed media dirs
        # (v0.3.33+). Those folders contain non-cataloguable content (home
        # videos, music, lectures, misc rips) where TMDB matches would be
        # spurious — we just write a placeholder result instead.
        other_paths: set[str] = {
            p for p in (uncached + needs_media_type)
            if (path_label.get(p) or "").strip().lower() == "other"
        }
        if other_paths:
            for p in list(other_paths):
                # Mark in cache so we don't re-check on every refresh. The
                # title comes from the folder name — no listing of the folder
                # on the NAS inside the transaction (SC-25).
                parsed = parse_folder_name(p, walk_files=False)
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
                    # Lists the folder for ids: on the NAS, so off the loop.
                    parsed = await asyncio.to_thread(parse_folder_name, path)
                    # Whether the folder has any bracket ID — used by
                    # the dir-label and TVDB-implies-TV fallbacks below.
                    # Pre-v0.3.131 this name was referenced but never
                    # defined, throwing NameError on every resolve_posters
                    # call that needed media_type backfill (i.e. any
                    # folder with media_type still NULL in cache). The
                    # endpoint then returned 500 and the whole batch's
                    # posters fell back to placeholders. v0.3.131+.
                    has_explicit_id = bool(
                        parsed.get("imdb_id")
                        or parsed.get("tvdb_id")
                        or parsed.get("tmdb_id")
                    )
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
        plex = await _plex_for(plex_url, plex_token, plex_path_mapping) if uncached else None
        # Their files' names and durations (the year missing from a folder's
        # name, film or show, a runtime): one query (v0.10.0).
        files_of = await _files_by_title(db, uncached) if uncached else {}

        async def _resolve_one(path: str):
            # Lists the folder for ids: on the NAS, so off the loop (SC-25).
            parsed = await asyncio.to_thread(parse_folder_name, path)
            found = await _resolve_title(path, parsed, tmdb_key, _media_type_hint_for(path), plex,
                                         files=files_of.get(path))
            return path, parsed, found

        # Concurrency 16 — kept from v0.3.121 so the slower (now
        # download-included) per-resolve cost is mitigated by more
        # parallel work. Stays under TMDB's 40 req/10s ceiling.
        sem = asyncio.Semaphore(16)
        async def _bounded(path: str):
            async with sem:
                return await _resolve_one(path)

        resolved = await asyncio.gather(*[_bounded(p) for p in uncached])

        # All network work done — now write all results to cache + result
        # in a single transaction so the per-row INSERT chain isn't N more
        # commits. v0.3.63.
        now_iso = datetime.now(timezone.utc).isoformat()
        for path, parsed, found in resolved:
            poster_url, source, image_data, tmdb_meta = (found["poster_url"], found["source"], found["image_data"],
                                                         found["meta"])
            imdb_id = tmdb_meta.get("imdb_id")
            entry = {
                "title": parsed["title"],
                # The folder name's, else TMDB's (v0.10.0).
                "year": parsed.get("year") or tmdb_meta.get("year") or None,
                "poster_url": f"data:image/jpeg;base64,{image_data}" if image_data else poster_url,
                "source": source,
                "genres": tmdb_meta.get("genres") or None,
                "country": tmdb_meta.get("country") or None,
                "media_type": tmdb_meta.get("media_type"),
                "imdb_id": imdb_id or None,
                **_rating(imdb_id, tmdb_meta.get("rating")),
                **_title_extras(tmdb_meta.get("network"), tmdb_meta.get("status"), tmdb_meta.get("credits")),
            }
            result[path] = entry
            if found["failed"]:
                continue  # shown, not stored: looked up again next time (v0.10.0)

            await db.execute(
                """INSERT OR REPLACE INTO poster_cache
                   (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type,
                    network, status, imdb_id, details_v, art_retry, original_language, credits, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (path, parsed["title"], entry["year"], poster_url, source, image_data,
                 entry["rating"], entry["genres"], entry["country"], tmdb_meta.get("media_type"),
                 tmdb_meta.get("network"), tmdb_meta.get("status"), imdb_id, tmdb_meta.get("details_v", 0),
                 int(found["art_retry"]), tmdb_meta.get("original_language"), tmdb_meta.get("credits") or None, now_iso),
            )

        await db.commit()
        return result
    finally:
        await db.close()


# --- Prefetch endpoint (called after scan or manually) ---

_prefetch_task: asyncio.Task | None = None
# A run asked for while one ran (v0.10.0): its `scheduled`, None for none.
_prefetch_again: bool | None = None


@router.post("/prefetch")
async def prefetch_endpoint():
    """Refresh Posters: the prefetch, for every title without a poster."""
    return await start_prefetch()


async def start_prefetch(scheduled: bool = False) -> dict:
    """Start bulk poster prefetch for all scanned titles. Returns immediately.
    `scheduled`: the library sync's run every few hours (_run_prefetch).
    Asked for while one runs, it runs once more after it (v0.10.0): a scan's
    new titles waited up to 3 hours when the scheduled run was going."""
    global _prefetch_task, _prefetch_again
    if _prefetch_task and not _prefetch_task.done():
        # The run after it skips no more than any of those asking for it would.
        _prefetch_again = scheduled if _prefetch_again is None else (_prefetch_again and scheduled)
        return {"status": "already_running"}
    _prefetch_task = asyncio.create_task(_prefetch_runs(scheduled))
    return {"status": "started"}


async def _prefetch_runs(scheduled: bool) -> None:
    global _prefetch_again
    while True:
        await _run_prefetch(scheduled=scheduled)
        if _prefetch_again is None:
            return
        scheduled, _prefetch_again = _prefetch_again, None


def stop_prefetch() -> None:
    """At shutdown: a running prefetch is cancelled, a run waiting dropped."""
    global _prefetch_again
    _prefetch_again = None
    if _prefetch_task and not _prefetch_task.done():
        _prefetch_task.cancel()


# `POST /re-resolve` (with two modes — "placeholder" and "auto") was
# removed in v0.3.138. The two reasons users hit it have both gone
# automatic:
#   * stale placeholders → invalidated on read after 7d (see TTL block
#     in resolve_posters)
#   * resolver-fix retroactive cleanup → handled by the type-impossible
#     check (v0.3.111+) and one-shot purge migrations (v0.3.116+) per
#     release that needs them
# Manual fixes via the Fix-match modal cover individual mismatches.


@router.get("/prefetch-status")
async def prefetch_status():
    """Get prefetch progress."""
    return _prefetch_progress.copy()


_prefetch_progress = {"status": "idle", "total": 0, "resolved": 0, "cached": 0}


async def _backfill_details(tmdb_key: str, media_type_hint_for) -> int:
    """Matched titles without the details of DETAILS_VERSION — cached before
    their IMDb id, a show's network and status (v0.10.0), their country,
    genres and year (DETAILS_VERSION 2) or all their genres (3) were
    fetched, or matched through Plex: find each on TMDB again — no image
    download — and store its details. A show's season folders are looked
    up once; a Fix match by the
    IMDb id the user picked (else its title), and it keeps that id, not the
    folder's. What TMDB has none of doesn't replace what's stored, and a
    title TMDB doesn't find is marked done: neither is asked again. A lookup
    that fails is tried again next time, and a row changed meanwhile (a Fix
    match, found again) is left as it is now. Returns the rows updated."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA busy_timeout=10000")
        async with db.execute(
            "SELECT folder_path, title, year, source, media_type, imdb_id, resolved_at FROM poster_cache "
            "WHERE source IN ('tmdb', 'tmdb-manual', 'plex') AND COALESCE(details_v, 0) < ? "
            "ORDER BY folder_path", (DETAILS_VERSION,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    if not rows:
        return 0
    print(f"[POSTER] Fetching details (IMDb id, country, genres, network, show status, credits) for {len(rows)} title(s)",
          flush=True)
    # The Scanner's progress bar: this phase's own count, not the posters' finished one (v0.10.0).
    _prefetch_progress.update(phase="details", total=len(rows), resolved=0)
    sem = asyncio.Semaphore(3)
    lookups: dict[tuple, asyncio.Future] = {}  # one per title: a show's season folders share theirs

    async def lookup(title, year, hint, imdb_id, tvdb_id, tmdb_id) -> dict:
        """The title's TMDB match with its details (_find_on_tmdb, as the
        Poster grid finds it); {} when TMDB doesn't find it. Raises when a
        request failed."""
        async with sem:
            await asyncio.sleep(0.35)  # ~3 req/sec, as the prefetch
            _, _, meta = await _find_on_tmdb(tmdb_key, title=title, year=year, media_type=hint,
                                             imdb_id=imdb_id, tvdb_id=tvdb_id, tmdb_id=tmdb_id)
            if not meta.get("tmdb_id"):
                return {}
            if meta.get("details_v") == DETAILS_VERSION:  # fetched for the search's IMDb check
                return meta
            details = await _tmdb_details(meta["tmdb_id"], meta.get("media_type"), tmdb_key)
        if not details:
            raise RuntimeError("TMDB details unavailable")  # rate-limited or down: next time
        return {**meta, **details}

    async def one(row: dict) -> tuple | None:
        path = row["folder_path"]
        parsed = await asyncio.to_thread(parse_folder_name, path)
        manual = row["source"] == "tmdb-manual"
        if manual:  # the user's pick: its IMDb id, else its own title — not the folder's
            key = (True, row["title"], row["year"], row["media_type"], row["imdb_id"] or None, None, None)
        else:
            hint = row["media_type"] or _title_media_type(parsed, {}, media_type_hint_for(path))
            key = (False, parsed.get("title"), parsed.get("year"), hint, parsed.get("imdb_id"), parsed.get("tvdb_id"),
                   parsed.get("tmdb_id"))
        if key not in lookups:
            lookups[key] = asyncio.ensure_future(lookup(*key[1:]))
        try:
            meta = await lookups[key]
        except Exception:
            return None
        imdb_id = (row["imdb_id"] if manual else parsed.get("imdb_id")) or meta.get("imdb_id") or ""
        tv = meta.get("media_type") == "tv"  # a show's network and status; a film has none
        network, status = ((meta.get("network") or ""), (meta.get("status") or "")) if tv else (None, None)
        return (imdb_id, network, network, status, status,
                _rating(imdb_id, None)["rating"], meta.get("rating"), meta.get("country") or "",
                meta.get("genres") or "", meta.get("year") or "", meta.get("original_language") or "",
                meta.get("credits") or "", DETAILS_VERSION, path, row["source"], row["resolved_at"], DETAILS_VERSION)

    done = 0
    for i in range(0, len(rows), 20):
        updates = [r for r in await asyncio.gather(*[one(r) for r in rows[i:i + 20]], return_exceptions=True)
                   if isinstance(r, tuple)]
        if updates:
            db = await aiosqlite.connect(DB_PATH)
            try:
                await db.execute("PRAGMA busy_timeout=30000")
                # Never replaced by none. The IMDb rating replaces TMDB's, which
                # only fills in a missing one; the year in the folder name stays.
                # Only the row as it was read: every write of a new match (a Fix
                # match, found again) sets resolved_at.
                cur = await db.executemany(
                    "UPDATE poster_cache SET imdb_id = COALESCE(NULLIF(?, ''), imdb_id, ''), "
                    "network = COALESCE(NULLIF(?, ''), network, ?), status = COALESCE(NULLIF(?, ''), status, ?), "
                    "rating = COALESCE(?, rating, ?), country = COALESCE(NULLIF(?, ''), country), "
                    "genres = COALESCE(NULLIF(?, ''), genres), year = COALESCE(NULLIF(year, ''), NULLIF(?, '')), "
                    "original_language = COALESCE(NULLIF(?, ''), original_language), "
                    "credits = COALESCE(NULLIF(?, ''), credits), "
                    "details_v = ? WHERE folder_path = ? AND source IS ? AND resolved_at IS ? "
                    "AND COALESCE(details_v, 0) < ?", updates)
                done += cur.rowcount
                await db.commit()
            finally:
                await db.close()
        _prefetch_progress["resolved"] = min(i + 20, len(rows))
        if (i + 20) % 500 < 20 and i + 20 < len(rows):
            print(f"[POSTER] Details: {i + 20}/{len(rows)}", flush=True)
    print(f"[POSTER] Details stored for {done}/{len(rows)} title(s)"
          + (f"; {len(rows) - done} failed (tried again next time) or changed meanwhile" if done < len(rows) else ""),
          flush=True)
    return done


# Plex's own matches (source 'plex', made without a TMDB key) have been
# found on TMDB with one (_run_prefetch).
_PLEX_ROWS_FOUND_FLAG = "_v0_10_0_plex_rows_found_on_tmdb"


async def _run_prefetch(scheduled: bool = False):
    """Prefetch posters for all title-level folders. Uses short DB connections to avoid locking.

    Titles are found as the Poster grid finds them (_resolve_title); those
    found while Plex didn't answer get Plex's poster now if it has one
    (_retry_plex_art: Plex only). Never resolved again: a title with its
    image, the user's Fix match, an Other folder's row or a Plex match; a
    match whose image didn't download only gets its image (v0.10.0). `scheduled`
    (every few hours, v0.10.0) also leaves a match TMDB has no poster for,
    and titles not found: with Plex all of them, else those not found in the
    last week — the Poster grid tries them again after a week, Plex first,
    and finding them here every few hours would put that off forever.
    Refresh Posters and a scan's run try every title not found."""
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
                "SELECT file_path, file_size, duration FROM scan_results WHERE removed_from_list = 0"
            ) as cur:
                all_files = [tuple(r) for r in await cur.fetchall()]

            # Load settings
            settings = {}
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_url', 'plex_token', 'tmdb_api_key', 'plex_path_mapping')"
            ) as cur:
                for row in await cur.fetchall():
                    settings[row["key"]] = row["value"]

            await _maybe_purge_misnamed_rows(db)
            await _maybe_purge_plex_rows(db)
            # Plex's own matches, made without a TMDB key: found on TMDB once
            # one is there (v0.10.0); without one, they will be again.
            plex_rows_found = False
            if resolve_tmdb_key_sync(settings.get("tmdb_api_key")):
                async with db.execute("SELECT 1 FROM settings WHERE key = ?", (_PLEX_ROWS_FOUND_FLAG,)) as cur:
                    plex_rows_found = bool(await cur.fetchone())
            else:
                await db.execute("DELETE FROM settings WHERE key = ?", (_PLEX_ROWS_FOUND_FLAG,))
                await db.commit()
            async with db.execute(
                "SELECT folder_path, source, poster_url, resolved_at, image_data IS NOT NULL AS has_image, "
                "art_retry, title, year, media_type, imdb_id FROM poster_cache"
            ) as cur:
                cached_rows = {r["folder_path"]: r for r in await cur.fetchall()}

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
            async with db.execute("SELECT path FROM media_dirs") as cur:  # as the Scanner tree reads them
                media_roots = frozenset((r["path"] or "").rstrip("/") for r in await cur.fetchall())
        finally:
            await db.close()

        def _media_type_hint_for(path: str) -> str | None:
            for prefix, label in dir_label_pairs:
                if path.startswith(prefix):
                    return _label_to_media_type(label)
            return None

        # Step 2: the titles, keyed as the Poster grid keys them: a file
        # loose in a media folder by itself, never as the media folder.
        from backend.scan_filters import title_key
        files_of_title: dict[str, list] = {}
        for f in all_files:
            tf = title_key(f[0], media_roots)
            if tf:
                files_of_title.setdefault(tf, []).append(f)
        title_folders = set(files_of_title)

        plex_url = settings.get("plex_url", "").rstrip("/")
        plex_token = settings.get("plex_token", "")
        tmdb_key = resolve_tmdb_key_sync(settings.get("tmdb_api_key"))
        path_mapping = settings.get("plex_path_mapping", "")

        folder_list = sorted(title_folders)
        plex_on = bool(plex_url and plex_token)
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=PLACEHOLDER_TTL_DAYS)).isoformat()
        to_resolve: list[str] = []
        images: list[tuple[str, str]] = []  # (folder, poster_url): matched, only the image is missing
        plex_rows: set[str] = set()  # Plex's own matches, found on TMDB now
        for p in folder_list:
            row = cached_rows.get(p)
            if row is None:
                to_resolve.append(p)
            elif row["source"] == "plex" and tmdb_key and not plex_rows_found:
                to_resolve.append(p)
                plex_rows.add(p)
            elif row["has_image"] or row["source"] in ("tmdb-manual", "other-skipped", "plex"):
                continue
            elif row["source"] == "tmdb" and row["poster_url"]:
                images.append((p, row["poster_url"]))
            elif scheduled and (row["source"] == "tmdb" or (row["source"] == "placeholder" and (
                    plex_on or (row["resolved_at"] or "") >= stale_cutoff))):
                continue
            else:
                to_resolve.append(p)
        if not tmdb_key and not plex_on:
            # Nothing can be found here without either: placeholders would
            # only keep the Poster grid from looking again.
            to_resolve = []
        # Titles found while Plex didn't answer: its poster asked for (v0.10.0).
        art_rows = [cached_rows[p] for p in folder_list
                    if p in cached_rows and cached_rows[p]["art_retry"] and cached_rows[p]["source"] == "tmdb"]
        # Plex, for the titles' posters (v0.10.0); it's local, not paced.
        plex = await _plex_for(plex_url, plex_token, path_mapping) if (to_resolve or art_rows) else None
        if not tmdb_key and plex and plex["libraries"] is None:
            to_resolve = []  # Plex's own matches, without a TMDB key: none while it doesn't answer
        _prefetch_progress["total"] = len(folder_list)
        _prefetch_progress["cached"] = len(folder_list) - len(to_resolve) - len(images)
        _prefetch_progress["resolved"] = _prefetch_progress["cached"]

        print(f"[POSTER] {len(folder_list)} titles total, {_prefetch_progress['cached']} cached, "
              f"{len(to_resolve)} to fetch, {len(images)} image(s) to download"
              + ("" if tmdb_key else " (no TMDB key)") + (" (scheduled)" if scheduled else ""), flush=True)

        # Images of matches that didn't download: downloaded with no
        # connection open, each batch written in one short transaction, only
        # where the row is still the one read.
        image_sem = asyncio.Semaphore(4)

        async def _image(folder: str, url: str) -> tuple | None:
            async with image_sem:
                img = await _download_image(url, plex_url, plex_token)
            return (img, folder, url) if img else None
        for i in range(0, len(images), 50):
            got = [r for r in await asyncio.gather(*[_image(f, u) for f, u in images[i:i + 50]]) if r]
            if got:
                db_w = await aiosqlite.connect(DB_PATH)
                try:
                    await db_w.execute("PRAGMA busy_timeout=30000")
                    await db_w.executemany("UPDATE poster_cache SET image_data = ? WHERE folder_path = ? "
                                           "AND image_data IS NULL AND poster_url = ?", got)
                    await db_w.commit()
                finally:
                    await db_w.close()
            _prefetch_progress["resolved"] += len(images[i:i + 50])
        resolved_before = _prefetch_progress["resolved"]

        # Step 3: Sequential resolution with rate limiting + batch DB writes
        # TMDB rate limit: 40 requests/10 seconds. We pace at ~3 req/sec to stay under.
        now_str = datetime.now(timezone.utc).isoformat()
        sem = asyncio.Semaphore(3)
        _INSERT_SQL = """INSERT OR REPLACE INTO poster_cache
            (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type,
             network, status, imdb_id, details_v, art_retry, original_language, credits, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

        async def _do_one(path: str) -> tuple | None:
            async with sem:
                await asyncio.sleep(0.35)  # ~3 req/sec to respect TMDB rate limits
                parsed = await asyncio.to_thread(parse_folder_name, path)  # lists the folder: on the NAS
                found = await _resolve_title(path, parsed, tmdb_key, _media_type_hint_for(path), plex,
                                             files=_title_files(files_of_title.get(path, [])), rate_limit_wait=5)
                if found["failed"]:
                    # Nothing found, but a lookup didn't answer: nothing
                    # written (v0.10.0). A placeholder would replace a match
                    # — its country, genres, IMDb id — for a week.
                    return None
                poster_url, source, image_data, tmdb_meta = (found["poster_url"], found["source"],
                                                             found["image_data"], found["meta"])
                imdb_id = tmdb_meta.get("imdb_id")

                return (path, parsed["title"], parsed.get("year") or tmdb_meta.get("year") or None, poster_url,
                        source, image_data, _rating(imdb_id, tmdb_meta.get("rating"))["rating"],
                        tmdb_meta.get("genres") or None, tmdb_meta.get("country") or None, tmdb_meta.get("media_type"),
                        tmdb_meta.get("network"), tmdb_meta.get("status"), imdb_id, tmdb_meta.get("details_v", 0),
                        int(found["art_retry"]), tmdb_meta.get("original_language"), tmdb_meta.get("credits") or None,
                        now_str)

        # Process in chunks of 20 (rate-limited HTTP, batch DB write)
        plex_rows_left = 0  # Plex's own matches TMDB didn't answer for: found again next time
        for ci in range(0, len(to_resolve), 20):
            chunk = to_resolve[ci:ci + 20]
            results = await asyncio.gather(*[_do_one(p) for p in chunk], return_exceptions=True)
            batch = [r for r in results if isinstance(r, tuple)]
            plex_rows_left += sum(1 for p, r in zip(chunk, results) if p in plex_rows and not isinstance(r, tuple))

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

            _prefetch_progress["resolved"] = resolved_before + ci + len(chunk)
            if (ci + len(chunk)) % 100 < 20:
                print(f"[POSTER] Progress: {ci + len(chunk)}/{len(to_resolve)}", flush=True)

        if art_rows and _plex_answers(plex):
            await _retry_plex_art(art_rows, plex)
        if tmdb_key and not plex_rows_found and not plex_rows_left:
            db_w = await aiosqlite.connect(DB_PATH)
            try:
                await db_w.execute("PRAGMA busy_timeout=30000")
                await db_w.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                                   (_PLEX_ROWS_FOUND_FLAG,))
                await db_w.commit()
            finally:
                await db_w.close()

        if tmdb_key:
            await _backfill_details(tmdb_key, _media_type_hint_for)

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

    image_url = plex_image_url(plex_url, unquote(path), plex_token)
    if not image_url:
        raise HTTPException(status_code=400, detail="Not a Plex artwork path")

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


async def _tmdb_get(client, url: str, params: dict, *, retries: int = 2):
    """GET wrapper that backs off on TMDB rate-limits (429).

    Pre-v0.3.126 the resolver helpers treated `status_code != 200` as
    "no match" and returned a placeholder, including for 429s. The
    frontend then re-queued the same paths on the next render and
    hammered TMDB again — so a transient rate-limit (often hit when
    the bundled `SHRINKERR_TMDB_API_KEY` is shared across many
    deployments) turned into permanent placeholder posters until the
    user manually rotated their TMDB key.

    Now: if we see 429 we honour the `Retry-After` header (capped at
    5 s) or fall back to a 2 s sleep, and retry up to `retries` times.
    Other non-2xx codes still propagate as "no match" — only 429 is
    transient. Returns the final response (which may still be a 429
    after retries exhaust; caller handles).
    """
    import httpx as _httpx
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params)
        except _httpx.RequestError:
            raise
        if resp.status_code != 429 or attempt == retries:
            return resp
        retry_after = 2.0
        try:
            ra = resp.headers.get("retry-after")
            if ra:
                retry_after = max(0.5, min(5.0, float(ra)))
        except (TypeError, ValueError):
            pass
        await asyncio.sleep(retry_after)
    return resp


def _check_tmdb(resp) -> None:
    """A lookup TMDB didn't answer (still rate-limited after _tmdb_get's
    retries, down, a bad key) raises instead of reading as "no match"
    (v0.10.0): the details backfill marks a title TMDB doesn't find as done.
    The resolvers' callers catch it and try the next way, as for no match."""
    if resp.status_code != 200:
        raise RuntimeError(f"TMDB returned {resp.status_code}")


_plex_logged: set[str] = set()  # requests Plex refuses for good, logged once each
_plex_warned_at: float | None = None
PLEX_WARN_EVERY = 3600  # seconds between "Plex didn't answer" lines
# Plex not answering for this many titles in a row isn't asked for the rest
# of the batch (the prefetch: of the run).
_PLEX_FAILURES_STOP = 5


def _plex_json(resp, what: str) -> dict:
    """A Plex answer's JSON. A request it refuses for good — a 4xx but 401 /
    403 (a server without the legacy /search, say) — is no match ({}),
    logged once. Anything else that isn't an answer raises: an error
    status, a refused token, a page that isn't JSON — Plex didn't answer."""
    status = resp.status_code
    if 400 <= status < 500 and status not in (401, 403):
        if f"{what} {status}" not in _plex_logged:
            _plex_logged.add(f"{what} {status}")
            print(f"[POSTER] Plex answers {status} to {what}: no match from it", flush=True)
        return {}
    if status != 200:
        raise RuntimeError(f"Plex returned {status}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError("Plex's answer couldn't be read") from exc


def _plex_unanswered(exc: Exception) -> None:
    """Says Plex didn't answer — at most once in PLEX_WARN_EVERY, not for
    each title."""
    global _plex_warned_at
    now = time.monotonic()
    if _plex_warned_at is None or now - _plex_warned_at >= PLEX_WARN_EVERY:
        _plex_warned_at = now
        print(f"[POSTER] Plex didn't answer ({type(exc).__name__}): TMDB's posters for now, Plex's are asked "
              "for again later", flush=True)


async def _plex_for(plex_url: str, plex_token: str, path_mapping: str) -> dict | None:
    """Plex as _resolve_title asks it for a batch of titles, its libraries
    read once (None: they couldn't be read); None when it isn't set up.
    `failures`: titles in a row it didn't answer for (_plex_answers)."""
    if not (plex_url and plex_token):
        return None
    from backend.plex import get_plex_libraries
    try:
        libraries = await get_plex_libraries(plex_url, plex_token)
    except Exception as exc:
        _plex_unanswered(exc)
        libraries = None
    return {"url": plex_url, "token": plex_token, "mapping": path_mapping, "libraries": libraries, "failures": 0}


def _plex_answers(plex: dict | None) -> bool:
    """Plex is set up and asked: its libraries were read and it hasn't
    failed _PLEX_FAILURES_STOP titles in a row."""
    return bool(plex) and plex["libraries"] is not None and plex["failures"] < _PLEX_FAILURES_STOP


def _plex_failed(plex: dict, exc: Exception) -> None:
    plex["failures"] += 1
    _plex_unanswered(exc)


_EPISODE_NAME_RE = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,3}")
# A film in parts: "Heat.cd1.avi", "Heat - Part 2.mkv", "Disc 1".
_PART_NAME_RE = re.compile(r"(?:^|[ ._\-\[(])(?:cd|dis[ck]|dvd|part|pt)[ ._-]?\d{1,2}(?=[ ._\-\])]|$)", re.IGNORECASE)


def _title_files(rows) -> dict:
    """What a title's files say (v0.10.0), from (file_path, file_size,
    duration) rows: its biggest file's name and its duration — the film,
    not its extras; a film in parts (cd1, cd2…): theirs together, else
    none — and whether they're episodes ("S01E02")."""
    rows = [r for r in rows if r[0]]
    if not rows:
        return {}
    main = max(rows, key=lambda r: r[1] or 0)
    duration = main[2] or None
    parts = [r for r in rows if _PART_NAME_RE.search(r[0].rsplit("/", 1)[-1].rsplit(".", 1)[0])]
    if len(parts) > 1:
        duration = sum(r[2] for r in parts) if all(r[2] for r in parts) else None
    return {"name": main[0], "duration": duration,
            "episodes": any(_EPISODE_NAME_RE.search(r[0].rsplit("/", 1)[-1]) for r in rows)}


async def _files_by_title(db, keys) -> dict[str, dict]:
    """_title_files of each title key: the files in it (a folder) or the
    key itself (a file loose in a media folder)."""
    from backend.database import prefix_clause
    keys = list(dict.fromkeys(keys))
    out: dict[str, dict] = {}
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        exact = set(chunk)
        under_sql, params = prefix_clause([k.rstrip("/") + "/" for k in chunk])
        async with db.execute(
            f"SELECT file_path, file_size, duration FROM scan_results WHERE removed_from_list = 0 "
            f"AND (file_path IN ({','.join('?' * len(chunk))}) OR {under_sql})", [*chunk, *params],
        ) as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        by_key: dict[str, list] = {}
        for row in rows:
            key = row[0] if row[0] in exact else max(
                (k for k in chunk if row[0].startswith(k.rstrip("/") + "/")), key=len, default=None)
            if key:
                by_key.setdefault(key, []).append(row)
        out.update({k: _title_files(v) for k, v in by_key.items()})
    return out


def _title_media_type(parsed: dict, files: dict, label_hint: str | None) -> str | None:
    """Film or show, for the title search (v0.10.0): a [tvdb-N] id, a
    season folder or pack, episode files: a show; else the media folder's
    label; else an IMDb / TMDB id: a film."""
    if parsed.get("tvdb_id") or parsed.get("tv") or files.get("episodes"):
        return "tv"
    return label_hint or _media_type_hint_from_parsed(parsed)


def _file_year(parsed: dict, files: dict) -> str | None:
    """The year in the title's file name ("Heat.1995.1080p.mkv" in "Heat"),
    when it names the same title."""
    if not files.get("name"):
        return None
    from backend.media_parser import parse_media_name
    named = parse_media_name(files["name"].rsplit("/", 1)[-1])
    if named.year and _same_title(_norm_title(named.title), _norm_title(parsed.get("title"))):
        return named.year
    return None


async def _find_on_tmdb(api_key: str, *, title: str | None, year: str | None, media_type: str | None,
                        imdb_id: str | None = None, tvdb_id: str | None = None, tmdb_id: str | None = None,
                        server_ids: dict | None = None, duration: float | None = None, ask=None) -> tuple:
    """The title on TMDB (v0.10.0), the first way that finds it:
      1. the ids in its name — IMDb, TVDB, TMDB;
      2. the ids a media server gave the title in its folder (`server_ids`,
         _plex_ids_here) — TMDB, IMDb, a show's TVDB;
      3. its title (_resolve_tmdb_search: the best ranked match, the
         folder's IMDb id checked, a file's `duration` telling alike
         results apart).
    Returns (poster_url, source, meta). `ask(lookup, *args, **kwargs)` makes
    each request: by default a failed one raises (the details backfill
    tries it again next time); the Poster grid and the prefetch pass one
    that notes it and goes on."""
    async def strict(lookup, *args, **kwargs):
        return await lookup(*args, **kwargs)
    ask = ask or strict
    ways: list[tuple] = []
    asked: set[tuple] = set()  # an id the folder and Plex both give is asked about once

    def way(kind: str, value, lookup, args, kwargs=None) -> None:
        if (kind, str(value).lower()) not in asked:
            asked.add((kind, str(value).lower()))
            ways.append((lookup, args, kwargs or {}))
    if imdb_id:
        way("imdb", imdb_id, _resolve_tmdb, (imdb_id, api_key))
    if tvdb_id:
        way("tvdb", tvdb_id, _resolve_tmdb_tvdb, (tvdb_id, api_key))
    if tmdb_id:
        way("tmdb", tmdb_id, _resolve_tmdb_id, (tmdb_id, media_type, api_key), {"title": title})
    if server_ids:
        server_type = server_ids.get("media_type")
        if server_ids.get("tmdb"):
            way("tmdb", server_ids["tmdb"], _resolve_tmdb_id, (server_ids["tmdb"], server_type, api_key))
        if server_ids.get("imdb"):
            way("imdb", server_ids["imdb"], _resolve_tmdb, (server_ids["imdb"], api_key))
        if server_ids.get("tvdb") and server_type == "tv":
            way("tvdb", server_ids["tvdb"], _resolve_tmdb_tvdb, (server_ids["tvdb"], api_key))
    if title:
        ways.append((_resolve_tmdb_search, (title, year, api_key),
                     {"media_type_hint": media_type, "imdb_id": imdb_id, "duration": duration}))
    for lookup, args, kwargs in ways:
        poster_url, source, meta = await ask(lookup, *args, **kwargs)
        if source != "placeholder":
            return poster_url, source, meta
    return None, "placeholder", {}


async def _resolve_title(folder: str, parsed: dict, tmdb_key: str, label_hint: str | None,
                         plex: dict | None = None, *, files: dict | None = None,
                         rate_limit_wait: float = 0) -> dict:
    """A title's match, found the same way by the Poster grid and the
    prefetch (v0.10.0). TMDB decides which title it is (_find_on_tmdb: the
    ids in its name, the ids Plex gave its files when Plex's title and year
    are the folder's, its title); Plex, when set up, supplies the poster of
    that title (_plex_art: its ids agree, or its files are in the folder),
    else TMDB's is kept. `files` (_title_files) gives the year missing from
    the folder's name, film or show and a runtime to tell alike results
    apart. Without a TMDB key — a source or fork run, the images bring one —
    Plex's own match is the fallback (_resolve_plex, source "plex").

    Returns {poster_url, source, image_data, meta, failed, art_retry}.
    `failed`: not to be stored — nothing was found and a lookup didn't
    answer (a placeholder would hide the title a week). `art_retry`: Plex
    didn't answer, so TMDB's poster for now; the prefetch asks Plex for its
    own later (_retry_plex_art) — the match itself is never Plex's to
    change. A TMDB lookup that doesn't answer is no match and the next way
    is tried, `rate_limit_wait` seconds after a rate limit (the prefetch's
    pacing)."""
    import httpx
    files = files or {}
    found = {"poster_url": None, "source": "placeholder", "image_data": None, "meta": {}, "failed": False,
             "art_retry": False}
    plex_url, plex_token = (plex["url"], plex["token"]) if plex else ("", "")

    if not tmdb_key:
        if not plex:
            return found
        try:
            if plex["libraries"] is None:
                raise RuntimeError("its libraries couldn't be read")
            poster_url, source, meta = await _resolve_plex(
                folder, parsed, plex_url, plex_token, plex["mapping"], libraries=plex["libraries"])
        except Exception as exc:
            _plex_unanswered(exc)
            return {**found, "failed": True}
        if source == "plex":
            found.update(poster_url=poster_url, source=source, meta=meta,
                         image_data=await _download_image(poster_url, plex_url, plex_token))
        return found

    tmdb_down = False

    async def ask(lookup, *args, **kwargs):
        nonlocal tmdb_down
        try:
            return await lookup(*args, **kwargs)
        except Exception as exc:
            tmdb_down = True
            if rate_limit_wait and "429" in str(exc):
                await asyncio.sleep(rate_limit_wait)
            return None, "placeholder", {}

    year = parsed.get("year") or _file_year(parsed, files)
    plex_down = bool(plex) and not _plex_answers(plex)
    client = httpx.AsyncClient(timeout=10) if plex and not plex_down else None
    try:
        lookup = _PlexLookup(client, plex, folder) if client else None
        server_ids = None
        if lookup:
            try:
                server_ids = await _plex_ids_here(lookup, parsed, year)
            except Exception as exc:
                plex_down = True
                _plex_failed(plex, exc)
        poster_url, source, meta = await _find_on_tmdb(
            tmdb_key, title=parsed.get("title"), year=year, media_type=_title_media_type(parsed, files, label_hint),
            imdb_id=parsed.get("imdb_id"), tvdb_id=parsed.get("tvdb_id"), tmdb_id=parsed.get("tmdb_id"),
            server_ids=server_ids, duration=files.get("duration"), ask=ask)
        art = None
        if source == "placeholder":
            if tmdb_down:
                return {**found, "failed": True}
            # TMDB doesn't know it (a home video, a concert): Plex's own
            # match, as without a key — or, Plex not answering, nothing
            # stored (it may have it).
            if plex:
                if lookup and not plex_down:
                    try:
                        poster_url, source, meta = await _plex_own_match(lookup, parsed)
                    except Exception as exc:
                        plex_down = True
                        _plex_failed(plex, exc)
                if plex_down:
                    return {**found, "failed": True}
            if source == "placeholder":
                return found
        else:
            meta = await _with_details(meta, tmdb_key, parsed.get("imdb_id"))
            if lookup and not plex_down:
                try:
                    art = await _plex_art(lookup, {"imdb": meta.get("imdb_id"), "tmdb": meta.get("tmdb_id"),
                                                   "tvdb": meta.get("tvdb_id") or parsed.get("tvdb_id")},
                                          meta.get("media_type"), [parsed.get("title"), meta.get("title")])
                except Exception as exc:
                    plex_down = True
                    _plex_failed(plex, exc)
        if lookup and not plex_down:
            plex["failures"] = 0  # it answered: its failures weren't in a row
    finally:
        if client:
            await client.aclose()
    poster_url = art or poster_url
    found.update(poster_url=poster_url, source=source, meta=meta, art_retry=bool(plex) and plex_down)
    if poster_url:
        # Reverted from v0.3.121. The "skip image fetch for TMDB
        # CDN URLs and let the browser do it" optimisation traded
        # backend response time for browser-side load latency on
        # EVERY page visit — cache rows used to render instantly
        # from base64 stored in poster_cache, but URL-only rows
        # require the browser to fetch from TMDB CDN every time.
        # On filtered Scanner views with hundreds of cards, the
        # browser's per-host connection limit + TMDB CDN
        # variability made posters trickle in or never load. Now
        # always backend-download + base64-cache so cached reads
        # are instant for the user. v0.3.128+.
        found["image_data"] = await _download_image(poster_url, plex_url, plex_token)
    return found


async def _retry_plex_art(rows: list, plex: dict) -> int:
    """Plex's posters for titles found while it didn't answer (art_retry,
    v0.10.0) — Plex is asked, TMDB isn't, and the match stays as it is: a
    title Plex has (its IMDb id agrees, or its files are in the folder) gets
    Plex's poster, one it hasn't keeps TMDB's; either way it isn't asked
    again. A poster that doesn't download is tried again next time. When
    Plex's ids for the files here (_plex_ids_here) are another title's, the
    title is looked up again (its row deleted): it was found by its name
    because Plex didn't answer. Rows changed meanwhile (a Fix match, found
    again) are left as they are. Returns the posters replaced."""
    import httpx
    updates: list[tuple] = []
    again: list[tuple] = []
    async with httpx.AsyncClient(timeout=10) as client:
        for row in rows:
            if not _plex_answers(plex):
                break
            folder = row["folder_path"]
            parsed = parse_folder_name(folder, walk_files=False)
            lookup = _PlexLookup(client, plex, folder)
            try:
                here = await _plex_ids_here(lookup, parsed, row["year"] or parsed.get("year"))
                if here and here.get("imdb") and row["imdb_id"] and here["imdb"] != row["imdb_id"].lower():
                    again.append((folder, row["resolved_at"]))
                    plex["failures"] = 0
                    continue
                poster = await _plex_art(lookup, {"imdb": row["imdb_id"] or parsed.get("imdb_id"),
                                                  "tvdb": parsed.get("tvdb_id")},
                                         row["media_type"], [row["title"] or parsed.get("title")])
                plex["failures"] = 0
            except Exception as exc:
                _plex_failed(plex, exc)
                continue
            image = await _download_image(poster, plex["url"], plex["token"]) if poster else None
            if poster and not image:
                continue
            updates.append((poster, image, folder, row["resolved_at"]))
    if updates or again:
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("PRAGMA busy_timeout=30000")
            await db.executemany(
                "UPDATE poster_cache SET poster_url = COALESCE(?, poster_url), image_data = COALESCE(?, image_data), "
                "art_retry = 0 WHERE folder_path = ? AND art_retry = 1 AND resolved_at IS ?", updates)
            await db.executemany(
                "DELETE FROM poster_cache WHERE folder_path = ? AND art_retry = 1 AND resolved_at IS ?", again)
            await db.commit()
        finally:
            await db.close()
    if again:
        print(f"[POSTER] {len(again)} title(s) Plex has as another title: looked up again", flush=True)
    replaced = sum(1 for u in updates if u[0])
    if updates:
        print(f"[POSTER] Plex's posters for {replaced} of {len(updates)} title(s) found while it didn't answer",
              flush=True)
    return replaced


# A Plex item's ids: its Guid list (asked for with includeGuids=1) and a
# legacy agent's guid ("com.plexapp.agents.imdb://tt0118798?lang=en").
_PLEX_GUID_RE = re.compile(r"(?:com\.plexapp\.agents\.)?(imdb|tmdb|tvdb|themoviedb|thetvdb)://([^/?]+)",
                           re.IGNORECASE)
_PLEX_ID_KINDS = {"imdb": "imdb", "tmdb": "tmdb", "themoviedb": "tmdb", "tvdb": "tvdb", "thetvdb": "tvdb"}


def _plex_ids(item: dict) -> dict[str, str]:
    """{"imdb": "tt0118798", "tmdb": "9622", ...}: the ids Plex has for an item."""
    ids: dict[str, str] = {}
    for guid in [*((g or {}).get("id") for g in item.get("Guid") or []), item.get("guid")]:
        m = _PLEX_GUID_RE.match(guid or "")
        if m:
            ids.setdefault(_PLEX_ID_KINDS[m.group(1).lower()], m.group(2).lower())
    return ids


def _within(path: str, folder: str) -> bool:
    """`path` is `folder` or in it. Compared as NFC: a share can give "ú"
    as one character or as "u" and an accent ("Hrútar")."""
    path, folder = (unicodedata.normalize("NFC", p).rstrip("/") for p in (path, folder))
    return path == folder or path.startswith(folder + "/")


def _plex_ids_agree(item: dict, parsed: dict) -> bool | None:
    """Whether a Plex item's ids are the folder's ([tt…], [tvdb-…],
    [tmdb-…]): all of the kinds both have the same; None when they have no
    kind in common."""
    ids = _plex_ids(item)
    shared = [k for k in ("imdb", "tvdb", "tmdb") if parsed.get(f"{k}_id") and ids.get(k)]
    return all(ids[k] == str(parsed[f"{k}_id"]).lower() for k in shared) if shared else None


# Plex tells namesakes apart by their year: "Butterfly (2025)".
_PLEX_TITLE_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def _plex_item_is_this(item: dict, folder: str, parsed: dict, paths_comparable: bool) -> bool:
    """Whether a Plex search result is the title in `folder` (the path Plex
    sees it at), v0.10.0 — the first with the folder's title was taken, so
    a remake or any namesake in Plex gave its poster. In this order:
      1. Its ids and the folder's ([tt…], [tvdb-…], [tmdb-…];
         _plex_ids_agree), when they have a kind in common: the same, this
         title wherever its files are (another copy, a folder Plex hasn't
         scanned again); another, not this title even in this folder (Plex
         took it for another film).
      2. Where its files are, when Plex says (a film's Media/Part files, a
         show's Location) and `paths_comparable` (the folder is in one of
         Plex's libraries as Plex sees it — else the path mapping is
         missing): in this folder, or a show's season folder in its
         Location. Elsewhere: another title.
      3. Its title (or original title, _norm_title; a year Plex added to
         tell namesakes apart left out) the folder's, and its year the
         folder's (±1) when both have one."""
    agree = _plex_ids_agree(item, parsed)
    if agree is not None:
        return agree
    files = [p["file"] for m in item.get("Media") or [] for p in m.get("Part") or [] if p.get("file")]
    locations = [loc["path"] for loc in item.get("Location") or [] if loc.get("path")]
    if paths_comparable and (files or locations):
        return (any(_within(f, folder) for f in files)
                or any(_within(loc, folder) or _within(folder, loc) for loc in locations))
    norm = _norm_title(parsed.get("title"))
    if not any(_same_title(norm, _norm_title(_PLEX_TITLE_YEAR_RE.sub("", item.get(k) or "")))
               for k in ("title", "originalTitle")):
        return False
    try:
        return not (parsed.get("year") and item.get("year")) or abs(int(parsed["year"]) - int(item["year"])) <= 1
    except (TypeError, ValueError):
        return True


class _PlexLookup:
    """One title's questions to Plex (v0.10.0), over `client`: its searches
    (cached), a show's own record (for its Location, once), and whether an
    item's files are in the title's folder.

    Plex's JSON relied on — as documented, not checked against every server
    version: MediaContainer.Metadata[] items with ratingKey, type ("movie",
    "show", ...), title, originalTitle, year, thumb, Guid[].id
    ("imdb://tt…", with includeGuids=1) or a legacy agent's guid, a film's
    Media[].Part[].file and a show's Location[].path (its own record,
    /library/metadata/{ratingKey}). Requests raise when Plex doesn't answer
    (_plex_json)."""

    def __init__(self, client, plex: dict, folder_path: str):
        from backend.plex import _translate_path
        self.client, self.url = client, plex["url"]
        self.headers = {"X-Plex-Token": plex["token"], "Accept": "application/json"}
        self.libraries = plex["libraries"] or []
        path = _translate_path(folder_path, plex["mapping"]) if plex["mapping"] else folder_path
        # A disc's key is its VIDEO_TS / BDMV folder; the title is its
        # parent's (parse_folder_name), and so are files beside it.
        if path.rstrip("/").rsplit("/", 1)[-1].upper() in ("VIDEO_TS", "BDMV"):
            path = path.rstrip("/").rsplit("/", 1)[0]
        self.folder = path
        # Plex's paths say where an item is only when this folder is in one
        # of its libraries as Plex sees it — else the path mapping is missing.
        self.comparable = any(_within(path, lp) for lib in self.libraries for lp in lib.get("paths", []))
        self._searches: dict[tuple, list] = {}
        self._locations: dict[str, list] = {}

    async def search(self, query: str, scope: str) -> list[tuple[dict, str]]:
        """(item, "movie" | "tv") with a poster that Plex finds for `query`:
        "section" — its searches of the libraries this folder is in;
        "global" — its global search, films and shows only (a season, an
        episode or a collection isn't a title)."""
        if (query, scope) not in self._searches:
            found: list[tuple[dict, str]] = []
            if scope == "section":
                for lib in self.libraries:
                    if not lib.get("id") or not any(_within(self.folder, lp) for lp in lib.get("paths", [])):
                        continue
                    media_type = "movie" if lib.get("type") == "movie" else "tv"
                    resp = await self.client.get(
                        f"{self.url}/library/sections/{lib['id']}/search",
                        params={"type": 1 if media_type == "movie" else 2, "query": query, "includeGuids": 1},
                        headers=self.headers)
                    found += [(item, media_type) for item in
                              _plex_json(resp, "a library search").get("MediaContainer", {}).get("Metadata", [])]
            else:
                resp = await self.client.get(f"{self.url}/search", params={"query": query, "includeGuids": 1},
                                             headers=self.headers)
                for item in _plex_json(resp, "its search").get("MediaContainer", {}).get("Metadata", []):
                    media_type = {"movie": "movie", "show": "tv"}.get(item.get("type"))
                    if media_type:
                        found.append((item, media_type))
            self._searches[(query, scope)] = [(item, mt) for item, mt in found if item.get("thumb")]
        return self._searches[(query, scope)]

    async def located(self, item: dict) -> dict:
        """`item` with its Location: a show's search result has none, its
        record is asked for (once). Only when paths can be compared."""
        key = str(item.get("ratingKey") or "")
        if not self.comparable or item.get("type") != "show" or item.get("Location") or not key.isdigit():
            return item
        if key not in self._locations:
            resp = await self.client.get(f"{self.url}/library/metadata/{key}", params={"includeGuids": 1},
                                         headers=self.headers)
            record = (_plex_json(resp, "a show's record").get("MediaContainer", {}).get("Metadata") or [{}])[0]
            self._locations[key] = record.get("Location") or []
        return {**item, "Location": self._locations[key]}

    def here(self, item: dict) -> bool | None:
        """Whether the item's files are in this folder (a show: its Location,
        or this a season folder in it); None when it can't be told."""
        files = [p["file"] for m in item.get("Media") or [] for p in m.get("Part") or [] if p.get("file")]
        locations = [loc["path"] for loc in item.get("Location") or [] if loc.get("path")]
        if not self.comparable or not (files or locations):
            return None
        return (any(_within(f, self.folder) for f in files)
                or any(_within(loc, self.folder) or _within(self.folder, loc) for loc in locations))

    @staticmethod
    def poster(item: dict) -> str:
        from urllib.parse import quote
        return f"/api/posters/image?path={quote(item['thumb'], safe='')}"


async def _plex_own_match(lookup: "_PlexLookup", parsed: dict) -> tuple:
    """Plex's own match for the folder (v0.10.0) — without a TMDB key, or
    for a title TMDB doesn't know (a home video, a concert): the first item
    its library search (else its global search) finds that is this title
    (_plex_item_is_this), of the type a bracket id says (v0.3.133: its
    global search gave `Charlie's Angels (1976) [tvdb-77170]` the 2000
    film; a [tvdb-N] show in a films library isn't Plex's film). A show
    whose ids can't tell but is otherwise this title is asked for its own
    record (its Location). Raises when Plex doesn't answer: its answer is
    unknown, not "no match"."""
    wanted = _media_type_hint_from_parsed(parsed)
    for scope in ("section", "global"):
        for item, media_type in await lookup.search(parsed["title"], scope):
            if wanted and media_type != wanted:
                continue  # a namesake of the other type isn't taken before it
            if _plex_ids_agree(item, parsed) is None and _plex_item_is_this(item, lookup.folder, parsed, False):
                item = await lookup.located(item)
            if _plex_item_is_this(item, lookup.folder, parsed, lookup.comparable):
                return lookup.poster(item), "plex", {"media_type": media_type}
    return None, "placeholder", {}


async def _resolve_plex(folder_path, parsed, plex_url, plex_token, path_mapping, libraries: list | None = None):
    """_plex_own_match over a connection of its own. `libraries`: Plex's
    (get_plex_libraries), read for a batch of titles; else read here."""
    import httpx
    from backend.plex import get_plex_libraries

    async with httpx.AsyncClient(timeout=10) as client:
        if libraries is None:
            libraries = await get_plex_libraries(plex_url, plex_token)
        return await _plex_own_match(_PlexLookup(client, {"url": plex_url, "token": plex_token,
                                                          "mapping": path_mapping, "libraries": libraries},
                                                 folder_path), parsed)


def _plex_title_fits(item: dict, title: str | None, year: str | None) -> bool:
    """A Plex item's title (or original title; a year it added to tell
    namesakes apart left out) close to `title`, and its year within one of
    `year` when both have one."""
    norm = _norm_title(title)
    names = [_norm_title(_PLEX_TITLE_YEAR_RE.sub("", item.get(k) or "")) for k in ("title", "originalTitle")]
    if not any(_same_title(norm, n) or _close_titles(norm, n) for n in names if n):
        return False
    try:
        return not (year and item.get("year")) or abs(int(year) - int(item["year"])) <= 1
    except (TypeError, ValueError):
        return True


async def _plex_ids_here(lookup: _PlexLookup, parsed: dict, year: str | None) -> dict | None:
    """The ids Plex gave the title in this folder (v0.10.0) — {"imdb",
    "tmdb", "tvdb", "media_type"} — from its library search for the
    folder's title: its item whose files are here, only when that item's
    title is close to the folder's and its year within one (Plex's match
    can be wrong: a new film's file matched to the 1997 one) and its ids
    don't contradict the folder's. None when it has none such."""
    if not lookup.comparable or not parsed.get("title"):
        return None
    for item, media_type in await lookup.search(parsed["title"], "section"):
        ids = _plex_ids(item)
        if not ids or _plex_ids_agree(item, parsed) is False or not _plex_title_fits(item, parsed["title"], year):
            continue
        if lookup.here(await lookup.located(item)):
            return {**ids, "media_type": media_type}
    return None


def _ids_agree(plex_ids: dict, ids: dict) -> bool | None:
    """Whether a Plex item's ids are a title's: all the kinds both have
    the same; None when they have none in common."""
    shared = [k for k in ("imdb", "tmdb", "tvdb") if plex_ids.get(k) and ids.get(k)]
    return all(plex_ids[k] == str(ids[k]).lower() for k in shared) if shared else None


async def _plex_art(lookup: _PlexLookup, ids: dict, media_type: str | None, queries) -> str | None:
    """Plex's poster for the title TMDB found (v0.10.0): the first item of
    its type that Plex's searches for `queries` (the folder's title, TMDB's)
    find whose ids are the title's — or, its ids not telling, whose files
    are in this folder. Never one whose ids are another title's, even in
    this folder (Plex's match was wrong). None: Plex hasn't it."""
    asked: set[str] = set()
    for query in queries:
        if not query or _norm_title(query) in asked:
            continue
        asked.add(_norm_title(query))
        for scope in ("section", "global"):
            for item, item_type in await lookup.search(query, scope):
                if media_type and item_type != media_type:
                    continue
                agree = _ids_agree(_plex_ids(item), ids)
                if agree or (agree is None and lookup.here(await lookup.located(item))):
                    return lookup.poster(item)
    return None


async def _resolve_tmdb_id(tmdb_id, media_type, api_key, title: str | None = None):
    """Resolve by TMDB id (v0.10.0) — the title's record, with its details,
    in one request. Films and shows are numbered apart on TMDB, so an id is
    one of each: without `title` (a media server's id, of a known type)
    only `media_type`'s is asked; with the folder's `title`, its type is
    asked first (else a film's), then the other, and the record whose title
    is the folder's is taken (else the first found)."""
    import httpx
    order = [media_type] if media_type in ("movie", "tv") else []
    if title or not order:
        order += [mt for mt in ("movie", "tv") if mt not in order]
    first = None
    for mt in order:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await _tmdb_get(client, f"https://api.themoviedb.org/3/{mt}/{tmdb_id}",
                                   {"api_key": api_key, "append_to_response": _details_append(mt == "tv")})
        if resp.status_code == 404:
            continue
        _check_tmdb(resp)
        data = resp.json()
        meta = _merge_details(_extract_tmdb_meta(data, mt, api_key), _details_of(data, mt == "tv"))
        poster = data.get("poster_path")
        found = (f"https://image.tmdb.org/t/p/w300{poster}" if poster else None), "tmdb", meta
        if not title or _names_title(data, title):
            return found
        first = first or found
    return first or (None, "placeholder", {})


def _names_title(item: dict, title: str) -> bool:
    """A TMDB record or result named `title` (_norm_title): the same, close,
    or one in the other — its title or original title."""
    norm = _norm_title(title)
    names = {_norm_title(item.get(k)) for k in ("title", "name", "original_title", "original_name")} - {""}
    return bool(norm) and any(_same_title(norm, n) or _close_titles(norm, n) or norm in n or n in norm for n in names)


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
        resp = await _tmdb_get(
            client,
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            {"api_key": api_key, "external_source": "imdb_id"},
        )
        _check_tmdb(resp)
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
    """Resolve by TVDB ID — TV ONLY.

    TVDB IDs identify TV shows. TMDB's /find can occasionally return a
    movie cross-reference for a TVDB ID (metadata-side mapping
    artifact); pre-v0.3.111 we accepted that and produced
    "TV folder → movie poster" mismatches like
    `Matador (2014) [tvdb-281467] → Matador (1986) movie`. Now we
    reject any non-TV result and let the caller fall through to the
    type-constrained title search (which uses the same bracket hint
    to filter to TV).
    """
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await _tmdb_get(
            client,
            f"https://api.themoviedb.org/3/find/{tvdb_id}",
            {"api_key": api_key, "external_source": "tvdb_id"},
        )
        _check_tmdb(resp)
        items = resp.json().get("tv_results", [])
        if items:
            item = items[0]
            meta = _extract_tmdb_meta(item, "tv", api_key)
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
# TMDB's TV genres that are two (v0.10.0): stored as their parts, as the
# films' genres name them, so the Genre list has Action, not also "Action &
# Adventure". scan_filters.row_title splits them in rows stored before.
_COMBINED_GENRES = {"Action & Adventure": ("Action", "Adventure"), "Sci-Fi & Fantasy": ("Sci-Fi", "Fantasy"),
                    "War & Politics": ("War", "Politics")}


def _genre_names(genres: list | None) -> str:
    """A title's genres as stored: all its TMDB genres (v0.10.0: the first
    three before) — ids (search results) or {id, name} (its record) — named
    by _TMDB_GENRES, else as TMDB names them, the combined TV genres split,
    without repeats: "Action, Adventure, Drama"; "" for none."""
    pairs = ((g.get("id"), g.get("name")) if isinstance(g, dict) else (g, None) for g in genres or [])
    named = [n for n in (_TMDB_GENRES.get(gid) or name for gid, name in pairs) if n]
    out: list[str] = []
    for name in named:
        out += [part for part in _COMBINED_GENRES.get(name, (name,)) if part not in out]
    return ", ".join(out)


def _country_names(item: dict) -> str:
    """A title's countries as stored: its first two origin countries (else
    production countries), named by _COUNTRY_NAMES, else as TMDB's
    production_countries name them, else the code: "Great Britain, United
    States"; "" for none."""
    produced = {c.get("iso_3166_1"): c.get("name") for c in item.get("production_countries") or []
                if c.get("iso_3166_1")}
    codes = item.get("origin_country") or list(produced)
    return ", ".join(_COUNTRY_NAMES.get(c) or produced.get(c) or c for c in codes[:2])


def _extract_tmdb_meta(item: dict, media_type: str, api_key: str) -> dict:
    """Extract metadata from a TMDB result item."""
    return {
        "rating": round(item.get("vote_average", 0), 1) if item.get("vote_average") else None,
        "genres": _genre_names(item.get("genre_ids")) or None,
        "country": _country_names(item) or None,
        "media_type": media_type,
        "tmdb_id": item.get("id"),
        "title": item.get("title") or item.get("name"),
        "original_language": item.get("original_language"),
    }


# The details a title's TMDB record gives (_tmdb_details). Bump when they
# change: rows with an older version are fetched again (_backfill_details).
#   1: IMDb id, a TV show's network and status (v0.10.0)
#   2: country, genres and year, for films too
#   3: all of a title's genres, not the first three
#   4: its credits — actors, directors, studios (the Title lists)
DETAILS_VERSION = 4
CREDITS_ACTORS = 10  # the top-billed actors kept


def _details_append(tv: bool) -> str:
    """What a title's TMDB record is asked for with it (_details_of)."""
    return "external_ids,aggregate_credits" if tv else "external_ids,credits"


def _credits_of(data: dict, tv: bool) -> str:
    """A title's credits as poster_cache keeps them: JSON {"actor": the
    CREDITS_ACTORS top-billed, "director": a film's directors or a show's
    creators, "studio": its production companies}; "" when TMDB has none."""
    def names(items, most: int = 1 << 30) -> list[str]:
        out: dict[str, None] = {}  # in order, once each (a long soap's cast: thousands)
        for item in items or []:
            name = (item.get("name") or "").strip() if isinstance(item, dict) else ""
            if name:
                out[name] = None
                if len(out) >= most:
                    break
        return list(out)
    credits = (data.get("aggregate_credits") if tv else data.get("credits")) or {}
    cast = sorted((c for c in credits.get("cast") or [] if isinstance(c, dict)),
                  key=lambda c: c.get("order") if isinstance(c.get("order"), int) else 1 << 30)
    directors = data.get("created_by") if tv else [c for c in credits.get("crew") or []
                                                    if isinstance(c, dict) and c.get("job") == "Director"]
    out = {"actor": names(cast, CREDITS_ACTORS), "director": names(directors),
           "studio": names(data.get("production_companies"))}
    out = {k: v for k, v in out.items() if v}
    return json.dumps(out, ensure_ascii=False) if out else ""


def _details_of(data: dict, tv: bool) -> dict:
    """The details in a title's TMDB record (/movie/{id} or /tv/{id} with
    its external_ids): IMDb id, country, genres, year and a TV show's
    network and status. What TMDB has none of is "". With them, when TMDB
    has them (v0.10.0): a film's runtime (minutes), a show's TVDB id — for
    telling alike results apart and finding the title's Plex poster."""
    date = data.get("first_air_date") if tv else data.get("release_date")
    out = {"imdb_id": data.get("imdb_id") or (data.get("external_ids") or {}).get("imdb_id") or "",
           "country": _country_names(data), "genres": _genre_names(data.get("genres")), "year": (date or "")[:4],
           "credits": _credits_of(data, tv)}
    if tv:
        networks = data.get("networks") or []
        out["network"] = (networks[0].get("name") if networks else None) or ""
        out["status"] = data.get("status") or ""
        if (data.get("external_ids") or {}).get("tvdb_id"):
            out["tvdb_id"] = str(data["external_ids"]["tvdb_id"])
    elif data.get("runtime"):
        out["runtime"] = data["runtime"]
    return out


async def _tmdb_details(tmdb_id, media_type: str, api_key) -> dict:
    """What TMDB's search results leave out, from the title's own record —
    one request (v0.10.0): its IMDb id — the IMDb link and rating for
    folders named without one — a film's country (search results only give
    a show's), genres and year, and a TV show's network and status
    ("Returning Series", "Ended", "Canceled", ...) for the Scanner's filters.
    {} when the request failed; none of them when TMDB has no such title
    (404: a deleted or merged entry), so it isn't asked again."""
    import httpx
    tv = media_type == "tv"
    url = f"https://api.themoviedb.org/3/{'tv' if tv else 'movie'}/{tmdb_id}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await _tmdb_get(client, url, {"api_key": api_key, "append_to_response": _details_append(tv)})
        if resp.status_code == 404:
            return _details_of({}, tv)
        if resp.status_code != 200:
            return {}
        data = resp.json()
    return _details_of(data, tv)


def _merge_details(meta: dict, details: dict) -> dict:
    """`meta` plus the title's details, fetched (_tmdb_details): `details_v`
    is DETAILS_VERSION. A search result's country or genres aren't replaced
    by none."""
    return {**meta, **{k: v for k, v in details.items() if v or not meta.get(k)}, "details_v": DETAILS_VERSION}


async def _with_details(meta: dict, api_key, imdb_id: str | None = None) -> dict:
    """`meta` plus the title's details (_tmdb_details, unless the title
    search's IMDb check fetched them), with `imdb_id` (the folder's), when
    given, as its IMDb id."""
    if meta.get("tmdb_id") and api_key and meta.get("details_v") != DETAILS_VERSION:
        try:
            details = await _tmdb_details(meta["tmdb_id"], meta.get("media_type"), api_key)
        except Exception:
            details = {}
        if details:
            meta = _merge_details(meta, details)
    return {**meta, "imdb_id": imdb_id} if imdb_id else meta


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


# Title matching (v0.10.0): titles are compared as _norm_title gives them.
_TITLE_PUNCT_RE = re.compile(r"[^\w\s]")
# A sequel's number written either way ("II" or "2"), for _close_titles.
_ROMAN = {r: str(n) for n, r in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), 1)}
# How alike two titles of the same year (±1) must be for the last pass of
# rank_tmdb_matches (difflib's ratio, 0-1). 0.8 takes "Colour Out of Space"
# for "Color Out of Space" (0.97), "The Godfather 2" for "The Godfather Part
# II" (0.86). A title within another is pass 2's, before. One letter is
# much of a short title: under _SHORT_TITLE characters 0.9 — "Colour" is
# "Color" (0.91), but "Lucky" isn't "Lucy" (0.89), "The Ring" isn't "The
# King" (0.88), "Glass" isn't "Class" (0.80).
_CLOSE_TITLE_RATIO = 0.8
_SHORT_TITLE_RATIO = 0.9
_SHORT_TITLE = 10


def _norm_title(text: str | None) -> str:
    """A title as matched: casefolded, without accents ("Amélie" is
    "amelie") or punctuation (": - – ' ’ . , ! ?" and the like: "Mission:
    Impossible" is "mission impossible"), "&" as "and", single spaces."""
    # NFKD splits an accent off its letter ("é" is "e" + U+0301); the accent,
    # not a word character, goes with the punctuation.
    text = unicodedata.normalize("NFKD", text or "").casefold()
    text = _TITLE_PUNCT_RE.sub("", text.replace("&", " and ").replace("_", " "))
    return " ".join(text.split())


def _same_title(a: str, b: str) -> bool:
    """Normalised titles that are the same, spaces aside ("Spider-Man" is
    "Spider Man")."""
    return bool(a and b) and (a == b or a.replace(" ", "") == b.replace(" ", ""))


def _close_titles(a: str, b: str) -> bool:
    """Normalised titles close enough to be one, for a result of the
    folder's year: alike by _CLOSE_TITLE_RATIO ("Harry Potter Philosophers
    Stone" and "Harry Potter and the Philosopher's Stone": 0.89; short ones
    by _SHORT_TITLE_RATIO), or the same but for digits written for letters
    ("Se7en" is "Seven", "M3GAN" "Megan") — never with other numbers: "Saw
    II" isn't "Saw III", "Frozen" isn't "Frozen II" ("II" and "2" are one)."""
    wa, wb = [_ROMAN.get(w, w) for w in a.split()], [_ROMAN.get(w, w) for w in b.split()]
    if not wa or not wb or {w for w in wa if w.isdigit()} != {w for w in wb if w.isdigit()}:
        return False
    a, b = " ".join(wa), " ".join(wb)
    if len(a) == len(b) and all(x == y or (x.isdigit() and y.isalpha()) or (x.isalpha() and y.isdigit())
                                for x, y in zip(a, b)):
        return True
    ratio = _SHORT_TITLE_RATIO if max(len(a), len(b)) < _SHORT_TITLE else _CLOSE_TITLE_RATIO
    return difflib.SequenceMatcher(None, a, b).ratio() >= ratio


def rank_tmdb_matches(results: list[dict], title: str, year: str | None) -> list[dict]:
    """The TMDB search results that match `title` (and `year`), best first
    (rank_tmdb_tiers). [] for none."""
    return [item for tier in rank_tmdb_tiers(results, title, year) for item in tier]


def rank_tmdb_tiers(results: list[dict], title: str, year: str | None) -> list[list[dict]]:
    """The TMDB search results that match `title` (and `year`), by the pass
    below that took them (best first; a result once), each in TMDB's order
    (its relevance, as Fix match lists them). [] for none.

    Titles are compared normalised (_norm_title), with a result's title /
    name and its original title / name ("Druk" is "Another Round").

    Pass ordering (v0.3.83+):

    When the user provided a year, run year-AWARE passes only.
    Pre-v0.3.83 the order was:
      1) exact title + year  →  2) exact title any year  →
      3) partial title + year
    which silently mis-resolved e.g. "See no evil (2014)" to the
    2006 movie of that title (the 2014 sequel is titled "See No
    Evil 2", so step 1 missed; step 2's any-year exact match
    picked the 2006 entry). Year-aware partial matching now
    runs BEFORE year-blind exact matching, and we no longer fall
    back to year-blind passes at all when a year was given —
    better to return no match than the wrong year.

    When no year was given, pure title-based passes apply
    (preserves the long-standing behaviour for users without
    year-tagged folders).
    """
    norm = _norm_title(title)
    items = []
    for item in results:
        names = {_norm_title(item.get(k)) for k in ("title", "name", "original_title", "original_name")} - {""}
        items.append((item, names, str(item.get("release_date") or item.get("first_air_date") or "")[:4]))
    tiers: list[list[dict]] = []

    def add(found) -> None:
        tier = [item for item in found if all(item is not r for t in tiers for r in t)]
        if tier:
            tiers.append(tier)

    def same(names) -> bool:
        return any(_same_title(norm, n) for n in names)

    def partial(names) -> bool:
        return bool(norm) and any(norm in n or n in norm for n in names)

    if year:
        # 1. Exact title + exact year
        add(item for item, names, iy in items if same(names) and iy == year)
        # 2. Partial title + exact year — catches "See no evil" → "See No
        #    Evil 2" (2014) and "Odyssey" → "The Odyssey" (2025).
        add(item for item, names, iy in items if partial(names) and iy == year)
        try:
            target = int(year)
        except ValueError:
            return tiers

        def near(iy: str) -> bool:
            return iy.isdigit() and abs(int(iy) - target) <= 1
        # 3. Exact title + ±1 year — covers the "TMDB has 2014, folder
        #    has 2013" metadata-drift case without admitting the
        #    "off by 8 years" wrong-movie cases that pre-v0.3.83
        #    Pass 2 was admitting.
        add(item for item, names, iy in items if same(names) and near(iy))
        # 4. A close title + the year (±1) — what Fix match would show
        #    first: "The Hobbit Battle of the Five Armies" (2014) for "The
        #    Hobbit: The Battle of the Five Armies", "Seven" for "Se7en".
        add(item for item, names, iy in items if near(iy) and any(_close_titles(norm, n) for n in names))
        # No year-aware match — no match rather than silently picking a
        # wrong-year title-match.
        return tiers

    # No-year path: original behaviour kept.
    # A. Exact title (any year)
    add(item for item, names, _ in items if same(names))
    # B. Partial title (any year)
    add(item for item, names, _ in items if partial(names))
    return tiers


def pick_tmdb_match(results: list[dict], title: str, year: str | None) -> dict | None:
    """The TMDB search result that matches `title` (and `year`) best
    (rank_tmdb_matches), or None.

    Shared by poster matching and the original-language lookup (v0.10.0), so
    a title gets its language from the same TMDB entry as its poster."""
    ranked = rank_tmdb_matches(results, title, year)
    return ranked[0] if ranked else None


# A title search checks this many of its matches' IMDb ids, or runtimes, at
# most: one details request each (pick_tmdb_candidate).
_IMDB_CHECKED_MATCHES = 3
# A file's runtime is a film's when within 15% of it, or 10 minutes: cuts,
# PAL speed-up (4%), credits; a remake differs more.
_RUNTIME_SLACK = 0.15
_RUNTIME_SLACK_SECONDS = 600


def _poster_log(message: str) -> None:
    print(f"[POSTER] {message}", flush=True)


async def _candidate_details(item: dict, media_type: str, api_key: str) -> dict:
    """A search result's details (_tmdb_details); raises RuntimeError when
    TMDB didn't give them (rate-limited, down)."""
    try:
        details = await _tmdb_details(item.get("id"), media_type, api_key)
    except Exception as exc:
        raise RuntimeError(f"TMDB gave no details for {media_type} {item.get('id')}") from exc
    if not details:
        raise RuntimeError(f"TMDB gave no details for {media_type} {item.get('id')}")
    return details


async def pick_imdb_checked(ranked: list[dict], imdb_id: str | None, api_key: str,
                            media_type: str = "movie", log=_poster_log,
                            sole: dict | None = None) -> tuple[dict | None, dict]:
    """The first of a title search's matches (`ranked`, rank_tmdb_matches)
    that is the title with `imdb_id` — the folder's, when TMDB's /find
    doesn't know it (common for new titles), v0.10.0: up to
    _IMDB_CHECKED_MATCHES are checked, one details request each
    (_tmdb_details), and one whose IMDb id on TMDB is another title's is
    skipped. Returns (match, its details), or (None, {}). Without `imdb_id`:
    the first, unchecked, with no details. Raises RuntimeError when TMDB
    didn't give a match's details (rate-limited, down): unchecked, it isn't
    taken — it's looked up again later. `log`: the caller's, for a skipped
    match.

    `sole`: the only result with the folder's title and year, if one. When
    every match checked has another IMDb id, it's still taken: TMDB didn't
    know the folder's id (its /find came back empty), so the two ids are
    likely one title's — IMDb merges duplicates, and one side keeps the old
    id ("Summer of Soul (2021)"). A namesake among several results of a
    title and year ("Buddy", 2026) is still refused."""
    if not imdb_id:
        return (ranked[0] if ranked else None), {}
    skipped: dict[int, dict] = {}
    for item in ranked[:_IMDB_CHECKED_MATCHES]:
        mt = item.get("media_type") or media_type
        details = await _candidate_details(item, mt, api_key)
        found = details.get("imdb_id") or ""
        if found and found.lower() != imdb_id.lower():
            skipped[id(item)] = details
            continue
        return item, details
    if sole is not None and id(sole) in skipped:
        log(f"TMDB {sole.get('media_type') or media_type} {sole.get('id')} has another IMDb id than "
            f"{imdb_id}, but is the only one of its title and year: taken")
        return sole, skipped[id(sole)]
    for item in ranked[:_IMDB_CHECKED_MATCHES]:
        if id(item) in skipped:
            log(f"TMDB {item.get('media_type') or media_type} {item.get('id')} is "
                f"{skipped[id(item)].get('imdb_id')}, not {imdb_id}: skipped")
    return None, {}


def _runtime_fits(runtime_minutes, duration: float) -> bool:
    return bool(runtime_minutes) and abs(runtime_minutes * 60 - duration) <= max(
        _RUNTIME_SLACK_SECONDS, _RUNTIME_SLACK * runtime_minutes * 60)


async def pick_tmdb_candidate(results: list[dict], title: str, year: str | None, api_key: str, *,
                              media_type: str | None = None, imdb_id: str | None = None,
                              duration: float | None = None, log=_poster_log) -> tuple[dict | None, dict]:
    """The title among a TMDB search's results (v0.10.0), with its details
    when they were fetched to choose it ({} else): the best ranked
    (rank_tmdb_tiers). With the folder's IMDb id, the first that isn't
    another title (pick_imdb_checked). With alike results in the best pass
    — a remake without a year in the name, two films of a title and year —
    and the file's `duration` (seconds) known: the best match, unless its
    runtime on TMDB is known and isn't the file's (_runtime_fits) and
    another film of them (up to _IMDB_CHECKED_MATCHES in all) is — an
    unknown runtime (new, obscure entries) isn't evidence against it. A
    clear match costs no request.
    Raises RuntimeError when TMDB didn't give a candidate's details. Shared
    by poster matching and the original-language lookup, so both take the
    same title."""
    tiers = rank_tmdb_tiers(results, title, year)
    if not tiers:
        return None, {}
    if imdb_id:
        norm = _norm_title(title)
        same = [i for i in results if year and str(i.get("release_date") or i.get("first_air_date") or "")[:4] == year
                and any(_same_title(_norm_title(i.get(k)), norm)
                        for k in ("title", "name", "original_title", "original_name"))]
        return await pick_imdb_checked([i for t in tiers for i in t], imdb_id, api_key, media_type or "movie", log,
                                       sole=same[0] if len(same) == 1 else None)
    best = tiers[0]

    def film(item: dict) -> bool:
        return (item.get("media_type") or media_type or "movie") == "movie"
    if not duration or len(best) < 2 or not film(best[0]):
        return best[0], {}
    details = await _candidate_details(best[0], "movie", api_key)
    if not details.get("runtime") or _runtime_fits(details["runtime"], duration):
        return best[0], details
    for item in best[1:_IMDB_CHECKED_MATCHES]:
        if film(item):
            other = await _candidate_details(item, "movie", api_key)
            if _runtime_fits(other.get("runtime"), duration):
                return item, other
    return best[0], details


async def _resolve_tmdb_search(title, year, api_key, media_type_hint: str | None = None,
                               imdb_id: str | None = None, duration: float | None = None):
    """Search by title+year — requires strict matching to avoid mismatches.

    `imdb_id` (v0.10.0): the folder's, for a title its IMDb id didn't find
    (TMDB's /find lacks the link for many new titles): the match is the
    first whose IMDb id isn't another title's (pick_imdb_checked; raises
    when it couldn't be checked). The details fetched for the check come
    with the match (`details_v`), so _with_details doesn't ask again.
    `duration`: the file's, in seconds, telling alike results apart
    (pick_tmdb_candidate).

    `media_type_hint` (v0.3.82+): when set to "movie" or "tv", uses the
    type-specific TMDB endpoint (/search/tv or /search/movie) instead
    of /search/multi. v0.3.112+ change: /search/multi's `year` param
    only filters movies — it leaves TV shows year-blind, which broke
    "Nashville (2012) [tvdb-259055]" and similar cases where TMDB had
    the right TV show but our post-filter never received a year-
    constrained TV result. The typed endpoints honour
    `first_air_date_year` (TV) and `year` (movie) respectively, so
    the year filter actually applies to the type we care about.
    """
    import httpx
    if len(title) < 3:
        return None, "placeholder", {}

    async with httpx.AsyncClient(timeout=10) as client:
        if media_type_hint == "tv":
            endpoint = "https://api.themoviedb.org/3/search/tv"
            params = {"api_key": api_key, "query": title}
            if year:
                params["first_air_date_year"] = year
        elif media_type_hint == "movie":
            endpoint = "https://api.themoviedb.org/3/search/movie"
            params = {"api_key": api_key, "query": title}
            if year:
                params["year"] = year
        else:
            endpoint = "https://api.themoviedb.org/3/search/multi"
            params = {"api_key": api_key, "query": title}
            if year:
                params["year"] = year
        resp = await _tmdb_get(client, endpoint, params)
        _check_tmdb(resp)
        results = resp.json().get("results", [])
        # Typed endpoints don't include media_type on each item;
        # stamp it so the downstream _extract_tmdb_meta sees the right
        # value. /search/multi returns mixed types — no post-filter here
        # since hint is None. Caller wanted "anything".
        if media_type_hint in ("movie", "tv"):
            for r in results:
                r["media_type"] = media_type_hint
        # /search/multi finds people too: never a title (v0.10.0).
        results = [r for r in results if r.get("media_type", "movie") in ("movie", "tv")]
    item, details = await pick_tmdb_candidate(results, title, year, api_key, media_type=media_type_hint,
                                              imdb_id=imdb_id, duration=duration)
    if item is None:
        return None, "placeholder", {}
    mt = item.get("media_type", "movie")
    meta = _extract_tmdb_meta(item, mt, api_key)
    if details:
        meta = _merge_details(meta, details)
    # Poster is optional — a title with no TMDB poster is still a
    # better match than falling through to a wrong-year/wrong-show
    # fuzzy guess. Render placeholder, keep the meta. v0.3.56+.
    poster = item.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w300{poster}" if poster else None
    return poster_url, "tmdb", meta


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
        raise ApiError(400, "TMDB API key not configured", code="posters.tmdbKeyMissing")

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

    # Title search for additional candidates. Use the typed TMDB
    # endpoint when we have an inferred type, since /search/multi's
    # `year` parameter only filters MOVIES — TV results come back
    # year-blind, which then drop out when we post-filter to TV. The
    # typed /search/tv endpoint accepts `first_air_date_year` and
    # actually applies it. Same root-cause fix as v0.3.112's resolver
    # change. Without an inferred type, fall back to /search/multi.
    if inferred_type == "tv":
        endpoint = "https://api.themoviedb.org/3/search/tv"
        params = {"api_key": tmdb_key, "query": req.query.strip()}
        if req.year:
            params["first_air_date_year"] = req.year
    elif inferred_type == "movie":
        endpoint = "https://api.themoviedb.org/3/search/movie"
        params = {"api_key": tmdb_key, "query": req.query.strip()}
        if req.year:
            params["year"] = req.year
    else:
        endpoint = "https://api.themoviedb.org/3/search/multi"
        params = {"api_key": tmdb_key, "query": req.query.strip()}
        if req.year:
            params["year"] = req.year

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(endpoint, params=params)
        if resp.status_code != 200:
            raise ApiError(resp.status_code, f"TMDB returned {resp.status_code}", code="posters.tmdbError", params={"status": resp.status_code})
        raw_results = resp.json().get("results", [])
    except httpx.RequestError as exc:
        raise ApiError(502, f"TMDB request failed: {exc}", code="posters.tmdbRequestFailed", params={"error": str(exc)})

    # Normalise + filter by inferred type (when bracket told us). Skip
    # the ones we already pinned above so the user doesn't see
    # duplicates. Typed-endpoint items don't carry media_type, so we
    # stamp it from `inferred_type`; /search/multi items already have
    # it.
    title_results: list[dict] = []
    for item in raw_results:
        mt = item.get("media_type") or inferred_type
        if mt not in ("movie", "tv"):
            continue
        if inferred_type and mt != inferred_type:
            continue
        entry = _normalise_tmdb_item(item, force_media_type=mt)
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
        raise ApiError(400, "media_type must be 'movie' or 'tv'", code="posters.invalidMediaType")

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'tmdb_api_key'") as cur:
            row = await cur.fetchone()
            tmdb_key = row["value"] if row else ""
    finally:
        await db.close()

    if not tmdb_key:
        raise ApiError(400, "TMDB API key not configured", code="posters.tmdbKeyMissing")

    # Fetch the specific TMDB item details
    import httpx
    detail_url = f"https://api.themoviedb.org/3/{req.media_type}/{req.tmdb_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(detail_url, params={"api_key": tmdb_key,
                                                        "append_to_response": _details_append(req.media_type == "tv")})
        if resp.status_code != 200:
            raise ApiError(resp.status_code, f"TMDB returned {resp.status_code}", code="posters.tmdbError", params={"status": resp.status_code})
        data = resp.json()
    except httpx.RequestError as exc:
        raise ApiError(502, f"TMDB request failed: {exc}", code="posters.tmdbRequestFailed", params={"error": str(exc)})

    title = data.get("title") or data.get("name") or "Unknown"
    release_date = data.get("release_date") or data.get("first_air_date") or ""
    year = release_date[:4] if release_date else None
    poster_path = data.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w300{poster_path}" if poster_path else None

    # v0.10.0: its details — IMDb id (the IMDb link and rating), country,
    # genres and a show's network and status — as the automatic match stores
    # them (the record is the one _tmdb_details reads).
    details = _details_of(data, req.media_type == "tv")
    genre_names, country_names, imdb_id = details["genres"], details["country"], details["imdb_id"]
    rated = _rating(imdb_id, round(data.get("vote_average", 0), 1) if data.get("vote_average") else None)
    rating = rated["rating"]
    network, status = details.get("network"), details.get("status")

    # Backend-download + base64-cache so subsequent reads of this
    # folder render instantly from poster_cache without a browser
    # round-trip. (v0.3.121's "skip TMDB CDN fetch" was reverted in
    # v0.3.128 — see _resolve_one.)
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
    # v0.9.96: map TMDB's 2-letter code (es/de) to the 3-letter ISO 639-2/B
    # (spa/ger) the rest of the app uses — the auto/api path already does this
    # via map_language_code; without it, manual matches stored a 2-letter
    # native that broke keep-language matching and displayed wrong.
    from backend.metadata import map_language_code
    _raw_lang = (data.get("original_language") or "").strip().lower() or None
    original_lang = map_language_code(_raw_lang) if _raw_lang else None

    # Upsert into poster_cache + propagate native_language to scan_results.
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            """INSERT OR REPLACE INTO poster_cache
               (folder_path, title, year, poster_url, source, image_data, rating, genres, country, media_type,
                network, status, imdb_id, details_v, original_language, credits, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.folder_path, title, year, poster_url, "tmdb-manual", image_data,
             rating, genre_names or None, country_names or None, req.media_type,
             network, status, imdb_id, DETAILS_VERSION, _raw_lang, details["credits"] or None,
             datetime.now(timezone.utc).isoformat()),
        )
        # v0.9.95: a manual match is authoritative — ALWAYS mark the row(s)
        # 'tmdb-manual', even if TMDB has no original_language for the pick
        # (keep the existing native then). Match both files UNDER the folder
        # (`<folder>/%`) AND the exact path itself, since the poster grid
        # passes a loose single file's own path as `folder_path` (its
        # `<file>/%` prefix would otherwise match nothing → update skipped →
        # the item stayed 'heuristic' after matching).
        folder = req.folder_path.rstrip("/")
        from backend.database import prefix_clause
        under_sql, under_params = prefix_clause([folder + "/"])
        await db.execute(
            "UPDATE scan_results SET "
            "native_language = COALESCE(?, native_language), "
            "language_source = 'tmdb-manual' "
            f"WHERE file_path = ? OR {under_sql}",
            (original_lang, folder, *under_params),
        )
        await db.commit()
        # SC-05 (v0.10.0): re-sort the folder's tracks against the matched
        # native — changing only the label left them sorted against the
        # wrong guess, with the real original-language audio marked removable.
        resort = []
        if original_lang:
            from backend.routes.scan import _reclass_item
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, audio_tracks_json, subtitle_tracks_json, duration FROM scan_results "
                f"WHERE file_path = ? OR {under_sql}", (folder, *under_params),
            ) as cur:
                resort = [it for it in (_reclass_item(r, original_lang) for r in await cur.fetchall()) if it]
    finally:
        await db.close()
    if resort:
        from backend.routes.scan import _write_lang_batch
        await _write_lang_batch(resort)

    return {
        "title": title,
        "year": year,
        "poster_url": f"data:image/jpeg;base64,{image_data}" if image_data else poster_url,
        "media_type": req.media_type,
        **rated,
        "imdb_id": imdb_id or None,
        "genres": genre_names or None,
        "country": country_names or None,
        "source": "tmdb-manual",
        # Surface the language update so the UI can refresh the file
        # tree's native-language display without a hard reload. v0.3.81+.
        "native_language": original_lang,
    }
