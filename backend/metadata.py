"""TMDB / TVDB API integration for original-language detection."""

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import httpx

from backend.database import DB_PATH

# ---------------------------------------------------------------------------
# a) ID parsing
# ---------------------------------------------------------------------------

_IMDB_RE = re.compile(r"\[(tt\d+)\]")
_TVDB_RE = re.compile(r"\[tvdb-(\d+)\]")


def parse_media_id(file_path: str) -> tuple[str, str] | None:
    """Walk up to 4 parent directories looking for [tt\\d+] or [tvdb-\\d+]."""
    p = Path(file_path)
    parts = [p.name] + [parent.name for parent in list(p.parents)[:4]]
    for part in parts:
        m = _IMDB_RE.search(part)
        if m:
            return ("imdb", m.group(1))
        m = _TVDB_RE.search(part)
        if m:
            return ("tvdb", m.group(1))
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
# c) TVDB JWT token management
# ---------------------------------------------------------------------------

_tvdb_token: Optional[str] = None
_tvdb_token_expires: float = 0.0


async def _get_tvdb_token(api_key: str, client: httpx.AsyncClient) -> Optional[str]:
    """Return a cached TVDB JWT token, refreshing if expired (24h, 60s buffer)."""
    global _tvdb_token, _tvdb_token_expires

    if _tvdb_token and time.time() < _tvdb_token_expires:
        return _tvdb_token

    try:
        resp = await client.post(
            "https://api4.thetvdb.com/v4/login",
            json={"apikey": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        _tvdb_token = data["data"]["token"]
        _tvdb_token_expires = time.time() + 86400 - 60  # 24h minus 60s buffer
        return _tvdb_token
    except Exception as exc:
        print(f"[METADATA] TVDB auth failed: {exc}", flush=True)
        return None


# ---------------------------------------------------------------------------
# d) TMDB lookup
# ---------------------------------------------------------------------------


async def _lookup_tmdb(
    imdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language on TMDB via IMDb ID."""
    url = (
        f"https://api.themoviedb.org/3/find/{imdb_id}"
        f"?external_source=imdb_id&api_key={api_key}"
    )
    resp = await client.get(url)
    resp.raise_for_status()
    data = resp.json()

    for bucket in ("movie_results", "tv_results"):
        items = data.get(bucket, [])
        if items:
            lang = items[0].get("original_language")
            if lang:
                return lang
    return None


# ---------------------------------------------------------------------------
# e) TVDB lookup
# ---------------------------------------------------------------------------


async def _lookup_tvdb(
    tvdb_id: str, api_key: str, client: httpx.AsyncClient
) -> str | None:
    """Look up original language on TVDB via series ID."""
    token = await _get_tvdb_token(api_key, client)
    if not token:
        return None

    resp = await client.get(
        f"https://api4.thetvdb.com/v4/series/{tvdb_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", {}).get("originalLanguage") or None


# ---------------------------------------------------------------------------
# f) Main orchestrator
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
        # Fetch API keys
        tmdb_key: Optional[str] = None
        tvdb_key: Optional[str] = None
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('tmdb_api_key', 'tvdb_api_key')") as cur:
            rows = await cur.fetchall()
            for row in rows:
                if row["key"] == "tmdb_api_key":
                    tmdb_key = row["value"]
                elif row["key"] == "tvdb_api_key":
                    tvdb_key = row["value"]

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
            elif id_type == "tvdb":
                # TMDB's find endpoint supports tvdb_id as external source
                url = f"https://api.themoviedb.org/3/find/{media_id}?external_source=tvdb_id&api_key={tmdb_key}"
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                    for bucket in ("tv_results", "movie_results"):
                        items = data.get(bucket, [])
                        if items:
                            raw_lang = items[0].get("original_language")
                            if raw_lang:
                                break
                except Exception:
                    pass

        mapped = map_language_code(raw_lang) if raw_lang else None
        now = datetime.now(timezone.utc).isoformat()

        if raw_lang:
            print(
                f"[METADATA] {'TMDB' if id_type == 'imdb' else 'TVDB'} lookup "
                f"for {media_id}: {raw_lang} -> {mapped}",
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


async def test_tvdb_key(api_key: str) -> bool:
    """Validate a TVDB API key by attempting authentication."""
    async with httpx.AsyncClient(timeout=10) as client:
        token = await _get_tvdb_token(api_key, client)
        return token is not None
