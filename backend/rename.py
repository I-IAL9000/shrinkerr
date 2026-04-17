"""File renaming engine.

Supports Plex-optimized naming with a Sonarr/Radarr-style token system.
Tokens are resolved from (in order): filename parsing, *arr APIs, TMDB.

Default patterns match Plex's recommended structure:
  Movies: {Title} ({Year}) {Quality} {VideoCodec} {AudioCodec} {AudioChannels}-{ReleaseGroup}
  TV:     {SeriesTitle} - S{season:00}E{episode:00} - {EpisodeTitle} {Quality} ...
"""
from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass, asdict
from typing import Optional

import aiosqlite
import httpx

from backend.database import DB_PATH
from backend.media_parser import parse_media_name, parse_media_path


# ── Defaults (Plex-optimized) ────────────────────────────────────────────────

DEFAULT_MOVIE_FILE_PATTERN = (
    "{Title} ({Year}) {Quality} {VideoDynamicRange} {AudioCodec} {AudioChannels} {VideoCodec}-{ReleaseGroup}"
)
DEFAULT_MOVIE_FOLDER_PATTERN = "{Title} ({Year}) [tmdb-{TmdbId}]"
DEFAULT_TV_FILE_PATTERN = (
    "{SeriesTitle} - S{season:00}E{episode:00} - {EpisodeTitle} "
    "{Quality} {VideoDynamicRange} {AudioCodec} {AudioChannels} {VideoCodec}-{ReleaseGroup}"
)
DEFAULT_TV_FOLDER_PATTERN = "{SeriesTitle} ({Year}) [tvdb-{TvdbId}]"
DEFAULT_SEASON_FOLDER_PATTERN = "Season {season:00}"


# ── Tokens (exposed to the frontend for the picker) ──────────────────────────

TOKEN_CATEGORIES: list[dict] = [
    {
        "category": "Movie",
        "media_type": "movie",
        "tokens": [
            {"token": "Title", "example": "Dragonfly", "desc": "Movie title"},
            {"token": "Year", "example": "2002", "desc": "Release year"},
            {"token": "TmdbId", "example": "12345", "desc": "TMDB ID"},
            {"token": "ImdbId", "example": "tt0231402", "desc": "IMDb ID"},
            {"token": "Edition", "example": "Director's Cut", "desc": "Edition"},
        ],
    },
    {
        "category": "TV",
        "media_type": "tv",
        "tokens": [
            {"token": "SeriesTitle", "example": "Firefly", "desc": "Series title"},
            {"token": "Year", "example": "2002", "desc": "Series year"},
            {"token": "season", "example": "1", "desc": "Season number (use {season:00} for zero-padded)"},
            {"token": "episode", "example": "1", "desc": "Episode number (use {episode:00} for zero-padded)"},
            {"token": "EpisodeTitle", "example": "Serenity", "desc": "Episode title"},
            {"token": "TvdbId", "example": "78874", "desc": "TVDB ID"},
        ],
    },
    {
        "category": "Quality",
        "media_type": "both",
        "tokens": [
            {"token": "Quality", "example": "1080p BluRay", "desc": "Combined resolution + source. Don't also use {Resolution} or {Source} or you'll get duplicates."},
            {"token": "Resolution", "example": "1080p", "desc": "Video resolution only (use instead of {Quality} if you want resolution and source separated)"},
            {"token": "Source", "example": "BluRay", "desc": "Source only, e.g. BluRay, WEB-DL, HDTV (use instead of {Quality} if you want resolution and source separated)"},
            {"token": "VideoCodec", "example": "x265", "desc": "Video codec"},
            {"token": "VideoDynamicRange", "example": "HDR", "desc": "HDR / HDR10+ / DV / Dolby Vision, blank for SDR"},
        ],
    },
    {
        "category": "Audio",
        "media_type": "both",
        "tokens": [
            {"token": "AudioCodec", "example": "DTS", "desc": "Audio codec"},
            {"token": "AudioChannels", "example": "5.1", "desc": "Audio channel layout"},
        ],
    },
    {
        "category": "Release",
        "media_type": "both",
        "tokens": [
            {"token": "ReleaseGroup", "example": "DiMEPiECE", "desc": "Release group"},
            {"token": "Proper", "example": "Proper", "desc": "Proper/Repack marker"},
        ],
    },
]


