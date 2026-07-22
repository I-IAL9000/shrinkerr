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


async def _lookup_tmdb(
    imdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language on TMDB via IMDb ID."""
    # params= instead of URL-interpolated `?api_key=…` so httpx exception
    # messages don't carry the raw key if something upstream logs them.
    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"external_source": "imdb_id", "api_key": api_key},
        )
    except Exception as exc:
        _lookup_debug(f"imdb /find {imdb_id}: request error {exc!r}")
        return None
    if resp.status_code != 200:
        _lookup_debug(f"imdb /find {imdb_id}: HTTP {resp.status_code} {resp.text[:120]!r}")
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


async def _lookup_tmdb_by_tmdb_id(
    tmdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language directly by a TMDB id (v0.9.89). A TMDB id
    doesn't encode whether it's a movie or a show, so try /movie then /tv."""
    for kind in ("movie", "tv"):
        try:
            resp = await client.get(
                f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
                params={"api_key": api_key},
            )
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                _lookup_debug(f"tmdb /{kind}/{tmdb_id}: HTTP {resp.status_code} {resp.text[:120]!r}")
                continue
            lang = resp.json().get("original_language")
            if lang:
                return lang
        except Exception as exc:
            _lookup_debug(f"tmdb /{kind}/{tmdb_id}: request error {exc!r}")
            continue
    return None


# ---------------------------------------------------------------------------
# d) Main orchestrator
# ---------------------------------------------------------------------------


async def lookup_original_language(file_path: str) -> str | None:
    """Resolve the original language for *file_path* using TMDB/TVDB APIs.

    Returns a 3-letter ISO 639-2/B code, or None if lookup fails / no ID found.
    """
    parsed = parse_media_id(file_path)
    if parsed is None:
        return None

    id_type, media_id = parsed

    # Read API keys from settings table
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
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
            print(f"[METADATA] No TMDB API key for {id_type} lookup", flush=True)
            return None

        # Do the lookup via TMDB
        raw_lang: Optional[str] = None
        async with httpx.AsyncClient(timeout=10) as client:
            if id_type == "imdb":
                raw_lang = await _lookup_tmdb(media_id, tmdb_key, client)
            elif id_type == "tmdb":
                raw_lang = await _lookup_tmdb_by_tmdb_id(media_id, tmdb_key, client)
            elif id_type == "tvdb":
                # TMDB's find endpoint supports tvdb_id as external source.
                # Pass the key as params= rather than interpolating it into
                # the URL string — means httpx exception messages don't
                # carry the raw key even if something upstream prints them.
                try:
                    resp = await client.get(
                        f"https://api.themoviedb.org/3/find/{media_id}",
                        params={"external_source": "tvdb_id", "api_key": tmdb_key},
                    )
                    if resp.status_code != 200:
                        _lookup_debug(f"tvdb /find {media_id}: HTTP {resp.status_code} {resp.text[:120]!r}")
                    else:
                        data = resp.json()
                        for bucket in ("tv_results", "movie_results"):
                            items = data.get(bucket, [])
                            if items:
                                raw_lang = items[0].get("original_language")
                                if raw_lang:
                                    break
                        if not raw_lang:
                            _lookup_debug(f"tvdb /find {media_id}: no lang "
                                          f"(tv={len(data.get('tv_results', []))}, movie={len(data.get('movie_results', []))})")
                except Exception as exc:
                    _lookup_debug(f"tvdb /find {media_id}: request error {exc!r}")

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


# ---------------------------------------------------------------------------
# g) Test endpoint helpers
# ---------------------------------------------------------------------------


async def test_tmdb_key(api_key: str) -> bool:
    """Validate a TMDB API key by looking up The Shawshank Redemption (tt0111161)."""
    async with httpx.AsyncClient(timeout=10) as client:
        lang = await _lookup_tmdb("tt0111161", api_key, client)
        return lang == "en"
