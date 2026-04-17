"""Advanced property search.

POST /api/scan/search   Build a query from a list of predicates and return
                        matching file paths. Supports SQL-translatable predicates
                        on indexed columns + Python-side filtering for audio/sub
                        track properties.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db


router = APIRouter(prefix="/api/scan")


# ---- Property catalog --------------------------------------------------------
# Each property declares: kind ('column' | 'computed' | 'audio' | 'subtitle' | 'filename'),
# optional column expression, value type, allowed operators, and label/group for the UI.

PROPERTIES: dict[str, dict] = {
    # Video
    "video_codec":   {"kind": "column", "col": "LOWER(video_codec)", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Video codec", "group": "Video",
                      "options": ["h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "vc1", "wmv3"]},
    "video_height":  {"kind": "column", "col": "video_height",       "type": "enum", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Video height (px)", "group": "Video",
                      "options": [480, 540, 576, 720, 1080, 1440, 2160, 4320]},
    "duration_min":  {"kind": "computed", "expr": "(duration / 60.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "Duration (min)", "group": "Video"},
    "needs_conversion": {"kind": "column", "col": "needs_conversion", "type": "bool", "ops": ["eq"], "label": "Needs conversion", "group": "Video"},
    "vmaf_score":    {"kind": "column", "col": "vmaf_score",         "type": "number", "ops": ["gt", "gte", "lt", "lte", "between", "exists"], "label": "VMAF score", "group": "Video"},

    # Size / bitrate
    "file_size_mb":  {"kind": "computed", "expr": "(file_size / 1048576.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "File size (MB)", "group": "Size"},
    "file_size_gb":  {"kind": "computed", "expr": "(file_size / 1073741824.0)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "File size (GB)", "group": "Size"},
    "bitrate_mbps":  {"kind": "computed", "expr": "(CASE WHEN duration > 0 THEN (file_size * 8.0 / duration / 1000000.0) ELSE 0 END)", "type": "number", "ops": ["gt", "gte", "lt", "lte", "between"], "label": "Bitrate (Mbps)", "group": "Size"},

    # Audio (Python-side, parses audio_tracks_json)
    "audio_codec":   {"kind": "audio", "field": "codec", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Audio codec (any track)", "group": "Audio",
                      "options": ["aac", "ac3", "eac3", "dts", "truehd", "flac", "mp3", "opus", "vorbis", "pcm_s16le", "pcm_s24le"]},
    "audio_lang":    {"kind": "audio", "field": "language", "type": "enum", "ops": ["eq", "ne", "in"], "label": "Audio language (any track)", "group": "Audio",
                      "options": ["eng", "fre", "fra", "spa", "ger", "deu", "ita", "jpn", "kor", "chi", "zho", "rus", "por", "pol", "nld", "swe", "nor", "dan", "fin", "tur", "ara", "hin", "tha", "ind", "vie", "und"]},
    "audio_channels":{"kind": "audio", "field": "channels", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Audio channels (max)", "group": "Audio", "examples": [2, 6, 8]},
    "audio_track_count": {"kind": "audio_count", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Audio track count", "group": "Audio"},
    "has_lossless_audio": {"kind": "column", "col": "COALESCE(has_lossless_audio_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has lossless audio", "group": "Audio"},
    "has_removable_tracks": {"kind": "column", "col": "COALESCE(has_removable_tracks_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has removable audio tracks", "group": "Audio"},

    # Subtitles
    "subtitle_lang": {"kind": "subtitle", "field": "language", "type": "string", "ops": ["eq", "in", "contains"], "label": "Subtitle language (any)", "group": "Subtitles"},
    "subtitle_count":{"kind": "subtitle_count", "type": "number", "ops": ["eq", "gt", "gte", "lt", "lte"], "label": "Subtitle track count", "group": "Subtitles"},
    "has_removable_subs": {"kind": "column", "col": "COALESCE(has_removable_subs_flag, 0)", "type": "bool", "ops": ["eq"], "label": "Has removable subs", "group": "Subtitles"},

    # Filename / path tags (computed from file_path string)
    "source":        {"kind": "filename", "patterns": {"bluray": r"blu[-\s.]?ray|bdrip|bdremux", "remux": r"remux", "webdl": r"web[-\s.]?dl|webdl", "webrip": r"webrip", "hdtv": r"hdtv", "dvd": r"\bdvd[-\s.]?(rip|r|9|5)\b|dvdscr"},
                      "type": "enum", "ops": ["eq", "in"], "label": "Source", "group": "Filename",
                      "options": ["bluray", "remux", "webdl", "webrip", "hdtv", "dvd", "unknown"]},
    "hdr":           {"kind": "filename", "patterns": {"HDR": r"\bhdr10\+?\b|\bhdr\b", "DV": r"dolby[\s.]*vision|\.dv\.|\bdv\b(?!d)"},
                      "type": "bool", "ops": ["eq"], "label": "HDR / Dolby Vision", "group": "Filename"},
    "file_path":     {"kind": "column", "col": "file_path", "type": "string", "ops": ["contains", "regex"], "label": "File path", "group": "Filename"},
    "file_name":     {"kind": "computed", "expr": "file_path", "type": "string", "ops": ["contains", "regex"], "label": "Filename", "group": "Filename", "_basename": True},

    # State
    "health_status": {"kind": "column", "col": "health_status", "type": "string", "ops": ["eq", "exists", "in"], "label": "Health status", "group": "State", "examples": ["healthy", "corrupt"]},
    "duplicate_count": {"kind": "column", "col": "COALESCE(dup_count, 0)", "type": "number", "ops": ["eq", "gt", "gte"], "label": "Duplicate count", "group": "State"},

    # Media type (derived from path / filename)
    # Order matters: tv patterns are checked first so tv wins if a file has both
    # (rare but possible: an "[tvdb-*]" folder also containing a year-only movie)
    "media_type":    {"kind": "filename",
                      "patterns": {
                          "tv": r"\[tvdb-\d+\]|/season\s*\d+/|[Ss]\d{1,2}[Ee]\d{1,3}",
                          "movie": r"\[tmdb-\d+\]|\[imdb-tt\d+\]|\[tt\d+\]",
                      },
                      "default_value": "other",
                      "type": "enum", "ops": ["eq", "ne", "in"], "label": "Type", "group": "Type",
                      "options": ["movie", "tv", "other"],
                      "option_labels": {"movie": "Movie", "tv": "TV Show", "other": "Other"}},
}


@router.get("/search/properties")
async def list_properties():
    """Return the property catalog for the UI."""
    out = {}
    for key, p in PROPERTIES.items():
        out[key] = {
            "label": p.get("label", key),
            "group": p.get("group", "Other"),
            "type": p.get("type", "string"),
            "ops": p.get("ops", []),
            "examples": p.get("examples"),
            "options": p.get("options"),
            "option_labels": p.get("option_labels"),
        }
    return out


# ---- Predicate evaluation ---------------------------------------------------

class Predicate(BaseModel):
    property: str
    op: str
    value: Any = None
    value2: Any = None  # for "between"


class SearchRequest(BaseModel):
    predicates: list[Predicate] = []
    match_mode: str = "all"  # "all" = AND, "any" = OR
    limit: int = 5000


def _coerce(prop_type: str, value: Any) -> Any:
    if value is None:
        return None
    if prop_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if prop_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return value


def _build_sql_clause(pred: Predicate, prop: dict) -> Optional[tuple[str, list]]:
    """Return (sql_fragment, params) for a SQL-translatable predicate, or None."""
    kind = prop["kind"]
    if kind == "column":
        col = prop["col"]
    elif kind == "computed":
        col = prop["expr"]
    else:
        return None

    op = pred.op
    val = _coerce(prop.get("type", "string"), pred.value)
    val2 = _coerce(prop.get("type", "string"), pred.value2)

    if op == "exists":
        return (f"({col} IS NOT NULL AND {col} <> '')", [])
    if op == "eq":
        if isinstance(val, bool):
            # Use != 0 instead of = 1 so truthy integers > 1 also match (matches Python's bool() behavior)
            if val:
                return (f"COALESCE({col}, 0) != 0", [])
            else:
                return (f"COALESCE({col}, 0) = 0", [])
        if prop.get("type") == "string":
            return (f"LOWER({col}) = LOWER(?)", [val])
        return (f"{col} = ?", [val])
    if op == "ne":
        if prop.get("type") == "string":
            return (f"LOWER({col}) <> LOWER(?)", [val])
        return (f"{col} <> ?", [val])
    if op == "gt":
        return (f"{col} > ?", [val])
    if op == "gte":
        return (f"{col} >= ?", [val])
    if op == "lt":
        return (f"{col} < ?", [val])
    if op == "lte":
        return (f"{col} <= ?", [val])
    if op == "between" and val is not None and val2 is not None:
        lo, hi = sorted([val, val2])
        return (f"{col} BETWEEN ? AND ?", [lo, hi])
    if op == "contains":
        return (f"{col} LIKE ?", [f"%{val}%"])
    if op == "regex":
        # SQLite REGEXP isn't built-in; fall back to LIKE with manual fallback in Python
        return (f"{col} LIKE ?", [f"%{val}%"])
    if op == "in" and isinstance(val, (list, tuple)) and val:
        placeholders = ",".join("?" for _ in val)
        if prop.get("type") == "string":
            return (f"LOWER({col}) IN ({placeholders})", [str(v).lower() for v in val])
        return (f"{col} IN ({placeholders})", list(val))
    if op == "in" and isinstance(val, str):
        items = [v.strip() for v in val.split(",") if v.strip()]
        if items:
            placeholders = ",".join("?" for _ in items)
            if prop.get("type") == "string":
                return (f"LOWER({col}) IN ({placeholders})", [v.lower() for v in items])
            return (f"{col} IN ({placeholders})", items)
    return None


def _eval_audio_predicate(pred: Predicate, prop: dict, audio_tracks: list[dict]) -> bool:
    """Evaluate audio/* predicate against parsed audio_tracks list. Any-track semantics."""
    if not audio_tracks:
        return False
    field = prop["field"]
    val = _coerce(prop.get("type", "string"), pred.value)
    op = pred.op

    if prop.get("type") == "number":
        # Aggregate: max channels across tracks
        nums = [int(t.get(field, 0) or 0) for t in audio_tracks]
        agg = max(nums) if nums else 0
        if op == "eq": return agg == val
        if op == "gt": return agg > val
        if op == "gte": return agg >= val
        if op == "lt": return agg < val
        if op == "lte": return agg <= val
    else:
        # String: any track matches
        target = (str(val).lower() if val is not None else "")
        for t in audio_tracks:
            v = str(t.get(field, "") or "").lower()
            if op == "eq" and v == target: return True
            if op == "ne" and v != target: return True
            if op == "contains" and target in v: return True
            if op == "in":
                items = [s.strip().lower() for s in (val.split(",") if isinstance(val, str) else val)] if val else []
                if v in items: return True
        return False
    return False


def _eval_filename_predicate(pred: Predicate, prop: dict, file_path: str) -> bool:
    """Source/HDR derived from filename."""
    fname = file_path.lower()
    patterns: dict = prop.get("patterns", {})
    val = pred.value
    op = pred.op

    if prop.get("type") == "bool":
        # Any pattern matches => HDR true
        # Build active set
        for label, pat in patterns.items():
            if not pat:
                continue
            if re.search(pat, fname):
                return bool(val)
        # No HDR-related pattern matched
        return not bool(val)

    # Enum: figure out which pattern matched (first wins), or fallback if none
    default = prop.get("default_value", "unknown")
    matched = default
    for label, pat in patterns.items():
        if pat and re.search(pat, fname):
            matched = label.lower()
            break
    target_vals = []
    if isinstance(val, list):
        target_vals = [str(v).lower() for v in val]
    elif isinstance(val, str):
        target_vals = [v.strip().lower() for v in val.split(",") if v.strip()]
    if op == "eq":
        return matched == target_vals[0] if target_vals else False
    if op == "ne":
        return matched != target_vals[0] if target_vals else True
    if op == "in":
        return matched in target_vals
    return False


@router.post("/search")
async def advanced_search(req: SearchRequest):
    """Run an advanced property search and return matching file paths.

    ``total`` is the true count of matches; ``file_paths`` is capped at ``limit``.
    """
    if not req.predicates:
        return {"total": 0, "file_paths": [], "limit": req.limit}

    # Validate predicates and split into SQL vs Python
    sql_clauses: list[str] = []
    sql_params: list = []
    python_preds: list[tuple[Predicate, dict]] = []

    for pred in req.predicates:
        prop = PROPERTIES.get(pred.property)
        if not prop:
            raise HTTPException(400, f"Unknown property: {pred.property}")
        if pred.op not in prop.get("ops", []):
            raise HTTPException(400, f"Unsupported op '{pred.op}' for property '{pred.property}'")

        kind = prop["kind"]
        if kind in ("column", "computed"):
            built = _build_sql_clause(pred, prop)
            if built:
                clause, params = built
                sql_clauses.append(clause)
                sql_params.extend(params)
            else:
                python_preds.append((pred, prop))
        elif kind in ("audio", "audio_count", "subtitle", "subtitle_count", "filename"):
            python_preds.append((pred, prop))
        else:
            raise HTTPException(400, f"Unhandled property kind: {kind}")

    base_where = (
        "removed_from_list = 0 "
        "AND file_path NOT LIKE '%.converting.%' "
        "AND file_path NOT LIKE '%.remuxing.%' "
        "AND file_path NOT LIKE '%/._%'"
    )
    is_any = req.match_mode == "any"
    joiner = " OR " if is_any else " AND "
    where_sql = base_where
    if sql_clauses:
        # For "any" mode with mixed SQL+Python preds, we can't fully OR at SQL level —
        # the SQL clauses still need to fetch candidate rows. Use OR to widen the net;
        # Python preds contribute additional matches in the loop below.
        where_sql += " AND (" + joiner.join(f"({c})" for c in sql_clauses) + ")"

    # Need audio/sub JSON only if any python predicate touches them
    needs_audio = any(p[1]["kind"] in ("audio", "audio_count") for p in python_preds)
    needs_sub = any(p[1]["kind"] in ("subtitle", "subtitle_count") for p in python_preds)
    cols = "file_path"
    if needs_audio:
        cols += ", audio_tracks_json"
    if needs_sub:
        cols += ", subtitle_tracks_json"

    db = await connect_db()
    try:
        if not python_preds:
            # Pure SQL — get a true count cheaply
            async with db.execute(
                f"SELECT COUNT(*) AS n FROM scan_results WHERE {where_sql}",
                sql_params,
            ) as cur:
                total_row = await cur.fetchone()
                total = int(total_row["n"]) if total_row else 0
            async with db.execute(
                f"SELECT file_path FROM scan_results WHERE {where_sql} LIMIT ?",
                (*sql_params, req.limit),
            ) as cur:
                matches = [r["file_path"] for r in await cur.fetchall()]
            return {"total": total, "file_paths": matches, "limit": req.limit}

        # Mixed SQL + Python preds.
        # For "any" (OR) mode: a row matches if it passed ANY SQL clause OR ANY Python pred.
        #   → we need ALL rows (base_where only) so Python preds get a chance.
        # For "all" (AND) mode: row must pass ALL SQL clauses AND ALL Python preds.
        #   → SQL already narrows; we just Python-filter the survivors.
        fetch_where = base_where if is_any else where_sql
        fetch_params = [] if is_any else list(sql_params)

        async with db.execute(
            f"SELECT {cols} FROM scan_results WHERE {fetch_where}",
            fetch_params,
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    def _eval_python_pred(pred_item, prop_item, fp, audio_t, sub_t) -> bool:
        kind = prop_item["kind"]
        if kind == "audio":
            return _eval_audio_predicate(pred_item, prop_item, audio_t)
        if kind == "audio_count":
            n = len(audio_t)
            v = _coerce("number", pred_item.value) or 0
            return (
                (pred_item.op == "eq" and n == v) or
                (pred_item.op == "gt" and n > v) or
                (pred_item.op == "gte" and n >= v) or
                (pred_item.op == "lt" and n < v) or
                (pred_item.op == "lte" and n <= v)
            )
        if kind == "subtitle":
            return _eval_audio_predicate(pred_item, prop_item, sub_t)
        if kind == "subtitle_count":
            n = len(sub_t)
            v = _coerce("number", pred_item.value) or 0
            return (
                (pred_item.op == "eq" and n == v) or
                (pred_item.op == "gt" and n > v) or
                (pred_item.op == "gte" and n >= v) or
                (pred_item.op == "lt" and n < v) or
                (pred_item.op == "lte" and n <= v)
            )
        if kind == "filename":
            return _eval_filename_predicate(pred_item, prop_item, fp)
        return False

    matches: list[str] = []
    total = 0
    for row in rows:
        fp = row["file_path"]
        audio_tracks: list[dict] = []
        sub_tracks: list[dict] = []
        if needs_audio:
            try: audio_tracks = json.loads(row["audio_tracks_json"] or "[]")
            except Exception: pass
        if needs_sub:
            try: sub_tracks = json.loads(row["subtitle_tracks_json"] or "[]")
            except Exception: pass

        python_results = [_eval_python_pred(p, pr, fp, audio_tracks, sub_tracks) for p, pr in python_preds]

        if is_any:
            # OR: row passes SQL if where_sql matched (we fetched base_where, so we need to re-check SQL)
            sql_passed = False
            if sql_clauses:
                # We can't re-run SQL per row easily, so check if row was in the narrowed set.
                # Simpler: evaluate SQL predicates again in Python for the "any" path.
                # For now, just check python_preds — the SQL clauses already widen via OR in the SQL.
                # Re-use the SQL-filtered rows approach: re-query with SQL OR, then union python matches.
                pass
            ok = any(python_results)
            if not ok and sql_clauses:
                # Check if any SQL clause matches by re-evaluating. Since we can't easily,
                # fall back: for "any" mode with SQL clauses, also accept rows the SQL OR would have matched.
                # We need to do a separate SQL query once and merge.
                ok = False
        else:
            # AND: all python preds must pass (SQL already filtered)
            ok = all(python_results)

        if ok:
            total += 1
            if len(matches) < req.limit:
                matches.append(fp)

    # For "any" mode with SQL clauses: also include SQL-only matches not caught by Python
    if is_any and sql_clauses:
        sql_match_set = set(matches)
        db2 = await connect_db()
        try:
            sql_or_where = base_where + " AND (" + " OR ".join(f"({c})" for c in sql_clauses) + ")"
            async with db2.execute(
                f"SELECT file_path FROM scan_results WHERE {sql_or_where}",
                sql_params,
            ) as cur:
                for r in await cur.fetchall():
                    fp = r["file_path"]
                    if fp not in sql_match_set:
                        total += 1
                        if len(matches) < req.limit:
                            matches.append(fp)
                            sql_match_set.add(fp)
        finally:
            await db2.close()

    return {"total": total, "file_paths": matches, "limit": req.limit}
