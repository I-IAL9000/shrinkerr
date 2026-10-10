"""The Scanner's filters, each defined once (F23, v0.10.0).

The pills, their counts, the folder tree, the file lists and every "these
folders + the active filter" action (Add to Queue, estimates, health checks)
each re-implemented the filters — four copies that drifted: the audio-cleanup
count left out untagged tracks, the source counts and lists disagreed, the
low-bitrate count included files that don't need converting, a Dubbed
selection added nothing. Now a filter is one SQL expression over scan_results
or, when it needs the ignore list, the queue, Plex or the folder labels, one
Python predicate; everything evaluates that.

A filter string is comma-separated ids. Ids in the same group match any of
them ("res_4k,res_1080p" = 4K or 1080p); groups must all match; "!id"
excludes. Ids without a group are a group of their own.
"""
from __future__ import annotations

import asyncio
import bisect
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from backend.resolution import RANKS, sql_resolution_rank

GB = 1024 ** 3
LOW_BITRATE = 3_000_000    # bps; below it a file isn't worth converting
HIGH_BITRATE = 15_000_000  # bps
LISTED = "removed_from_list = 0"  # rows the Scanner shows


@dataclass(frozen=True)
class Filter:
    id: str
    group: Optional[str] = None  # same group: any matches; None: its own group
    sql: Optional[str] = None    # boolean over scan_results columns
    params: Optional[Callable[[], tuple]] = None  # values for sql's "?", at query time
    py: Optional[Callable[[dict, dict], bool]] = None  # (row, ctx) when it needs the context
    pre_sql: Optional[str] = None  # py filters: a necessary condition, to narrow queries


# ── Context helpers (the ignore list, Plex watch state, folder labels) ─────

def is_ignored_path(fp: str, ctx: dict) -> bool:
    """Ignored by hand, by an ignored folder, or by a skip rule (unless exempt)."""
    if fp in ctx["ignored_paths"]:
        return True
    ifs = ctx["ignored_folders_sorted"]
    if ifs:
        idx = bisect.bisect_right(ifs, fp) - 1
        if idx >= 0 and fp.startswith(ifs[idx]):
            return True
    # A skip rule, unless the file or a folder above it is exempt.
    sps = ctx["skip_prefixes_sorted"]
    if not sps:
        return False
    idx = bisect.bisect_right(sps, fp) - 1
    if idx < 0 or not fp.startswith(sps[idx]):
        return False
    exempt = ctx["rule_exempt_paths"]
    if fp in exempt:
        return False
    parent = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
    while parent:
        if parent in exempt:
            return False
        if "/" not in parent.rstrip("/"):
            break
        parent = parent.rstrip("/").rsplit("/", 1)[0] + "/"
    return True


def watch_status(fp: str, ctx: dict) -> Optional[str]:
    """Plex watch status via prefix matching."""
    for status, key in (("watched", "watched_sorted"), ("unwatched", "unwatched_sorted"),
                        ("watchlist", "watchlist_sorted")):
        prefixes = ctx.get(key) or []
        if prefixes:
            idx = bisect.bisect_right(prefixes, fp) - 1
            if idx >= 0 and fp.startswith(prefixes[idx]):
                return status
    return None