@dataclass
class RenameSettings:
    enabled_auto: bool = False
    rename_folders: bool = False
    movie_file_pattern: str = DEFAULT_MOVIE_FILE_PATTERN
    movie_folder_pattern: str = DEFAULT_MOVIE_FOLDER_PATTERN
    tv_file_pattern: str = DEFAULT_TV_FILE_PATTERN
    tv_folder_pattern: str = DEFAULT_TV_FOLDER_PATTERN
    season_folder_pattern: str = DEFAULT_SEASON_FOLDER_PATTERN
    separator: str = "space"    # space | dot | dash | underscore
    case_mode: str = "default"  # default | lower | upper
    remove_illegal: bool = True


_SETTINGS_KEYS = {
    "rename_enabled_auto": "enabled_auto",
    "rename_folders": "rename_folders",
    "rename_movie_file_pattern": "movie_file_pattern",
    "rename_movie_folder_pattern": "movie_folder_pattern",
    "rename_tv_file_pattern": "tv_file_pattern",
    "rename_tv_folder_pattern": "tv_folder_pattern",
    "rename_season_folder_pattern": "season_folder_pattern",
    "rename_separator": "separator",
    "rename_case_mode": "case_mode",
    "rename_remove_illegal": "remove_illegal",
}


async def get_settings() -> RenameSettings:
    """Load rename settings from DB."""
    s = RenameSettings()
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN (%s)"
            % ",".join("?" * len(_SETTINGS_KEYS)),
            list(_SETTINGS_KEYS.keys()),
        ) as cur:
            for row in await cur.fetchall():
                attr = _SETTINGS_KEYS[row["key"]]
                val = row["value"]
                if attr in ("enabled_auto", "rename_folders", "remove_illegal"):
                    setattr(s, attr, val.lower() == "true")
                else:
                    setattr(s, attr, val)
    finally:
        await db.close()
    return s


async def save_settings(data: dict) -> RenameSettings:
    """Persist rename settings to DB and return the merged config."""
    current = await get_settings()
    current_dict = asdict(current)
    updates = []
    for db_key, attr in _SETTINGS_KEYS.items():
        if attr in data:
            val = data[attr]
            current_dict[attr] = val
            if isinstance(val, bool):
                val = "true" if val else "false"
            updates.append((db_key, str(val)))

    if updates:
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                updates,
            )
            await db.commit()
        finally:
            await db.close()
    return RenameSettings(**current_dict)


# ── Metadata resolution ──────────────────────────────────────────────────────

@dataclass
class RenameMeta:
    # Primary identifiers
    title: str = ""
    year: Optional[str] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None
    # TV-specific
    series_title: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: str = ""
    # Media technical
    resolution: str = ""
    source: str = ""
    video_codec: str = ""
    video_dynamic_range: str = ""
    audio_codec: str = ""
    audio_channels: str = ""
    release_group: str = ""
    # Other
    edition: str = ""
    proper: str = ""
    media_type: str = "movie"  # "movie" | "tv"


_HDR_FROM_NAME_RE = re.compile(r"\b(HDR10Plus|HDR10|HDR|Dolby[\s\.\-]?Vision|DoVi|DV)\b", re.IGNORECASE)
_CHANNELS_FROM_NAME_RE = re.compile(r"\b([257])[\.\s]([012])\b")


def _parse_hdr_from_name(name: str) -> str:
    """Extract HDR/Dolby Vision tag from filename."""
    m = _HDR_FROM_NAME_RE.search(name)
    if not m:
        return ""
    v = m.group(1).lower().replace(".", "").replace("-", "").replace(" ", "")
    if "hdr10plus" in v:
        return "HDR10+"
    if "hdr10" in v:
        return "HDR10"
    if v == "hdr":
        return "HDR"
    if "dolbyvision" in v or v in ("dovi", "dv"):
        return "DV"
    return m.group(1)


def _parse_channels_from_audio(audio: str, name: str) -> str:
    """Extract channel layout (5.1, 7.1, 2.0) from audio codec tag or filename."""
    # Check the audio codec string first — DDP5.1, AAC5.1, etc.
    if audio:
        m = re.search(r"([257])[\.\s]?([012])\b", audio)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    # Fall back to pattern in filename: "...DTS.5.1..." or "...DTS 5.1..."
    m = _CHANNELS_FROM_NAME_RE.search(name)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return ""


