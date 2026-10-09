"""Resolve encoding rules for files based on media directories, file properties, and Plex metadata."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.database import connect_db
from backend.resolution import resolution_tier

_AGE_UNITS = {"h": 1, "d": 24, "w": 168}


def _parse_age_hours(s: str) -> Optional[int]:
    """Parse a date_added value '24h'/'7d'/'4w' into hours.
    Returns None on malformed input OR zero (rule then short-circuits
    to False). Rejecting zero is intentional — '0h' is semantically
    nonsense for a comparison ('newer than 0 hours ago' is always
    false; 'older than 0 hours' is always true). Forcing it to return
    False matches the spec's existing 'malformed value → False'
    semantics rather than silently producing surprising tautologies.
    v0.5.1+."""
    s = (s or "").strip().lower()
    m = re.match(r"^(\d+)\s*([hdw])$", s)
    if not m:
        return None
    hours = int(m.group(1)) * _AGE_UNITS[m.group(2)]
    if hours <= 0:
        return None
    return hours


def _make_rule_result(rule: dict) -> dict:
    return {
        "rule_id": rule["id"],
        "rule_name": rule["name"],
        "action": rule["action"],
        "encoder": rule.get("encoder"),
        "nvenc_preset": rule.get("nvenc_preset"),
        "nvenc_cq": rule.get("nvenc_cq"),
        "libx265_crf": rule.get("libx265_crf"),
        "libx265_preset": rule.get("libx265_preset"),
        "target_resolution": rule.get("target_resolution"),
        "audio_codec": rule.get("audio_codec"),
        "audio_bitrate": rule.get("audio_bitrate"),
        "queue_priority": rule.get("queue_priority"),
    }


def _parse_rule_conditions(rule: dict) -> tuple[str, list[dict]]:
    """Parse match_conditions JSON into (match_mode, conditions).

    Supports three formats:
      - New object format: {"match_mode": "all", "conditions": [...]}
      - Old array format: [{...}, ...] -> treated as match_mode "any"
      - Legacy single fields: match_type + match_value -> single condition, mode "any"
    """
    raw = rule.get("match_conditions")
    if not raw:
        # Legacy fallback
        if rule.get("match_type") and rule.get("match_value"):
            return "any", [{"type": rule["match_type"], "operator": "is", "value": rule["match_value"]}]
        return "any", []

    parsed = json.loads(raw) if isinstance(raw, str) else raw

    # New format: object with match_mode
    if isinstance(parsed, dict) and "conditions" in parsed:
        return parsed.get("match_mode", "any"), parsed.get("conditions", [])

    # Old format: plain array
    if isinstance(parsed, list):
        return "any", parsed

    return "any", []


def _match_op(actual: str, operator: str, expected: str) -> bool:
    """Compare actual value against expected using operator."""
    if operator == "is":
        return actual.lower() == expected.lower()
    elif operator == "is_not":
        return actual.lower() != expected.lower()
    elif operator == "contains":
        return expected.lower() in actual.lower()
    elif operator == "does_not_contain":
        return expected.lower() not in actual.lower()
    return False


def _detect_source(file_path: str) -> str:
    """Detect media source type from filename."""
    name = os.path.basename(file_path).lower()
    if "remux" in name:
        return "Remux"
    if "web-dl" in name or "webdl" in name:
        return "WEB-DL"
    if "webrip" in name:
        return "WEBRip"
    if "bluray" in name or "blu-ray" in name or "bdrip" in name:
        return "Blu-ray"
    if "hdtv" in name:
        return "HDTV"
    if "dvdrip" in name or "dvd" in name:
        return "DVD"
    return "Other"


_RULE_RESOLUTION_LABELS = {"4k": "4K", "1080p": "1080p", "720p": "720p", "sd": "SD"}


def _detect_resolution(video_width: Optional[int], video_height: Optional[int],
                       file_path: str = "") -> str:
    """Resolution label for rules, from the shared classifier (v0.10.0,
    SC-22: by height alone a 1920x800 film was "720p" and 2560x1440 "4K").
    A file whose size and path say nothing counts as SD, as before."""
    return _RULE_RESOLUTION_LABELS[resolution_tier(video_width, video_height, file_path) or "sd"]


def _detect_media_type(file_path: str) -> str:
    """Detect whether a file is TV or movie based on path patterns."""
    # S##E## pattern (including S#E# for single digits)
    if re.search(r'[Ss]\d{1,2}[Ee]\d{1,3}', file_path):
        return "tv"
    if "/Season " in file_path or "/Series " in file_path:
        return "tv"
    if "/Specials/" in file_path:
        return "tv"
    # Fallback: use media parser on the filename
    from backend.media_parser import parse_media_name
    parsed = parse_media_name(os.path.basename(file_path))
    return parsed.media_type


def _parse_release_group(file_path: str) -> str:
    """Extract release group from filename (last segment after final dash before extension)."""
    name = os.path.splitext(os.path.basename(file_path))[0]
    match = re.search(r'-([A-Za-z0-9]+)$', name)
    return match.group(1) if match else ""


def _codec_family_match(actual: str, expected: str) -> bool:
    """Check if two codec strings belong to the same family."""
    actual_l = actual.lower()
    expected_l = expected.lower()

    if actual_l == expected_l:
        return True

    # H.264 family
    h264_family = {"h264", "x264", "avc"}
    if actual_l in h264_family and expected_l in h264_family:
        return True

    # HEVC family
    hevc_family = {"hevc", "h265", "x265"}
    if actual_l in hevc_family and expected_l in hevc_family:
        return True

    return False


def _audio_codec_family_match(actual: str, expected: str) -> bool:
    """Check if two audio codec strings belong to the same family."""
    actual_l = actual.lower()
    expected_l = expected.lower()

    if actual_l == expected_l:
        return True

    # DTS family
    dts_family = {"dts", "dts-hd ma", "dts-hd hra"}
    if actual_l in dts_family and expected_l in dts_family:
        return True

    # TrueHD — just normalize
    if "truehd" in actual_l and "truehd" in expected_l:
        return True

    return False


# Every condition type _check_condition evaluates. Rule create / update
# accept exactly these — creation refused seven the rule editor offers
# (v0.10.0).
CONDITION_TYPES = frozenset({
    "directory", "source", "resolution", "video_codec", "audio_codec", "file_size",
    "date_added", "media_type", "title", "release_group",
    "label", "collection", "genre", "library", "plex_watched",
    "jellyfin_tag", "jellyfin_watched", "emby_tag", "emby_watched",
    "arr_tag", "nzbget_category",
})
# Conditions matched through plex_metadata_cache. Only Plex label /
# collection / genre / library rules used to load it, so a watched or
# Jellyfin/Emby tag rule on its own never matched (v0.10.0).
_CACHE_CONDITION_TYPES = frozenset({
    "label", "collection", "genre", "library",
    "plex_watched", "jellyfin_watched", "emby_watched", "jellyfin_tag", "emby_tag",
})


def _check_condition(cond: dict, file_path: str, scan_row: dict,
                     folder_metadata: list[tuple[str, str]],
                     extra_context: dict | None = None,
                     arr_tags: set[str] | frozenset = frozenset()) -> bool:
    """Check if a single condition matches a file.

    Args:
        cond: Condition dict with type, operator, value keys.
        file_path: Absolute path to the media file.
        scan_row: Dict with keys from scan_results: file_path, file_size,
                  video_codec, video_height, audio_tracks_json.
                  May be empty if file hasn't been scanned yet.
        folder_metadata: List of (metadata_type, metadata_value) tuples
                        from plex_metadata_cache for this file's folder hierarchy.
        extra_context: Optional dict with additional context (e.g. nzbget_category).
        arr_tags: Lower-cased Sonarr/Radarr tags of the file's series / movie.
    """
    ctype = cond.get("type", "")
    op = cond.get("operator", "is")
    value = cond.get("value", "")

    if not value:
        return False

    # 1. Directory — always prefix match, ignore operator
    if ctype == "directory":
        dir_prefix = value.rstrip("/") + "/"
        return file_path.startswith(dir_prefix)

    # 2. Source — detect from filename
    if ctype == "source":
        detected = _detect_source(file_path)
        return _match_op(detected, op, value)

    # 3. Resolution — from scan_row video_width / video_height
    if ctype == "resolution":
        detected = _detect_resolution(scan_row.get("video_width"), scan_row.get("video_height"), file_path)
        return _match_op(detected, op, value)

    # 4. Video codec — with family matching
    if ctype == "video_codec":
        actual_codec = (scan_row.get("video_codec") or "").strip()
        if not actual_codec:
            return False
        if op == "is":
            return _codec_family_match(actual_codec, value)
        elif op == "is_not":
            return not _codec_family_match(actual_codec, value)
        return _match_op(actual_codec, op, value)

    # 5. Audio codec — check across all audio tracks
    if ctype == "audio_codec":
        raw_tracks = scan_row.get("audio_tracks_json")
        if not raw_tracks:
            return False
        try:
            tracks = json.loads(raw_tracks) if isinstance(raw_tracks, str) else raw_tracks
        except (json.JSONDecodeError, ValueError):
            return False
        if not isinstance(tracks, list):
            return False

        has_match = any(
            _audio_codec_family_match(t.get("codec", ""), value)
            for t in tracks if isinstance(t, dict)
        )

        if op in ("contains", "is"):
            return has_match
        elif op in ("does_not_contain", "is_not"):
            return not has_match
        return False

    # 6. File size — value is in GB
    if ctype == "file_size":
        file_size = scan_row.get("file_size")
        if file_size is None:
            return False
        try:
            threshold_bytes = float(value) * (1024 ** 3)
        except (ValueError, TypeError):
            return False
        if op == "greater_than":
            return file_size > threshold_bytes
        elif op == "less_than":
            return file_size < threshold_bytes
        return False

    if ctype == "date_added":
        # Operator semantics: comparison is against file AGE
        # (now − detected_at), not raw timestamps. less_than 24h means
        # "age < 24h" = "newer than 24h". The frontend labels less_than
        # as "newer than" and greater_than as "older than" to match how
        # users read time. Future maintainers seeing `less_than` here
        # should know it maps inversely to a user-facing "newer than"
        # (older file = larger age value). v0.5.1+.
        detected_at = scan_row.get("new_detected_at")
        if not detected_at:
            # NULL = file has no watcher-recorded first-seen timestamp.
            # Three populations land here: pre-watcher library rows,
            # files added via the manual scanner (which only sets
            # new_detected_at when mark_new=True; ad-hoc scans don't),
            # and files imported via paths that bypass both. Treat all
            # as ancient: older-than returns True (matches "all backlog
            # files are older than 30 days"), newer-than returns False
            # (user wants fresh arrivals — NULL = no fresh evidence).
            return op == "greater_than"
        age_hours = _parse_age_hours(value)
        if age_hours is None:
            return False  # malformed value or zero
        try:
            dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            file_age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return False
        if op == "less_than":     # "newer than N units ago"
            return file_age_hours < age_hours
        if op == "greater_than":  # "older than N units ago"
            return file_age_hours > age_hours
        return False

    # 7. Media type — detect TV vs movie
    if ctype == "media_type":
        detected = _detect_media_type(file_path)
        return _match_op(detected, op, value)

    # 8. Title — match against basename
    if ctype == "title":
        basename = os.path.basename(file_path)
        if op == "contains":
            return value.lower() in basename.lower()
        elif op == "does_not_contain":
            return value.lower() not in basename.lower()
        return _match_op(basename, op, value)

    # 9. Release group — parsed from filename
    if ctype == "release_group":
        group = _parse_release_group(file_path)
        return _match_op(group, op, value)

    # 10. Plex metadata types — label, collection, genre, library
    if ctype in ("label", "collection", "genre", "library"):
        found = any(
            mt == ctype and mv.lower() == value.lower()
            for mt, mv in folder_metadata
        )
        if op in ("is", "contains"):
            return found
        elif op in ("is_not", "does_not_contain"):
            return not found
        return found

    # 11. Sonarr/Radarr tag of the file's series / movie (looked up by
    # resolve_rules_for_batch). Never evaluated before v0.10.0.
    if ctype in ("arr_tag", "tag"):
        found = value.lower() in arr_tags
        return not found if op in ("is_not", "does_not_contain") else found

    # 12. Plex watched status
    # plex_watched / jellyfin_watched / emby_watched — all three media-server
    # syncs write into the SAME shared `watch_status` row in
    # plex_metadata_cache (`metadata_value` = "watched" | "unwatched"), so
    # the three resolver branches can share one implementation. Pre-v0.4.3
    # all three returned False unconditionally (parking-lot stubs); now
    # they read the cache. The rule-builder UI sends `value` as the string
    # "true" or "false" — "true" means "is watched". v0.4.3+.
    if ctype in ("plex_watched", "jellyfin_watched", "emby_watched"):
        want_watched = (value or "").lower() in ("true", "1", "yes", "watched")
        cache_says_watched = any(
            mt == "watch_status" and mv == "watched"
            for mt, mv in folder_metadata
        )
        cache_says_unwatched = any(
            mt == "watch_status" and mv == "unwatched"
            for mt, mv in folder_metadata
        )
        # If the cache has no watch_status row for this folder yet (sync
        # hasn't run, or folder not in any watched library), fall back to
        # False so rules don't fire spuriously on uncached folders.
        if not cache_says_watched and not cache_says_unwatched:
            return False
        is_watched = cache_says_watched
        return (is_watched == want_watched) if op == "is" else (is_watched != want_watched)

    # Jellyfin tag — matched via plex_metadata_cache (shared cache table)
    if ctype == "jellyfin_tag":
        found = any(
            mt == "jellyfin_tag" and mv.lower() == value.lower()
            for mt, mv in folder_metadata
        )
        if op in ("is", "contains"):
            return found
        elif op in ("is_not", "does_not_contain"):
            return not found
        return found

    # Emby tag — matched via plex_metadata_cache (shared cache table)
    if ctype == "emby_tag":
        found = any(
            mt == "emby_tag" and mv.lower() == value.lower()
            for mt, mv in folder_metadata
        )
        if op in ("is", "contains"):
            return found
        elif op in ("is_not", "does_not_contain"):
            return not found
        return found

    # 17. NZBGet category — passed via extra_context from add-by-path
    if ctype == "nzbget_category":
        actual = (extra_context or {}).get("nzbget_category", "")
        return _match_op(actual, op, value)

    return False


async def get_skip_prefixes() -> list[str]:
    """Return folder prefixes that match 'skip' or 'ignore' encoding rules.

    'skip' = skip entirely, 'ignore' = skip conversion (audio/sub only).
    Both should hide files from "needs conversion" in the scanner.

    Lightweight function -- loads rules + cache once, returns prefix strings.
    Only extracts directory-type conditions since non-directory conditions
    (source, codec, resolution, etc.) can't be resolved to path prefixes.
    """
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT * FROM encoding_rules WHERE enabled = 1 AND action IN ('skip', 'ignore') ORDER BY priority ASC"
        ) as cur:
            rules = [dict(r) for r in await cur.fetchall()]

        if not rules:
            return []

        prefixes: list[str] = []

        # Collect directory prefixes directly from rule conditions
        for rule in rules:
            _, conditions = _parse_rule_conditions(rule)
            for cond in conditions:
                if cond.get("type") == "directory" and cond.get("value"):
                    prefixes.append(cond["value"].rstrip("/") + "/")

        # For Plex / Jellyfin / Emby metadata rules, get cached folder paths
        plex_types = {"label", "collection", "genre", "library", "jellyfin_tag", "emby_tag"}
        has_plex_conditions = any(
            any(c.get("type") in plex_types for c in conds)
            for _, (_, conds) in ((r, _parse_rule_conditions(r)) for r in rules)
        )
        if has_plex_conditions:
            async with db.execute(
                "SELECT folder_path, metadata_type, metadata_value FROM plex_metadata_cache"
            ) as cur:
                cache_entries = await cur.fetchall()

            for rule in rules:
                _, conditions = _parse_rule_conditions(rule)
                for cond in conditions:
                    ctype = cond.get("type", "")
                    cvalue = cond.get("value", "")
                    if ctype in plex_types and cvalue:
                        for entry in cache_entries:
                            if entry["metadata_type"] == ctype and entry["metadata_value"].lower() == cvalue.lower():
                                prefixes.append(entry["folder_path"])

        return prefixes
    finally:
        await db.close()


async def resolve_rules_for_batch(file_paths: list[str], extra_context: dict | None = None) -> dict[str, Optional[dict]]:
    """For each file path, find the first matching encoding rule.

    Condition types include directory, source, resolution, video_codec,
    audio_codec, file_size, media_type, title, release_group, and
    Plex metadata (label, collection, genre, library).

    match_mode controls how multiple conditions combine:
      - "any" (default): rule matches if ANY condition matches (OR logic)
      - "all": rule matches only if ALL conditions match (AND logic)

    Returns a dict mapping file_path -> matched rule dict (or None).
    """
    if not file_paths:
        return {}

    db = await connect_db()
    try:
        # Load all enabled rules ordered by priority
        async with db.execute(
            "SELECT * FROM encoding_rules WHERE enabled = 1 ORDER BY priority ASC"
        ) as cur:
            rules = [dict(r) for r in await cur.fetchall()]

        if not rules:
            return {fp: None for fp in file_paths}

        # Pre-parse conditions for each rule
        rules_with_conds = [(rule, _parse_rule_conditions(rule)) for rule in rules]

        # Batch load scan_results for all file paths.
        # v0.5.24: chunked the IN clause. resolve_rules_for_batch is called
        # from the estimate / queue-add paths with the user's full
        # selection — 1000+ items isn't unusual on a big library. Older
        # SQLite builds cap variable count at 999, and even modern builds
        # benefit from a smaller per-query plan.
        scan_data: dict[str, dict] = {}
        if file_paths:
            CHUNK = 900
            for i in range(0, len(file_paths), CHUNK):
                chunk = file_paths[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT file_path, file_size, video_codec, video_height, video_width, audio_tracks_json, "
                    f"new_detected_at FROM scan_results WHERE file_path IN ({placeholders})",
                    chunk,
                ) as cur:
                    for row in await cur.fetchall():
                        scan_data[row["file_path"]] = dict(row)

        # Check if any rule uses cached Plex / Jellyfin / Emby metadata
        has_plex_rules = any(
            any(c.get("type") in _CACHE_CONDITION_TYPES for c in conds)
            for _, (_, conds) in rules_with_conds
        )
        arr_tags: dict[str, set[str]] = {}
        if any(c.get("type") in ("arr_tag", "tag") for _, (_, conds) in rules_with_conds for c in conds):
            from backend.arr import arr_tags_for_paths
            arr_tags = await arr_tags_for_paths(file_paths)

        # Extract unique folder paths (with trailing slash)
        folder_map: dict[str, str] = {}  # file_path -> folder_path
        unique_folders: set[str] = set()
        for fp in file_paths:
            folder = str(Path(fp).parent).rstrip("/") + "/"
            folder_map[fp] = folder
            unique_folders.add(folder)

        # Load plex_metadata_cache entries only if needed
        # All types use prefix matching: a file in /media/Show/Season 1/
        # should match a cache entry for /media/Show/ (the show-level folder from Plex)
        folder_metadata: dict[str, list[tuple[str, str]]] = {}
        if has_plex_rules and unique_folders:
            async with db.execute(
                "SELECT folder_path, metadata_type, metadata_value FROM plex_metadata_cache"
            ) as cur:
                all_cache = await cur.fetchall()

            # Build a dict of cache_folder -> [(type, value), ...]
            cache_by_folder: dict[str, list[tuple[str, str]]] = {}
            for entry in all_cache:
                cf = entry["folder_path"]
                if cf not in cache_by_folder:
                    cache_by_folder[cf] = []
                cache_by_folder[cf].append((entry["metadata_type"], entry["metadata_value"]))

            # For each unique file folder, walk up its path hierarchy to find cache matches
            for folder in unique_folders:
                parts = folder.rstrip("/").split("/")
                for depth in range(len(parts), 0, -1):
                    prefix = "/".join(parts[:depth]) + "/"
                    if prefix in cache_by_folder:
                        if folder not in folder_metadata:
                            folder_metadata[folder] = []
                        for pair in cache_by_folder[prefix]:
                            if pair not in folder_metadata[folder]:
                                folder_metadata[folder].append(pair)

        # Resolve each file
        results: dict[str, Optional[dict]] = {}
        for fp in file_paths:
            scan_row = scan_data.get(fp, {})
            folder = folder_map.get(fp, "")
            meta = folder_metadata.get(folder, [])

            matched = None
            for rule, (match_mode, conditions) in rules_with_conds:
                if not conditions:
                    continue

                cond_results = [_check_condition(c, fp, scan_row, meta, extra_context, arr_tags.get(fp, frozenset()))
                                for c in conditions]

                if match_mode == "all":
                    rule_matches = all(cond_results) and len(cond_results) > 0
                else:  # "any" (default)
                    rule_matches = any(cond_results)

                if rule_matches:
                    matched = _make_rule_result(rule)
                    break

            results[fp] = matched

        return results
    finally:
        await db.close()