def build_dir_label_index(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort (path, label) pairs into a prefix-match-friendly index.

    Each entry's path gets a trailing slash so prefix matches don't false-
    positive on `/media/MovieDocs` when only `/media/Movie` is configured.
    Labels are lowercased for case-insensitive comparison. Sorted by path
    length descending so a nested dir wins over its parent (mirrors
    `media_dir_label_for` in backend/media_paths.py). v0.3.76+.
    """
    out: list[tuple[str, str]] = []
    for path, label in rows:
        if not path:
            continue
        norm = path.rstrip("/") + "/"
        out.append((norm, (label or "").strip().lower()))
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


# Pre-compiled in-path ID detectors. v0.3.85 broadened past the
# original `[tvdb-` / `[tt` / `[tmdb-` substring checks to also match
# curly-brace forms (Plex), `id` suffix forms (Jellyfin), and bare
# forms (file-level tagging without surrounding brackets). Mirrors
# `_extract_ids` in backend/routes/posters.py — both serve the same
# purpose of recognising user-tagged folders/files. Kept independent
# (rather than importing) so the hot-path classifier stays in this
# module.
_RE_TVDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tvdb(?:id)?[-=:]?\d+[\]\})]'         # bracketed/braced
    r'|(?<![a-z0-9])tvdb(?:id)?[-=]\d+(?![a-z0-9]))'  # bare with separator
)
_RE_TMDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tmdb(?:id)?[-=:]?\d+[\]\})]'
    r'|(?<![a-z0-9])tmdb(?:id)?[-=]\d+(?![a-z0-9]))'
)
_RE_IMDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tt\d+[\]\})]'                          # bracketed/braced
    r'|(?<![a-z0-9])tt\d{7,}(?![a-z0-9]))'             # bare, ≥7 digits
)


def classify_type(fp: str, dir_label_index: Optional[list[tuple[str, str]]]) -> str:
    """Classify a file as 'movie', 'tv', or 'other'.

    Resolution priority:
      1. Bracket / brace / bare ID anywhere in the path:
           `tvdb…` → tv;  `tmdb…` or `tt…` → movie.
         Recognised forms (Sonarr/Radarr/Plex/Jellyfin/manual tagging):
           [tvdb-N], [tvdbid-N], {tvdb-N}, tvdb-N, tvdbid-N
           [tmdb-N], [tmdbid-N], {tmdb-N}, tmdb-N, tmdbid-N
           [ttN], {ttN}, ttNNNNNNN (≥7 digits, surrounded by separators)
         The full path is searched, so file-level tagging works
         (`/media/Movies/Foo.tt1234567.mkv`) just as well as folder-
         level. v0.3.85+.
      2. Containing media directory's user-set label — "Movies" → movie,
         "TV Shows" → tv, "Other" / unset → other.
      3. Default to 'other'.
    """
    fp_lower = fp.lower()
    if _RE_TVDB_IN_PATH.search(fp_lower):
        return "tv"
    if _RE_TMDB_IN_PATH.search(fp_lower) or _RE_IMDB_IN_PATH.search(fp_lower):
        return "movie"
    if dir_label_index:
        for prefix, label in dir_label_index:
            if fp.startswith(prefix):
                if label in ("movies", "movie"):
                    return "movie"
                if label in ("tv shows", "tv show", "tv"):
                    return "tv"
                return "other"
    return "other"


# ── Row facts shared by the predicates and the enriched rows ──────────────
# Rows are dicts with scan_results' columns (at least file_path, file_size,
# duration, needs_conversion, converted). Facts several filters use are
# cached on the row under a leading "_".

def row_ignored(row: dict, ctx: dict) -> bool:
    v = row.get("_ignored")
    if v is None:
        v = row["_ignored"] = is_ignored_path(row["file_path"], ctx)
    return v


def row_bitrate(row: dict) -> float:
    dur = row.get("duration") or 0
    return (row.get("file_size") or 0) * 8 / dur if dur > 0 else 0


def row_low_bitrate(row: dict) -> bool:
    """Needs converting by codec, but its bitrate is too low to be worth it."""
    return bool(row.get("needs_conversion")) and (row.get("duration") or 0) > 0 and row_bitrate(row) < LOW_BITRATE


def row_converted(row: dict, ctx: dict) -> bool:
    """Converted by Shrinkerr, or already in the target format in a folder
    where Shrinkerr converted something."""
    fp = row["file_path"]
    if row.get("converted") or fp in ctx["converted_paths"]:
        return True
    parent = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
    return not row.get("needs_conversion") and parent in ctx["converted_folders"]


def row_watch_status(row: dict, ctx: dict) -> Optional[str]:
    if "_watch" not in row:
        row["_watch"] = watch_status(row["file_path"], ctx)
    return row["_watch"]


def row_type(row: dict, ctx: dict) -> str:
    v = row.get("_type")
    if v is None:
        v = row["_type"] = classify_type(row["file_path"], ctx.get("dir_label_index"))
    return v


# ── The filters ───────────────────────────────────────────────────────────

def _like_any(*patterns: str) -> str:
    # LIKE is case-insensitive for ASCII in SQLite.
    return "(" + " OR ".join(f"file_path LIKE '%{p}%'" for p in patterns) + ")"


_REMUX = _like_any("remux")
_BLURAY = _like_any("bluray", "blu-ray", "blu ray", "blu.ray", "blu_ray", "bdrip", "bdmv")
_WEBDL = _like_any("web-dl", "webdl", "webrip")
_HDTV = _like_any("hdtv")
_DVD = _like_any("dvd")
_CODEC = "COALESCE(video_codec, '')"  # LIKE ignores ASCII case
_H264 = f"({_CODEC} LIKE '%264%' OR {_CODEC} LIKE '%avc%')"
_HEVC = f"({_CODEC} LIKE '%265%' OR {_CODEC} LIKE '%hevc%')"
_AV1 = f"{_CODEC} LIKE '%av1%'"
_MPEG2 = f"{_CODEC} LIKE '%mpeg2%'"
_VC1 = f"({_CODEC} LIKE '%vc1%' OR {_CODEC} LIKE '%wmv%')"
_MPEG4 = f"({_CODEC} LIKE '%mpeg4%' OR {_CODEC} LIKE '%xvid%' OR {_CODEC} LIKE '%divx%')"
_VP9 = f"{_CODEC} LIKE '%vp9%'"
_NAMED_CODECS = f"({_H264} OR {_HEVC} OR {_AV1} OR {_MPEG2} OR {_VC1} OR {_MPEG4} OR {_VP9})"
_MKV = "file_path LIKE '%.mkv'"
_MP4 = "(file_path LIKE '%.mp4' OR file_path LIKE '%.m4v' OR file_path LIKE '%.mov')"
_AVI = "file_path LIKE '%.avi'"


def _tracks(column: str, condition: str, hint: tuple[str, ...] = ()) -> str:
    """Any track in a JSON track-list column meets `condition` (on t.value).
    `hint`: substrings one of which the raw JSON must contain — a cheap test
    that spares the per-track check on almost every row."""
    exists = (f"EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid({column}) THEN {column} ELSE '[]' END) t "
              f"WHERE {condition})")
    if not hint:
        return exists
    return "(" + " OR ".join(f"{column} LIKE '%{h}%'" for h in hint) + f") AND {exists}"


# Your languages: the audio and subtitle keep lists (Settings).
_MY_LANGS = ("(SELECT lower(k.value) FROM settings s, json_each(s.value) k "
             "WHERE s.key IN ('always_keep_languages', 'sub_keep_languages') AND json_valid(s.value))")
_TRACK_LANG = "lower(COALESCE(json_extract(t.value, '$.language'), ''))"
_TRACK_TEXT = "(COALESCE(json_extract(t.value, '$.profile'), '') || ' ' || COALESCE(json_extract(t.value, '$.title'), ''))"
_IMAGE_SUB_CODECS = "('hdmv_pgs_subtitle', 'pgssub', 'dvd_subtitle', 'vobsub', 'dvb_subtitle', 'xsub')"


def _job_exists(condition: str) -> str:
    return f"EXISTS (SELECT 1 FROM jobs j WHERE j.file_path = scan_results.file_path AND {condition})"
_UNMATCHED = "(language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))"


def _new_cutoff() -> tuple:
    return ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),)


def _recent_cutoff() -> tuple:
    return (time.time() - 86400,)


_NEEDS = "needs_conversion != 0"
_NEEDS_TIMED = "needs_conversion != 0 AND duration > 0"

_FILTER_LIST = [
    # Status: each its own group, so combining them narrows.
    Filter("new", sql="new_detected_at > ?", params=_new_cutoff),
    Filter("recent", sql="file_mtime > ?", params=_recent_cutoff),
    Filter("needs_conversion", pre_sql=_NEEDS,
           py=lambda r, c: bool(r.get("needs_conversion")) and not row_low_bitrate(r) and not row_ignored(r, c)),
    Filter("low_bitrate", pre_sql=_NEEDS_TIMED,
           py=lambda r, c: row_low_bitrate(r) and not row_ignored(r, c)),
    Filter("high_bitrate", pre_sql=_NEEDS_TIMED,
           py=lambda r, c: bool(r.get("needs_conversion")) and not row_ignored(r, c) and row_bitrate(r) > HIGH_BITRATE),
    # Ignore means "don't convert", not "don't tidy tracks / don't say the
    # audio is untagged" (v0.9.26, v0.9.31): these include ignored titles.
    Filter("sub_cleanup", sql="COALESCE(has_removable_subs_flag, 0) = 1"),
    Filter("unknown_language", sql="COALESCE(has_und_tracks_flag, 0) = 1"),
    Filter("disc_iso", sql="disc_type IS NOT NULL"),
    Filter("ignored", py=lambda r, c: row_ignored(r, c)),
    Filter("duplicates", sql="COALESCE(dup_count, 0) > 1"),
    Filter("corrupt", sql="(COALESCE(probe_status, 'ok') != 'ok' OR COALESCE(health_status, '') = 'corrupt')"),
    Filter("converted", py=row_converted),
    Filter("queued", py=lambda r, c: r["file_path"] in c["queued_paths"]),
    # Video codec
    Filter("x264", "codec", sql=_H264),
    Filter("x265", "codec", sql=_HEVC),
    Filter("av1", "codec", sql=_AV1),
    Filter("codec_mpeg2", "codec", sql=_MPEG2),
    Filter("codec_vc1", "codec", sql=_VC1),
    Filter("codec_mpeg4", "codec", sql=_MPEG4),
    Filter("codec_vp9", "codec", sql=_VP9),
    Filter("misc_codec", "codec", sql=f"NOT {_NAMED_CODECS}"),
    # Container, from the extension (discs and ISOs are "other")
    Filter("container_mkv", "container", sql=_MKV),
    Filter("container_mp4", "container", sql=_MP4),
    Filter("container_avi", "container", sql=_AVI),
    Filter("container_other", "container", sql=f"NOT ({_MKV} OR {_MP4} OR {_AVI})"),
    # Resolution: the one classifier in backend/resolution.py (SC-22).
    *(Filter(f"res_{tier}", "resolution", sql=f"{sql_resolution_rank()} = {RANKS[tier]}")
      for tier in ("4k", "1080p", "720p", "sd")),
    # Size
    Filter("size_small", "size", sql=f"COALESCE(file_size, 0) < {5 * GB}"),
    Filter("size_medium", "size", sql=f"COALESCE(file_size, 0) BETWEEN {5 * GB} AND {10 * GB}"),
    Filter("size_large", "size", sql=f"COALESCE(file_size, 0) > {10 * GB}"),
    Filter("large_files", sql=f"COALESCE(file_size, 0) > {10 * GB}"),  # older name of size_large
    # Audio
    Filter("audio_cleanup", "audio",
           sql="(COALESCE(has_removable_tracks_flag, 0) = 1 OR COALESCE(has_und_tracks_flag, 0) = 1)"),
    Filter("lossless_audio", "audio", sql="COALESCE(has_lossless_audio_flag, 0) = 1"),
    Filter("lossy_audio", "audio", sql="COALESCE(has_lossless_audio_flag, 0) = 0"),
    Filter("object_audio", "audio", sql=_tracks("audio_tracks_json",
           f"{_TRACK_TEXT} LIKE '%atmos%' OR {_TRACK_TEXT} LIKE '%dts:x%' "
           f"OR {_TRACK_TEXT} LIKE '%dts-x%' OR {_TRACK_TEXT} LIKE '%dtsx%'",
           hint=("atmos", "dts:x", "dts-x", "dtsx"))),
    Filter("audio_71", "audio", sql=_tracks("audio_tracks_json", "json_extract(t.value, '$.channels') >= 8")),
    # Subtitles
    Filter("image_subs", "subtitles", sql=_tracks(
        "subtitle_tracks_json", f"lower(COALESCE(json_extract(t.value, '$.codec'), '')) IN {_IMAGE_SUB_CODECS}",
        hint=("pgs", "dvd_subtitle", "vobsub", "dvb_subtitle", "xsub"))),
    # Language
    Filter("dubbed", "language", sql="COALESCE(is_dubbed_flag, 0) = 1"),
    Filter("not_api_matched", "language", sql=_UNMATCHED),
    # No audio or subtitle track in your languages (none set: nothing matches).
    Filter("missing_language", "language", sql=(
        f"EXISTS {_MY_LANGS} "
        f"AND NOT {_tracks('audio_tracks_json', f'{_TRACK_LANG} IN {_MY_LANGS}')} "
        f"AND NOT {_tracks('subtitle_tracks_json', f'{_TRACK_LANG} IN {_MY_LANGS}')}")),
    # Health checks (Corrupt is a status pill: a failed probe counts too)
    Filter("health_never", "health", sql="health_status IS NULL"),
    Filter("health_warnings", "health", sql="health_status = 'warnings'"),
    # What happened when Shrinkerr tried
    Filter("failed_before", "outcome", sql=_job_exists("j.status = 'failed'")),
    Filter("vmaf_rejected", "outcome",
           sql=_job_exists("j.error_key IN ('errors.vmafRejected', 'errors.vmafBelowThreshold')")),
    Filter("no_savings", "outcome", sql=(
        "EXISTS (SELECT 1 FROM ignored_files i WHERE i.file_path = scan_results.file_path "
        "AND i.reason = 'conversion_larger')")),
    # Plex
    *(Filter(f"plex_{s}", "plex", py=(lambda s: lambda r, c: row_watch_status(r, c) == s)(s))
      for s in ("watched", "unwatched", "watchlist")),
    # Type (path IDs, else the media folder's label)
    *(Filter(f"type_{k}", "type", py=(lambda k: lambda r, c: row_type(r, c) == k)(k))
      for k in ("movie", "tv", "other")),
    # Source, from the path. One each, first match wins: a Blu-ray remux
    # is a remux, not also a Blu-ray.
    Filter("src_remux", "source", sql=_REMUX),
    Filter("src_bluray", "source", sql=f"{_BLURAY} AND NOT {_REMUX}"),
    Filter("src_webdl", "source", sql=f"{_WEBDL} AND NOT ({_REMUX} OR {_BLURAY})"),
    Filter("src_hdtv", "source", sql=f"{_HDTV} AND NOT ({_REMUX} OR {_BLURAY} OR {_WEBDL})"),
    Filter("src_dvd", "source", sql=f"{_DVD} AND NOT ({_REMUX} OR {_BLURAY} OR {_WEBDL} OR {_HDTV})"),
    # VMAF of the conversion
    Filter("vmaf_excellent", "vmaf", sql="vmaf_score >= 93"),
    Filter("vmaf_good", "vmaf", sql="vmaf_score >= 87 AND vmaf_score < 93"),
    Filter("vmaf_poor", "vmaf", sql="vmaf_score < 87"),
]
FILTERS: dict[str, Filter] = {f.id: f for f in _FILTER_LIST}

# Columns the Python predicates read.
PY_COLUMNS = "file_path, file_size, duration, needs_conversion, converted, hdr_format"


# ── Advanced Search conditions (v0.10.0) ─────────────────────────────────
# Advanced Search had its own engine: a path list capped at 5,000 that the
# Scanner intersected in the browser, "regex" that was a substring match,
# "Filename" that searched the whole path, a Movie/TV guess that disagreed
# with the pills, and "audio codec is not X" meaning "some track isn't X".
# Its conditions now compile to filters like the pills and travel in the
# filter string as one "adv:" token (base64url JSON {"m": "all"|"any",
# "p": [{"property", "op", "value", "value2"}]}), so the tree, the lists,
# the counts and every "selection + filter" action apply them.

ADVANCED_PROPERTIES: dict[str, dict] = {
    # Video
    "video_codec":   {"kind": "column", "col": "LOWER(COALESCE(video_codec, ''))", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Video codec", "group": "Video",
                      "options": ["h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "vc1", "wmv3"]},
    "video_height":  {"kind": "column", "col": "COALESCE(video_height, 0)", "type": "enum", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Video height (px)", "group": "Video",
                      "options": [480, 540, 576, 720, 1080, 1440, 2160, 4320]},
    "duration_min":  {"kind": "column", "col": "(COALESCE(duration, 0) / 60.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "Duration (min)", "group": "Video"},
    "needs_conversion": {"kind": "column", "col": "needs_conversion", "type": "bool", "ops": ["eq"], "label": "Needs conversion", "group": "Video"},
    "vmaf_score":    {"kind": "column", "col": "vmaf_score", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between", "exists"], "label": "VMAF score", "group": "Video"},
    "hdr":           {"kind": "hdr", "type": "bool", "ops": ["eq"], "label": "HDR / Dolby Vision", "group": "Video"},

    # Size / bitrate
    "file_size_mb":  {"kind": "column", "col": "(COALESCE(file_size, 0) / 1048576.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "File size (MB)", "group": "Size"},
    "file_size_gb":  {"kind": "column", "col": "(COALESCE(file_size, 0) / 1073741824.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "File size (GB)", "group": "Size"},
    "bitrate_mbps":  {"kind": "column", "col": "(CASE WHEN duration > 0 THEN (file_size * 8.0 / duration / 1000000.0) ELSE 0 END)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "Bitrate (Mbps)", "group": "Size"},

    # Audio
    "audio_codec":   {"kind": "tracks", "column": "audio_tracks_json", "field": "codec", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Audio codec (any track)", "group": "Audio",
                      "options": ["aac", "ac3", "eac3", "dts", "truehd", "flac", "mp3", "opus", "vorbis", "pcm_s16le", "pcm_s24le"]},
    "audio_lang":    {"kind": "tracks", "column": "audio_tracks_json", "field": "language", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Audio language (any track)", "group": "Audio",
                      "options": ["eng", "fre", "fra", "spa", "ger", "deu", "ita", "jpn", "kor", "chi", "zho", "rus", "por", "pol", "nld", "swe", "nor", "dan", "fin", "tur", "ara", "hin", "tha", "ind", "vie", "und"]},
    "audio_channels":{"kind": "max_channels", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Audio channels (max)", "group": "Audio", "examples": [2, 6, 8]},
    "audio_track_count": {"kind": "count", "column": "audio_tracks_json", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Audio track count", "group": "Audio"},
    "has_lossless_audio": {"kind": "column", "col": "COALESCE(has_lossless_audio_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has lossless audio", "group": "Audio"},
    "has_removable_tracks": {"kind": "column", "col": "COALESCE(has_removable_tracks_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has removable audio tracks", "group": "Audio"},

    # Subtitles
    "subtitle_lang": {"kind": "tracks", "column": "subtitle_tracks_json", "field": "language", "type": "string", "ops": ["eq", "in", "contains"], "label": "Subtitle language (any)", "group": "Subtitles"},
    "subtitle_count":{"kind": "count", "column": "subtitle_tracks_json", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Subtitle track count", "group": "Subtitles"},
    "has_removable_subs": {"kind": "column", "col": "COALESCE(has_removable_subs_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has removable subs", "group": "Subtitles"},

    # Filename / path
    "source":        {"kind": "source", "type": "enum", "ops": ["eq", "in"], "label": "Source", "group": "Filename",
                      "options": ["remux", "bluray", "webdl", "hdtv", "dvd", "unknown"]},
    "file_path":     {"kind": "path", "type": "string", "ops": ["contains", "regex"], "label": "File path", "group": "Filename"},
    "file_name":     {"kind": "name", "type": "string", "ops": ["contains", "regex"], "label": "Filename", "group": "Filename"},

    # State
    "health_status": {"kind": "column", "col": "health_status", "type": "string", "ops": ["eq", "exists", "in"], "label": "Health status", "group": "State", "examples": ["healthy", "corrupt"]},
    "duplicate_count": {"kind": "column", "col": "COALESCE(dup_count, 0)", "type": "number", "ops": ["eq", "gt", "gte"], "label": "Duplicate count", "group": "State"},

    # Type: the pills' classification (path IDs, else the media folder's label)
    "media_type":    {"kind": "media_type", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Type", "group": "Type",
                      "options": ["movie", "tv", "other"],
                      "option_labels": {"movie": "Movie", "tv": "TV Show", "other": "Other"}},
}

_SQL_OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_HDR_NAME = re.compile(r"\bhdr10\+?\b|\bhdr\b|dolby[\s.]*vision|\.dv\.|\bdv\b(?!d)", re.IGNORECASE)


def _values(value) -> list:
    if isinstance(value, (list, tuple)):
        return [v for v in value if str(v).strip() != ""]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ("true", "1", "yes")


def escape_like(text: str) -> str:
    """`text` for a LIKE pattern with ESCAPE '\\': its % and _ match only themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_condition(index: int, pred: dict) -> Optional[Filter]:
    """One Advanced Search condition as a Filter, or None if it isn't valid
    (an unknown property or operator, or a missing value)."""
    prop = ADVANCED_PROPERTIES.get(pred.get("property"))
    op = pred.get("op")
    if not prop or op not in prop["ops"]:
        return None
    fid, kind, value, value2 = f"adv{index}", prop["kind"], pred.get("value"), pred.get("value2")

    def sql(text: str, *params) -> Filter:
        return Filter(fid, sql=text, params=(lambda: tuple(params)) if params else None)

    def number_cond(expr: str) -> Optional[Filter]:
        if op == "exists":
            return sql(f"{expr} IS NOT NULL")
        if op == "between":
            lo, hi = _num(value), _num(value2)
            return sql(f"{expr} BETWEEN ? AND ?", *sorted((lo, hi))) if lo is not None and hi is not None else None
        n = _num(value)
        return sql(f"{expr} {_SQL_OPS[op]} ?", n) if n is not None and op in _SQL_OPS else None

    if kind == "column":
        col, typ = prop["col"], prop["type"]
        if typ == "bool":
            return sql(f"COALESCE({col}, 0) != 0") if _truthy(value) else sql(f"COALESCE({col}, 0) = 0")
        if typ == "number" or (typ == "enum" and isinstance((prop.get("options") or [""])[0], int)):
            return number_cond(col)
        if op == "exists":
            return sql(f"COALESCE({col}, '') <> ''")
        text = f"LOWER(COALESCE({col}, ''))"
        if op == "in":
            vals = [str(v).lower() for v in _values(value)]
            return sql(f"{text} IN ({','.join('?' * len(vals))})", *vals) if vals else None
        if op in ("eq", "ne") and str(value or "").strip():
            return sql(f"{text} {_SQL_OPS[op]} ?", str(value).strip().lower())
        return None

    if kind == "tracks":
        column, field = prop["column"], prop["field"]
        cell = f"lower(COALESCE(json_extract(t.value, '$.{field}'), ''))"
        if op == "contains" and str(value or "").strip():
            return sql(_tracks(column, f"{cell} LIKE ? ESCAPE '\\'"), f"%{escape_like(str(value).strip().lower())}%")
        vals = [str(v).lower() for v in (_values(value) if op == "in" else [value]) if str(v or "").strip()]
        if not vals:
            return None
        cond = _tracks(column, f"{cell} IN ({','.join('?' * len(vals))})")
        # "is not X": no track is X (it meant "some track isn't X").
        return sql(f"NOT {cond}" if op == "ne" else cond, *vals)

    if kind == "max_channels":
        return number_cond("(SELECT COALESCE(MAX(json_extract(t.value, '$.channels')), 0) FROM json_each("
                           "CASE WHEN json_valid(audio_tracks_json) THEN audio_tracks_json ELSE '[]' END) t)")
    if kind == "count":
        column = prop["column"]
        return number_cond(f"json_array_length(CASE WHEN json_valid({column}) THEN {column} ELSE '[]' END)")

    if kind == "source":
        wanted = [str(v).lower() for v in (_values(value) if op == "in" else [value]) if str(v or "").strip()]
        wanted = ["webdl" if w == "webrip" else w for w in wanted]  # older saved searches
        parts = [f"({FILTERS['src_' + w].sql})" for w in wanted if "src_" + w in FILTERS]
        if "unknown" in wanted:
            parts.append("NOT (" + " OR ".join(f"({FILTERS[k].sql})" for k in FILTERS if k.startswith("src_")) + ")")
        return sql(" OR ".join(parts)) if parts else None

    if kind == "hdr":
        # The probe's HDR format; files scanned before it was stored: the name.
        def is_hdr(row, ctx):
            return bool(row.get("hdr_format")) or bool(_HDR_NAME.search(row["file_path"]))
        want = _truthy(value)
        return Filter(fid, py=lambda r, c: is_hdr(r, c) == want)

    if kind == "path":
        text = str(value or "").strip()
        if not text:
            return None
        if op == "regex":
            try:
                pattern = re.compile(text, re.IGNORECASE)
            except re.error:
                return None
            return Filter(fid, py=lambda r, c: bool(pattern.search(r["file_path"])))
        return sql("file_path LIKE ? ESCAPE '\\'", f"%{escape_like(text)}%")

    if kind == "name":
        text = str(value or "").strip()
        if not text:
            return None
        if op == "regex":
            try:
                pattern = re.compile(text, re.IGNORECASE)
            except re.error:
                return None
            return Filter(fid, py=lambda r, c: bool(pattern.search(r["file_path"].rsplit("/", 1)[-1])))
        needle = text.lower()
        return Filter(fid, py=lambda r, c: needle in r["file_path"].rsplit("/", 1)[-1].lower())

    if kind == "media_type":
        wanted = {str(v).lower() for v in (_values(value) if op == "in" else [value]) if str(v or "").strip()}
        if not wanted:
            return None
        negate = op == "ne"
        return Filter(fid, py=lambda r, c: (row_type(r, c) in wanted) != negate)
    return None


def encode_advanced(predicates: list[dict], match: str = "all") -> str:
    """The filter-string token for a set of conditions."""
    import base64
    import json
    raw = json.dumps({"m": "any" if match == "any" else "all", "p": predicates}, separators=(",", ":"))
    return "adv:" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_advanced(token: str) -> Optional[dict]:
    """{"m", "p"} from an "adv:" token, or None if it can't be read."""
    import base64
    import json
    if not token.startswith("adv:"):
        return None
    data = token[4:]
    try:
        spec = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode())
    except (ValueError, TypeError):
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("p"), list):
        return None
    return {"m": "any" if spec.get("m") == "any" else "all", "p": [p for p in spec["p"] if isinstance(p, dict)]}