def _strip_channels_from_audio(audio: str) -> str:
    """Strip trailing channel suffix from an audio codec so we render them separately."""
    if not audio:
        return audio
    return re.sub(r"\s*[257][\.\s]?[012]\s*$", "", audio).strip()


async def resolve_metadata(file_path: str, probe_info: Optional[dict] = None) -> RenameMeta:
    """Resolve all metadata tokens for a file. Order: filename → *arr → TMDB."""
    meta = RenameMeta()

    # Step 1: filename parsing (always runs)
    fname = os.path.basename(file_path)
    parsed = parse_media_name(fname)
    meta.media_type = parsed.media_type
    meta.title = parsed.title or ""
    meta.year = parsed.year
    meta.resolution = parsed.resolution or ""
    meta.source = _normalize_source(parsed.source or "")
    meta.video_codec = _normalize_video_codec(parsed.codec or "")
    meta.audio_codec = _strip_channels_from_audio(parsed.audio or "")
    meta.audio_channels = _parse_channels_from_audio(parsed.audio or "", fname)
    meta.video_dynamic_range = _parse_hdr_from_name(fname)
    meta.release_group = parsed.release_group or ""

    if parsed.season is not None:
        meta.season = parsed.season
    if parsed.episode is not None:
        meta.episode = parsed.episode
    if meta.media_type == "tv":
        meta.series_title = meta.title

    # Extract IDs from folder path (bracketed tags)
    folder_ids = _extract_ids_from_path(file_path)
    meta.tmdb_id = folder_ids.get("tmdb") or meta.tmdb_id
    meta.tvdb_id = folder_ids.get("tvdb") or meta.tvdb_id
    meta.imdb_id = folder_ids.get("imdb") or meta.imdb_id

    # Audio channels from probe info if available (overrides filename parsing)
    if probe_info:
        try:
            audio_tracks = probe_info.get("audio_tracks") or []
            for t in audio_tracks:
                if t.get("channels"):
                    meta.audio_channels = _format_channels(t["channels"])
                    break
            # Video dynamic range (overrides filename parsing when available)
            if probe_info.get("hdr"):
                meta.video_dynamic_range = probe_info["hdr"]
        except Exception:
            pass

    # Step 2: Sonarr/Radarr lookup (for canonical titles + episode titles)
    try:
        if meta.media_type == "tv":
            await _enrich_from_sonarr(meta, file_path)
        else:
            await _enrich_from_radarr(meta, file_path)
    except Exception:
        pass

    # Step 3: TMDB fallback if missing critical fields
    try:
        if not meta.title or (meta.media_type == "movie" and not meta.tmdb_id):
            await _enrich_from_tmdb(meta)
    except Exception:
        pass

    return meta


def _normalize_video_codec(codec: str) -> str:
    """Normalize parsed codec to a consistent form."""
    c = codec.lower().replace(".", "").replace(" ", "")
    if "265" in c or "hevc" in c:
        return "x265"
    if "264" in c or "avc" in c:
        return "x264"
    if "av1" in c:
        return "AV1"
    return codec


def _normalize_source(source: str) -> str:
    """Normalize parsed source tag to a canonical form.
    Fixes scene-mangled forms like 'www' → 'WEB'."""
    if not source:
        return source
    s = source.lower().replace(".", "").replace("-", "").replace(" ", "")
    if s == "www":
        return "WEB"
    if s == "webdl":
        return "WEB-DL"
    if s == "webrip":
        return "WEBRip"
    if s == "bluray" or s == "blueray":
        return "BluRay"
    if s == "hdrip":
        return "HDRip"
    if s == "tvrip":
        return "TVRip"
    if s == "hdtv":
        return "HDTV"
    if s == "bdrip":
        return "BDRip"
    if s == "brrip":
        return "BRRip"
    if s == "dvdrip":
        return "DVDRip"
    return source


def _format_channels(ch: int) -> str:
    """Format channel count as '5.1', '7.1', 'stereo', etc."""
    if ch == 1: return "mono"
    if ch == 2: return "2.0"
    if ch == 6: return "5.1"
    if ch == 8: return "7.1"
    return str(ch)


