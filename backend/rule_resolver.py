"""Resolve encoding rules for files based on media directories and Plex metadata."""

import json
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
        "target_resolution": rule.get("target_resolution"),
        "audio_codec": rule.get("audio_codec"),
        "audio_bitrate": rule.get("audio_bitrate"),
        "queue_priority": rule.get("queue_priority"),
    }


def _get_conditions(rule: dict) -> list[dict]:
    """Parse match_conditions JSON, falling back to legacy match_type/match_value."""
    if rule.get("match_conditions"):
        try:
            raw = rule["match_conditions"]
            conds = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(conds, list) and conds:
                return conds
        except (json.JSONDecodeError, ValueError):
            pass
    if rule.get("match_type") and rule.get("match_value"):
        return [{"type": rule["match_type"], "value": rule["match_value"]}]
    return []


async def get_skip_prefixes() -> list[str]:
    """Return folder prefixes that match 'skip' or 'ignore' encoding rules.

    'skip' = skip entirely, 'ignore' = skip conversion (audio/sub only).
    Both should hide files from "needs conversion" in the scanner.

    Lightweight function — loads rules + cache once, returns prefix strings.
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
            for cond in _get_conditions(rule):
                if cond["type"] == "directory":
                    prefixes.append(cond["value"].rstrip("/") + "/")

        # For Plex-based rules, get cached folder paths
        has_plex_conditions = any(
            any(c["type"] in ("label", "collection", "genre", "library") for c in _get_conditions(r))
            for r in rules
        )
        if has_plex_conditions:
            async with db.execute(
                "SELECT folder_path, metadata_type, metadata_value FROM plex_metadata_cache"
            ) as cur:
                cache_entries = await cur.fetchall()

            for rule in rules:
                for cond in _get_conditions(rule):
                    if cond["type"] in ("label", "collection", "genre", "library"):
                        for entry in cache_entries:
                            if entry["metadata_type"] == cond["type"] and entry["metadata_value"] == cond["value"]:
                                prefixes.append(entry["folder_path"])

        # Only log once on first call (avoid spamming on every poll)
        pass
        return prefixes
    finally:
        await db.close()


async def resolve_rules_for_batch(file_paths: list[str]) -> dict[str, Optional[dict]]:
    """For each file path, find the first matching encoding rule.

    Match types:
      - directory: file path starts with the condition's value (path prefix)
      - label/collection: exact match in plex_metadata_cache for the file's folder
      - library: prefix match against cached library paths

    A rule matches if ANY of its conditions match (OR logic).
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
        rules_with_conds = [(rule, _get_conditions(rule)) for rule in rules]

        # Check if any rule uses Plex metadata
        has_plex_rules = any(
            any(c["type"] in ("label", "collection", "genre", "library") for c in conds)
            for _, conds in rules_with_conds
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
            # Load all cache entries and group by folder_path
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
            # e.g., /media/TV/Show/Season 1/ checks: itself, /media/TV/Show/, /media/TV/, /media/
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
            folder = folder_map[fp]
            meta = folder_metadata.get(folder, [])
            matched = None

            for rule, conditions in rules_with_conds:
                if not conditions:
                    continue
                # Rule matches if ANY condition matches
                rule_matches = False
                for cond in conditions:
                    ctype = cond["type"]
                    cvalue = cond["value"]
                    if ctype == "directory":
                        dir_prefix = cvalue.rstrip("/") + "/"
                        if fp.startswith(dir_prefix) or folder.startswith(dir_prefix):
                            rule_matches = True
                            break
                    else:
                        if any(mt == ctype and mv == cvalue for mt, mv in meta):
                            rule_matches = True
                            break

                if rule_matches:
                    matched = _make_rule_result(rule)
                    break

            results[fp] = matched

        return results
    finally:
        await db.close()