# ── Filter expressions ────────────────────────────────────────────────────

@dataclass
class FilterExpr:
    """A parsed filter string; see the module docstring.

    Use: put `select_sql` after the selected columns and `where_sql` at the
    end of the WHERE clause (params in that order), then keep the rows for
    which `post_filter(row, ctx)` is true. `ctx` (the enrichment context)
    is only needed when `needs_ctx`.
    """
    groups: list[list[Filter]] = field(default_factory=list)
    excluded: list[Filter] = field(default_factory=list)
    select_sql: str = ""
    select_params: list = field(default_factory=list)
    where_sql: str = ""
    where_params: list = field(default_factory=list)
    # Groups / exclusions SQL can't settle on its own.
    _py_groups: list[list[Filter]] = field(default_factory=list)
    _py_excluded: list[Filter] = field(default_factory=list)

    @property
    def is_all(self) -> bool:
        return not self.groups and not self.excluded

    @property
    def needs_ctx(self) -> bool:
        return bool(self._py_groups or self._py_excluded)

    def post_filter(self, row: dict, ctx: Optional[dict]) -> bool:
        for group in self._py_groups:
            if not any(_value(f, row, ctx) for f in group):
                return False
        return not any(_value(f, row, ctx) for f in self._py_excluded)


def _value(f: Filter, row: dict, ctx: Optional[dict]) -> bool:
    if f.py:
        return bool(f.py(row, ctx))
    return bool(row[f"_f_{f.id}"])  # selected by select_sql


