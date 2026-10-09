"""TMDB / TVDB API integration for original-language detection."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx

from backend.database import DB_PATH


# ---------------------------------------------------------------------------
# TMDB API key resolution — central helper used by every TMDB call site.
#
# Precedence:
#   1. User-saved `tmdb_api_key` setting (wins)
#   2. `SHRINKERR_TMDB_API_KEY` environment variable — intended for image
#      maintainers to bake a non-commercial key in at build time so fresh
#      installs get poster/metadata lookups without the user having to
#      register with TMDB first (Sonarr/Radarr pattern).
#   3. Empty string — TMDB calls are skipped.
#
# To ship a bundled key, set ENV SHRINKERR_TMDB_API_KEY=<key> in your
# Dockerfile or docker-compose. TMDB non-commercial keys are free and
# issued per-user at <https://www.themoviedb.org/settings/api>. Attribution
# ("This product uses the TMDB API but is not endorsed or certified by
# TMDB") is required.
# ---------------------------------------------------------------------------

def _env_tmdb_key() -> str:
    return (os.environ.get("SHRINKERR_TMDB_API_KEY") or "").strip()


async def resolve_tmdb_key(db: aiosqlite.Connection) -> str:
    """Return the effective TMDB API key: user setting > env fallback > ''."""
    async with db.execute(
        "SELECT value FROM settings WHERE key = 'tmdb_api_key'"
    ) as cur:
        row = await cur.fetchone()
    user_key = (row["value"] if row else "") or ""
    return user_key or _env_tmdb_key()


def resolve_tmdb_key_sync(user_key: str | None) -> str:
    """Non-async helper for call sites that already have the user key in hand."""
    return (user_key or "").strip() or _env_tmdb_key()

# ---------------------------------------------------------------------------
# a) ID parsing
# ---------------------------------------------------------------------------

def parse_media_id(file_path: str) -> tuple[str, str] | None:
    """Walk the filename + up to 4 parent directories for an IMDb / TVDB /
    TMDB id, in any of the formats the poster system recognises.

    v0.9.89: reuses posters._extract_ids so language resolution stays ALIGNED
    with poster resolution — if an item gets a poster from an embedded id, its
    original language resolves from that same id. Previously this only matched
    `[tt…]` / `[tvdb-…]`, so files tagged with a TMDB id (or `{…}` / `tmdbid-`
    / bare forms) got a poster but never resolved a language, leaving them
    stuck as `heuristic` in the Not-API-matched filter."""
    from backend.routes.posters import _extract_ids  # lazy: avoids import cycle
    p = Path(file_path)
    parts = [p.name] + [parent.name for parent in list(p.parents)[:4]]
    for part in parts:
        imdb, tvdb, tmdb = _extract_ids(part)
        if imdb:
            return ("imdb", imdb)
        if tvdb:
            return ("tvdb", tvdb)
        if tmdb:
            return ("tmdb", tmdb)
    return None


# ---------------------------------------------------------------------------
# b) Language-code mapping  (ISO 639-1 -> ISO 639-2/B bibliographic)
# ---------------------------------------------------------------------------

ISO_639_1_TO_2B: dict[str, str] = {
    "en": "eng", "ja": "jpn", "ko": "kor", "is": "isl", "zh": "chi", "cn": "chi",
    "de": "ger", "fr": "fre", "es": "spa", "it": "ita", "pt": "por",
    "ru": "rus", "ar": "ara", "hi": "hin", "th": "tha", "sv": "swe",
    "da": "dan", "no": "nor", "fi": "fin", "nl": "dut", "pl": "pol",
    "tr": "tur", "he": "heb", "uk": "ukr", "cs": "cze", "hu": "hun",
    "ro": "rum", "el": "gre", "bg": "bul", "hr": "hrv", "sr": "srp",
    "vi": "vie", "ms": "may", "id": "ind", "ta": "tam", "te": "tel",
    "bn": "ben", "fa": "per", "ur": "urd", "ka": "kat", "sq": "alb",
    "mk": "mac", "ca": "cat", "cy": "wel", "ga": "gle", "fo": "fao",
    "nb": "nob", "nn": "nno", "af": "afr", "sw": "swa", "eu": "baq",
    "gl": "glg", "mn": "mon", "si": "sin", "ne": "nep", "my": "bur",
    "km": "khm", "lo": "lao", "am": "amh", "zu": "zul", "mt": "mlt",
    "lb": "ltz", "sl": "slv", "sk": "slo", "et": "est", "lv": "lav",
    "lt": "lit", "bs": "bos", "tl": "tgl", "ml": "mal", "kn": "kan",
    "mr": "mar", "pa": "pan", "gu": "guj", "hy": "arm",
}


def map_language_code(code: str) -> str:
    """Map a 2-letter ISO 639-1 code to its 3-letter ISO 639-2/B equivalent.

    If already 3 letters or unknown, return as-is.
    """
    if len(code) == 3:
        return code
    return ISO_639_1_TO_2B.get(code.lower(), code)


# ---------------------------------------------------------------------------
# c) TMDB lookup
# ---------------------------------------------------------------------------


_lookup_debug_budget = 25


def _lookup_debug(msg: str) -> None:
    """v0.9.92: log up to N lookup-failure diagnostics per process — enough to
    see why a lookup returns None (HTTP status, empty results, exception)
    without flooding a 600+ item refresh. Silent-fail paths hid a whole class
    of 'no API data'."""
    global _lookup_debug_budget
    if _lookup_debug_budget > 0:
        _lookup_debug_budget -= 1
        print(f"[METADATA] {msg}", flush=True)


class _TransientLookupError(Exception):
    """TMDB was rate-limiting, failing or unreachable: the answer is unknown,
    so it must not be cached as "no match" (SC-18: a 429 used to hide a title
    for 24 hours)."""


async def _tmdb_request(
    client: httpx.AsyncClient, url: str, params: dict, what: str
) -> httpx.Response:
    """GET from TMDB. A rate limit (429), a server error or a network failure
    raises _TransientLookupError; any other response is returned."""
    try:
        resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        _lookup_debug(f"{what}: request error {exc!r}")
        raise _TransientLookupError(what) from exc
    if resp.status_code == 429 or resp.status_code >= 500:
        _lookup_debug(f"{what}: HTTP {resp.status_code}")
        raise _TransientLookupError(what)
    if resp.status_code not in (200, 404):
        _lookup_debug(f"{what}: HTTP {resp.status_code} {resp.text[:120]!r}")
    return resp


async def _lookup_tmdb(
    imdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language on TMDB via IMDb ID."""
    # params= instead of URL-interpolated `?api_key=…` so httpx exception
    # messages don't carry the raw key if something upstream logs them.
    resp = await _tmdb_request(
        client, f"https://api.themoviedb.org/3/find/{imdb_id}",
        {"external_source": "imdb_id", "api_key": api_key}, f"imdb /find {imdb_id}")
    if resp.status_code != 200:
        return None
    data = resp.json()

    for bucket in ("movie_results", "tv_results"):
        items = data.get(bucket, [])
        if items:
            lang = items[0].get("original_language")
            if lang:
                return lang
    _lookup_debug(f"imdb /find {imdb_id}: no lang "
                  f"(movie={len(data.get('movie_results', []))}, tv={len(data.get('tv_results', []))})")
    return None