def _extract_ids_from_path(file_path: str) -> dict:
    """Pull [tmdb-xxx], [tvdb-xxx], [imdb-ttxxx] / [tt123] from the path."""
    ids: dict = {}
    for m in re.finditer(r"\[(tmdb|tvdb|imdb)[-:]([a-zA-Z0-9]+)\]", file_path):
        ids[m.group(1)] = m.group(2)
    # Bare IMDb ID format: [tt1234567]
    m = re.search(r"\[(tt\d+)\]", file_path)
    if m and "imdb" not in ids:
        ids["imdb"] = m.group(1)
    return ids


async def _get_arr_url_key(service: str) -> tuple[str, str, str]:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        keys = [f"{service}_url", f"{service}_api_key", f"{service}_path_mapping"]
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(keys))})",
            keys,
        ) as cur:
            rows = {r["key"]: r["value"] for r in await cur.fetchall()}
    finally:
        await db.close()
    return (
        rows.get(f"{service}_url", "").rstrip("/"),
        rows.get(f"{service}_api_key", ""),
        rows.get(f"{service}_path_mapping", ""),
    )


async def _enrich_from_sonarr(meta: RenameMeta, file_path: str) -> None:
    """Look up canonical series + episode title from Sonarr."""
    url, key, path_map = await _get_arr_url_key("sonarr")
    if not url or not key or meta.season is None or meta.episode is None:
        return
    from backend.arr import _translate_path
    mapped_path = _translate_path(file_path, path_map) if path_map else file_path
    async with httpx.AsyncClient(timeout=8) as client:
        # Find series by parsing title — try /api/v3/parse
        try:
            resp = await client.get(
                f"{url}/api/v3/parse",
                params={"title": os.path.basename(file_path)},
                headers={"X-Api-Key": key},
            )
            if resp.status_code == 200:
                data = resp.json()
                series = data.get("series") or {}
                if series.get("title"):
                    meta.series_title = series["title"]
                    if series.get("year"):
                        meta.year = str(series["year"])
                if series.get("tvdbId"):
                    meta.tvdb_id = str(series["tvdbId"])
                episodes = data.get("episodes") or []
                if episodes:
                    ep = episodes[0]
                    if ep.get("title"):
                        meta.episode_title = ep["title"]
        except Exception:
            pass


async def _enrich_from_radarr(meta: RenameMeta, file_path: str) -> None:
    """Look up canonical movie title + IDs from Radarr."""
    url, key, path_map = await _get_arr_url_key("radarr")
    if not url or not key:
        return
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            resp = await client.get(
                f"{url}/api/v3/parse",
                params={"title": os.path.basename(file_path)},
                headers={"X-Api-Key": key},
            )
            if resp.status_code == 200:
                data = resp.json()
                movie = data.get("movie") or {}
                if movie.get("title"):
                    meta.title = movie["title"]
                if movie.get("year"):
                    meta.year = str(movie["year"])
                if movie.get("tmdbId"):
                    meta.tmdb_id = str(movie["tmdbId"])
                if movie.get("imdbId"):
                    meta.imdb_id = movie["imdbId"]
        except Exception:
            pass