def parse_filter(text: Optional[str]) -> FilterExpr:
    """Parse "a,b,!c". Unknown ids are ignored (an old bookmark keeps working)."""
    expr = FilterExpr()
    by_group: dict[str, list[Filter]] = {}
    seen: set[str] = set()
    for token in (text or "").split(","):
        token = token.strip()
        spec = decode_advanced(token) if token.startswith("adv:") else None
        if spec is not None:
            # Advanced Search: all conditions must hold (each its own group),
            # or any of them (one group).
            conds = [f for f in (compile_condition(i, p) for i, p in enumerate(spec["p"])) if f]
            if spec["m"] == "any" and conds:
                by_group["_adv"] = conds
            for f in conds if spec["m"] == "all" else ():
                by_group[f"_{f.id}"] = [f]
            continue
        neg = token.startswith("!")
        f = FILTERS.get(token[1:] if neg else token)
        if f is None or token in seen:
            continue
        seen.add(token)
        if neg:
            expr.excluded.append(f)
        else:
            by_group.setdefault(f.group or f"_{f.id}", []).append(f)
    expr.groups = list(by_group.values())

    def sql_of(f: Filter, params: list) -> str:
        if f.params:
            params.extend(f.params())
        return f"({f.sql})"

    where, select = [], []
    for group in expr.groups:
        if all(f.sql for f in group):
            where.append("(" + " OR ".join(sql_of(f, expr.where_params) for f in group) + ")")
            continue
        expr._py_groups.append(group)
        for f in group:
            if f.sql:
                select.append(f"{sql_of(f, expr.select_params)} AS _f_{f.id}")
        # Narrow with the filters' necessary conditions, when all have one.
        if all(f.sql or f.pre_sql for f in group):
            where.append("(" + " OR ".join(
                sql_of(f, expr.where_params) if f.sql else f"({f.pre_sql})" for f in group) + ")")
    for f in expr.excluded:
        if f.sql:
            where.append(f"NOT COALESCE({sql_of(f, expr.where_params)}, 0)")
        else:
            expr._py_excluded.append(f)
    expr.select_sql = "".join(f", {s}" for s in select)
    expr.where_sql = "".join(f" AND {w}" for w in where)
    return expr