async def _lookup_tmdb_by_tvdb(
    tvdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language on TMDB via TVDB ID (TMDB's /find supports
    tvdb_id as an external source)."""
    resp = await _tmdb_request(
        client, f"https://api.themoviedb.org/3/find/{tvdb_id}",
        {"external_source": "tvdb_id", "api_key": api_key}, f"tvdb /find {tvdb_id}")
    if resp.status_code != 200:
        return None
    data = resp.json()
    for bucket in ("tv_results", "movie_results"):
        items = data.get(bucket, [])
        if items:
            lang = items[0].get("original_language")
            if lang:
                return lang
    _lookup_debug(f"tvdb /find {tvdb_id}: no lang "
                  f"(tv={len(data.get('tv_results', []))}, movie={len(data.get('movie_results', []))})")
    return None


async def _lookup_tmdb_by_title(
    title: str, year: "int | str | None", api_key: str, client: httpx.AsyncClient,
    *, prefer_tv: bool = False,
) -> str | None:
    """v0.9.93: fallback when an id lookup comes up empty — TMDB frequently
    lacks the imdb/tvdb external-id link for newer titles even though the title
    itself exists. Search by title (+year) and take the original language of
    the result the poster matcher accepts (v0.10.0, SC-18: this used to take
    the first result without comparing titles, so "Saw" could resolve to
    whatever TMDB ranked first)."""
    from backend.routes.posters import pick_tmdb_match  # lazy: avoids import cycle
    year = str(year) if year else None
    order = ("tv", "movie") if prefer_tv else ("movie", "tv")
    for kind in order:
        params = {"api_key": api_key, "query": title}
        if year:
            params["first_air_date_year" if kind == "tv" else "year"] = year
        resp = await _tmdb_request(
            client, f"https://api.themoviedb.org/3/search/{kind}", params,
            f"title search {kind} {title!r}")
        if resp.status_code != 200:
            continue
        match = pick_tmdb_match(resp.json().get("results", []), title, year)
        if match is None:
            _lookup_debug(f"title search {kind} {title!r} ({year}): no result matches the title")
            continue
        lang = match.get("original_language")
        if lang:
            _lookup_debug(f"title search {kind} {title!r} ({year}): matched -> {lang}")
            return lang
    return None


async def _lookup_tmdb_by_tmdb_id(
    tmdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language directly by a TMDB id (v0.9.89). A TMDB id
    doesn't encode whether it's a movie or a show, so try /movie then /tv."""
    for kind in ("movie", "tv"):
        resp = await _tmdb_request(
            client, f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
            {"api_key": api_key}, f"tmdb /{kind}/{tmdb_id}")
        if resp.status_code != 200:
            continue
        lang = resp.json().get("original_language")
        if lang:
            return lang
    return None


# Folders whose name is not the title: disc structure, season and disc-number
# folders, and extras. The title is the nearest folder above them.
_NON_TITLE_DIR_RE = re.compile(
    r"(?:bdmv|video_ts|audio_ts|stream|playlist|clipinf|backup|certificate"
    r"|(?:season|series|staffel|saison|temporada|stagione|seizoen|sæson|säsong|sesong|kausi)[ ._-]*\d+"
    r"|s\d{1,3}|specials?"
    r"|(?:disc|disk|dvd|cd|bd)[ ._-]*\d+"
    r"|extras?|featurettes?|behind[ ._-]the[ ._-]scenes|deleted[ ._-]scenes|interviews?"
    r"|scenes|shorts|trailers?|samples?|other)",
    re.IGNORECASE,
)
_SEASON_DIR_RE = re.compile(
    r"(?:season|series|staffel|saison|temporada|stagione|seizoen|sæson|säsong|sesong|kausi)[ ._-]*\d+"
    r"|s\d{1,3}|specials?",
    re.IGNORECASE,
)
_EPISODE_NAME_RE = re.compile(r"s\d{1,3}e\d{1,4}", re.IGNORECASE)


def title_search_name(file_path: str, media_roots: list[str]) -> tuple[str | None, bool]:
    """(path whose last part names the title, whether it looks like TV) for a
    title search (SC-18). The parent folder used to be searched as-is, so a
    disc searched "BDMV" or "VIDEO_TS", a season folder "S01" or "Staffel 1",
    and a file directly in a media folder that folder's name ("movies").

    Walks up past disc, season, disc-number and extras folders to the title
    folder. A file directly in a media folder (or with no title folder above
    it) is named by its own file name instead; a disc's marker file names
    nothing, so then there is no title (None)."""
    p = Path(file_path)
    roots = {r.rstrip("/") for r in media_roots if r}
    tv = bool(_EPISODE_NAME_RE.search(p.name))
    folder = p.parent
    while folder.name and str(folder) not in roots:
        if not _NON_TITLE_DIR_RE.fullmatch(folder.name):
            return str(folder), tv
        tv = tv or bool(_SEASON_DIR_RE.fullmatch(folder.name))
        folder = folder.parent
    if p.name.lower() in ("index.bdmv", "video_ts.ifo"):
        return None, tv
    return str(p.parent / p.stem), tv


# ---------------------------------------------------------------------------
# d) Main orchestrator
# ---------------------------------------------------------------------------


_no_key_logged = False


async def lookup_original_language(file_path: str) -> str | None:
    """Resolve the original language for *file_path* using TMDB/TVDB APIs.

    Returns a 3-letter ISO 639-2/B code, or None if lookup fails / no ID found.
    """
    global _no_key_logged
    parsed = parse_media_id(file_path)

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # v0.9.95: resolve by title/year even when there's no id in the path.
        # TMDB matches by name, so id-less items (Plex/manually-organised
        # libraries) can still be looked up — the id-less title search is
        # cached under a title key.
        async with db.execute("SELECT path FROM media_dirs") as cur:
            media_roots = [r["path"] for r in await cur.fetchall()]
        from backend.routes.posters import parse_folder_name
        name_path, is_tv = title_search_name(file_path, media_roots)
        _meta = parse_folder_name(name_path, walk_files=False) if name_path else {}
        title, year = _meta.get("title"), _meta.get("year")
        if parsed:
            id_type, media_id = parsed
        else:
            if not title:
                return None
            id_type, media_id = "title", f"{title.lower()}:{year or ''}"

        # Ensure metadata_cache table exists
        await db.execute(
            "CREATE TABLE IF NOT EXISTS metadata_cache ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "id_type TEXT NOT NULL, media_id TEXT NOT NULL, "
            "original_language TEXT, raw_api_language TEXT, "
            "looked_up_at TEXT NOT NULL, UNIQUE(id_type, media_id))"
        )
        # Fetch TMDB API key (TMDB resolves both IMDb and TVDB IDs via /find)
        tmdb_key = await resolve_tmdb_key(db)

        # Check cache
        async with db.execute(
            "SELECT original_language, raw_api_language, looked_up_at "
            "FROM metadata_cache WHERE id_type = ? AND media_id = ?",
            (id_type, media_id),
        ) as cur:
            cached = await cur.fetchone()

        if cached:
            if cached["original_language"]:
                return cached["original_language"]
            # Cached as NULL (failed lookup) — only retry after 24h
            looked_up = datetime.fromisoformat(cached["looked_up_at"])
            age = (datetime.now(timezone.utc) - looked_up).total_seconds()
            if age < 86400:
                return None

        # Use TMDB for both IMDb and TVDB lookups (TMDB supports both external ID types)
        if not tmdb_key:
            # Once per process: a scan without a key used to log this for
            # every file.
            if not _no_key_logged:
                _no_key_logged = True
                print("[METADATA] No TMDB API key, so original languages are not "
                      "looked up (add one in Settings → Metadata)", flush=True)
            return None

        # Do the lookup via TMDB
        raw_lang: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if id_type == "imdb":
                    raw_lang = await _lookup_tmdb(media_id, tmdb_key, client)
                elif id_type == "tmdb":
                    raw_lang = await _lookup_tmdb_by_tmdb_id(media_id, tmdb_key, client)
                elif id_type == "tvdb":
                    raw_lang = await _lookup_tmdb_by_tvdb(media_id, tmdb_key, client)

                # v0.9.93: id lookup came up empty (TMDB often lacks the
                # external-id link for newer titles) — fall back to a
                # title/year search, the same thing manual matching does.
                if not raw_lang and title:
                    raw_lang = await _lookup_tmdb_by_title(
                        title, year, tmdb_key, client,
                        prefer_tv=(id_type == "tvdb" or is_tv),
                    )
        except _TransientLookupError:
            # Rate-limited, failing or unreachable: unknown, not "no match".
            # Not cached, so the next scan or refresh asks again.
            return None

        mapped = map_language_code(raw_lang) if raw_lang else None
        now = datetime.now(timezone.utc).isoformat()

        if raw_lang:
            print(
                f"[METADATA] {id_type} lookup for {media_id}: {raw_lang} -> {mapped}",
                flush=True,
            )

        # Cache result
        await db.execute(
            "INSERT OR REPLACE INTO metadata_cache "
            "(id_type, media_id, original_language, raw_api_language, looked_up_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (id_type, media_id, mapped, raw_lang, now),
        )
        await db.commit()

        return mapped
    finally:
        await db.close()


async def recheck_title_matches() -> int:
    """One-time heal (v0.10.0, SC-18). Title searches used to take TMDB's first
    result and could search "BDMV" or "Season 01"; the answer was stored as
    source 'api', which a refresh never looks up again. Forget the cached title
    answers and return the rows they came from (no id in the path) to
    'heuristic', so the next scan or metadata refresh looks them up with the
    checked search. Their language stays as it is until then."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'title_matches_rechecked_v010'"
        ) as cur:
            if await cur.fetchone():
                return 0
        async with db.execute(
            "SELECT id, file_path FROM scan_results WHERE language_source = 'api'"
        ) as cur:
            ids = [(r["id"],) for r in await cur.fetchall()
                   if parse_media_id(r["file_path"]) is None]
        await db.executemany(
            "UPDATE scan_results SET language_source = 'heuristic' WHERE id = ?", ids)
        await db.execute("DELETE FROM metadata_cache WHERE id_type = 'title'")
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('title_matches_rechecked_v010', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        await db.commit()
    finally:
        await db.close()
    if ids:
        print(f"[METADATA] {len(ids)} title(s) matched by name only will be looked up "
              f"again on the next scan or metadata refresh", flush=True)
    return len(ids)


# ---------------------------------------------------------------------------
# g) Test endpoint helpers
# ---------------------------------------------------------------------------


async def test_tmdb_key(api_key: str) -> bool:
    """Validate a TMDB API key by looking up The Shawshank Redemption (tt0111161)."""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            lang = await _lookup_tmdb("tt0111161", api_key, client)
        except _TransientLookupError:
            return False
        return lang == "en"