async def _enrich_from_tmdb(meta: RenameMeta) -> None:
    """Fallback title + IDs from TMDB (only when *arr isn't configured or didn't match)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'tmdb_api_key'") as cur:
            row = await cur.fetchone()
        tmdb_key = row["value"] if row else ""
    finally:
        await db.close()
    if not tmdb_key or not meta.title:
        return
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            params: dict = {"api_key": tmdb_key, "query": meta.title}
            if meta.year:
                params["year"] = meta.year
            resp = await client.get("https://api.themoviedb.org/3/search/multi", params=params)
            if resp.status_code != 200:
                return
            results = resp.json().get("results", [])
            for item in results:
                mt = item.get("media_type")
                if mt != meta.media_type and mt in ("movie", "tv"):
                    continue
                if mt == "movie" and item.get("title"):
                    meta.title = item["title"]
                    if item.get("release_date"):
                        meta.year = item["release_date"][:4]
                    meta.tmdb_id = str(item.get("id")) if item.get("id") else meta.tmdb_id
                elif mt == "tv" and item.get("name"):
                    meta.series_title = item["name"]
                    if item.get("first_air_date"):
                        meta.year = item["first_air_date"][:4]
                break
        except Exception:
            pass


# ── Pattern rendering ────────────────────────────────────────────────────────

# Format: {Token} or {Token:00} (zero-padding for numeric tokens)
_TOKEN_RE = re.compile(r"\{([A-Za-z]+)(?::(\d+))?\}")


def _token_value(name: str, meta: RenameMeta) -> str:
    """Resolve a token name to its string value."""
    n = name.lower()
    if n == "title":
        return meta.title
    if n == "year":
        return meta.year or ""
    if n == "tmdbid":
        return meta.tmdb_id or ""
    if n == "imdbid":
        return meta.imdb_id or ""
    if n == "tvdbid":
        return meta.tvdb_id or ""
    if n == "seriestitle":
        return meta.series_title or meta.title
    if n == "season":
        return str(meta.season) if meta.season is not None else ""
    if n == "episode":
        return str(meta.episode) if meta.episode is not None else ""
    if n == "episodetitle":
        return meta.episode_title
    if n == "resolution":
        return meta.resolution
    if n == "source":
        return meta.source
    if n == "quality":
        parts = [p for p in (meta.resolution, meta.source) if p]
        return " ".join(parts)
    if n == "videocodec":
        return meta.video_codec
    if n == "videodynamicrange":
        return meta.video_dynamic_range
    if n == "audiocodec":
        return meta.audio_codec
    if n == "audiochannels":
        return meta.audio_channels
    if n == "releasegroup":
        return meta.release_group
    if n == "edition":
        return meta.edition
    if n == "proper":
        return meta.proper
    return ""


_ILLEGAL_CHARS = r'[<>:"|?*\x00-\x1f]'
_ILLEGAL_RE = re.compile(_ILLEGAL_CHARS)


def _apply_formatting(name: str, settings: RenameSettings) -> str:
    """Apply separator, case, and illegal char cleanup to a rendered name."""
    if settings.remove_illegal:
        name = _ILLEGAL_RE.sub("", name)
        name = name.replace("/", "-").replace("\\", "-")

    # Before separator normalization, clean up orphan separators from empty tokens.
    # E.g. "x264- " or "x264-" (nothing after the dash) comes from {VideoCodec}-{ReleaseGroup}
    # when ReleaseGroup is empty. Same for parentheses/brackets with only whitespace.
    # Strip punctuation followed by whitespace-only or end-of-string.
    name = re.sub(r"([\-\_])\s+", r" ", name)        # "x264- DTS" → "x264 DTS"
    name = re.sub(r"[\-\_]+\s*$", "", name)          # trailing "x264-" → "x264"
    name = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", name)  # empty () / [] / {}
    name = re.sub(r"\s+[\-\_]\s+", " ", name)        # " - " with empty around

    # Separator replacement
    # Replace all existing whitespace runs first, then apply the desired separator
    if settings.separator != "space":
        sep = {"dot": ".", "dash": "-", "underscore": "_"}.get(settings.separator, " ")
        name = re.sub(r"\s+", sep, name)

    # Collapse duplicates and trim
    if settings.separator == "dot":
        name = re.sub(r"\.{2,}", ".", name).strip(".")
    elif settings.separator == "dash":
        name = re.sub(r"-{2,}", "-", name).strip("-")
    elif settings.separator == "underscore":
        name = re.sub(r"_{2,}", "_", name).strip("_")
    else:
        name = re.sub(r"\s{2,}", " ", name).strip()

    # Final pass: strip any trailing punctuation left over (e.g. "x264-" at the very end)
    name = re.sub(r"[\-\_\.\s]+$", "", name)

    # Case
    if settings.case_mode == "lower":
        name = name.lower()
    elif settings.case_mode == "upper":
        name = name.upper()

    return name


def render_pattern(pattern: str, meta: RenameMeta, settings: RenameSettings) -> str:
    """Render a pattern template using the resolved metadata + formatting."""
    def _sub(m: re.Match) -> str:
        token = m.group(1)
        pad = m.group(2)
        val = _token_value(token, meta)
        if pad and val:
            try:
                return str(int(val)).zfill(len(pad))
            except ValueError:
                return val
        return val

    rendered = _TOKEN_RE.sub(_sub, pattern)
    return _apply_formatting(rendered, settings)


# ── High-level rename plan ───────────────────────────────────────────────────

@dataclass
class RenamePlan:
    old_path: str
    new_path: str
    old_folder: Optional[str] = None
    new_folder: Optional[str] = None
    old_season_folder: Optional[str] = None
    new_season_folder: Optional[str] = None
    reason: str = ""            # "noop" if nothing changes


async def build_plan(file_path: str, probe_info: Optional[dict] = None, settings: Optional[RenameSettings] = None) -> RenamePlan:
    """Compute a rename plan for a file. Returns old/new paths; caller applies."""
    if settings is None:
        settings = await get_settings()
    meta = await resolve_metadata(file_path, probe_info)

    # File-level rename
    ext = os.path.splitext(file_path)[1]
    old_dir = os.path.dirname(file_path)
    if meta.media_type == "tv":
        new_basename = render_pattern(settings.tv_file_pattern, meta, settings)
    else:
        new_basename = render_pattern(settings.movie_file_pattern, meta, settings)
    new_basename = new_basename.strip()
    if not new_basename:
        return RenamePlan(old_path=file_path, new_path=file_path, reason="noop")
    new_file_path = os.path.join(old_dir, new_basename + ext)

    plan = RenamePlan(old_path=file_path, new_path=new_file_path)

    # Folder renames (optional)
    if settings.rename_folders:
        if meta.media_type == "tv":
            # Season folder
            if meta.season is not None:
                old_season = old_dir
                # The title folder is one level up
                parent_of_season = os.path.dirname(old_season)
                new_season_name = render_pattern(settings.season_folder_pattern, meta, settings).strip()
                if new_season_name:
                    plan.old_season_folder = old_season
                    plan.new_season_folder = os.path.join(parent_of_season, new_season_name)
                # Series folder
                series_folder = parent_of_season
                new_series_name = render_pattern(settings.tv_folder_pattern, meta, settings).strip()
                if new_series_name:
                    plan.old_folder = series_folder
                    plan.new_folder = os.path.join(os.path.dirname(series_folder), new_series_name)
        else:
            # Movie folder (one level up from file)
            new_movie_folder = render_pattern(settings.movie_folder_pattern, meta, settings).strip()
            if new_movie_folder:
                plan.old_folder = old_dir
                plan.new_folder = os.path.join(os.path.dirname(old_dir), new_movie_folder)

    # If nothing actually changes, mark as noop
    if (plan.new_path == plan.old_path
        and (not plan.new_folder or plan.new_folder == plan.old_folder)
        and (not plan.new_season_folder or plan.new_season_folder == plan.old_season_folder)):
        plan.reason = "noop"

    return plan


async def apply_plan(plan: RenamePlan) -> dict:
    """Execute a rename plan on disk. Returns {old, new, applied, error}.

    Order of operations: file → season folder → series/movie folder.
    Each step only runs if the target differs. Uses os.rename (atomic within fs).
    """
    result: dict = {
        "old_path": plan.old_path,
        "new_path": plan.new_path,
        "applied": False,
        "error": None,
    }
    if plan.reason == "noop":
        result["applied"] = False
        result["error"] = "No changes"
        return result

    try:
        current_path = plan.old_path

        # 1. File rename
        if plan.new_path != plan.old_path:
            os.makedirs(os.path.dirname(plan.new_path), exist_ok=True)
            os.rename(plan.old_path, plan.new_path)
            current_path = plan.new_path

        # 2. Season folder rename (TV only)
        if plan.old_season_folder and plan.new_season_folder and plan.new_season_folder != plan.old_season_folder:
            # The current file moved with the folder rename — update current_path
            os.rename(plan.old_season_folder, plan.new_season_folder)
            current_path = current_path.replace(plan.old_season_folder, plan.new_season_folder, 1)

        # 3. Series/movie folder rename
        if plan.old_folder and plan.new_folder and plan.new_folder != plan.old_folder:
            os.rename(plan.old_folder, plan.new_folder)
            current_path = current_path.replace(plan.old_folder, plan.new_folder, 1)

        result["applied"] = True
        result["new_path"] = current_path
    except Exception as exc:
        result["error"] = str(exc)

    return result