# ── Counts ────────────────────────────────────────────────────────────────

async def count_all(db, ctx: dict) -> tuple[dict[str, int], int]:
    """Every filter's count over the Scanner list (the pills), and the total
    size of the files that need converting."""
    sql_filters = [f for f in _FILTER_LIST if f.sql]
    params: list = []
    sums = []
    for f in sql_filters:
        if f.params:
            params.extend(f.params())
        sums.append(f'SUM(CASE WHEN ({f.sql}) THEN 1 ELSE 0 END) AS "{f.id}"')
    async with db.execute(
        f"SELECT COUNT(*) AS \"all\", {', '.join(sums)} FROM scan_results WHERE {LISTED}", params
    ) as cur:
        row = await cur.fetchone()
    counts = {"all": row[0] or 0}
    for i, f in enumerate(sql_filters, start=1):
        counts[f.id] = row[i] or 0

    py_filters = [f for f in _FILTER_LIST if f.py]
    async with db.execute(f"SELECT {PY_COLUMNS} FROM scan_results WHERE {LISTED}") as cur:
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, values)) for values in await cur.fetchall()]

    def tally() -> int:
        needs_conversion_bytes = 0
        for f in py_filters:
            counts[f.id] = 0
        for r in rows:
            for f in py_filters:
                if f.py(r, ctx):
                    counts[f.id] += 1
                    if f.id == "needs_conversion":
                        needs_conversion_bytes += r["file_size"] or 0
        return needs_conversion_bytes

    # CPU-bound (~0.3 s for 60k files): off the event loop.
    return counts, await asyncio.to_thread(tally)
