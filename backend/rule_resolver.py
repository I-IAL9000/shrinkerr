"""Resolve encoding rules for files based on media directories, file properties, and Plex metadata."""

import json
import os
import re
from pathlib import Path
from typing import Optional

from backend.database import connect_db


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


def _detect_resolution(video_height: Optional[int]) -> str:
    """Map video height to resolution label."""
    if not video_height:
        return "SD"
    if video_height >= 1400:
        return "4K"
    if video_height >= 900:
        return "1080p"
    if video_height >= 600:
        return "720p"
    return "SD"


def _detect_media_type(file_path: str) -> str:
    """Detect whether a file is TV or movie based on path patterns."""
    # S##E## pattern
    if re.search(r'[Ss]\d{2}[Ee]\d{2}', file_path):
        return "tv"
    if "/Season " in file_path:
        return "tv"
    return "movie"


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


def _check_condition(cond: dict, file_path: str, scan_row: dict,
                     folder_metadata: list[tuple[str, str]],
                     extra_context: dict | None = None) -> bool:
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

    # 3. Resolution — from scan_row video_height
    if ctype == "resolution":
        detected = _detect_resolution(scan_row.get("video_height"))
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

    # 11. Tag — requires Sonarr/Radarr API calls
    # TODO: Implement tag matching via Sonarr/Radarr API. Currently used by NZBGet
    # extension which handles tag resolution separately. For batch rule resolution,
    # we skip tag conditions (return False) to avoid expensive API calls per file.
    if ctype == "tag":
        return False

    # 12. Plex watched status
    if ctype == "plex_watched":
        return False

    # 13. NZBGet category — passed via extra_context from add-by-path
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

        # For Plex-based rules, get cached folder paths
        plex_types = {"label", "collection", "genre", "library"}
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

        # Batch load scan_results for all file paths
        scan_data: dict[str, dict] = {}
        if file_paths:
            placeholders = ",".join("?" * len(file_paths))
            async with db.execute(
                f"SELECT file_path, file_size, video_codec, video_height, audio_tracks_json "
                f"FROM scan_results WHERE file_path IN ({placeholders})",
                file_paths
            ) as cur:
                for row in await cur.fetchall():
                    scan_data[row["file_path"]] = dict(row)

        # Check if any rule uses Plex metadata
        plex_types = {"label", "collection", "genre", "library"}
        has_plex_rules = any(
            any(c.get("type") in plex_types for c in conds)
            for _, (_, conds) in rules_with_conds
        )

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

                cond_results = [_check_condition(c, fp, scan_row, meta, extra_context) for c in conditions]

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
