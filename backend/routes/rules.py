import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db

router = APIRouter(prefix="/api/rules")


class MatchCondition(BaseModel):
    type: str   # 'directory', 'label', 'collection', 'genre', 'library', 'source', 'resolution', etc.
    operator: str = "is"  # "is", "is_not", "contains", "does_not_contain", "greater_than", "less_than"
    value: str


class RuleCreate(BaseModel):
    name: str
    match_mode: str = "any"  # "all" (AND) or "any" (OR)
    match_conditions: list[MatchCondition]
    action: str = "encode"  # 'encode', 'ignore', or 'skip'
    enabled: bool = True
    encoder: Optional[str] = None
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
    libx265_preset: Optional[str] = None
    target_resolution: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    queue_priority: Optional[int] = None


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    match_mode: Optional[str] = None
    match_conditions: Optional[list[MatchCondition]] = None
    action: Optional[str] = None
    enabled: Optional[bool] = None
    encoder: Optional[str] = None
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
    libx265_preset: Optional[str] = None
    target_resolution: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    queue_priority: Optional[int] = None


class ReorderRequest(BaseModel):
    rule_ids: list[int]


def _parse_conditions(rule: dict) -> list[dict]:
    """Parse match_conditions JSON, falling back to legacy match_type/match_value."""
    if rule.get("match_conditions"):
        try:
            conds = json.loads(rule["match_conditions"]) if isinstance(rule["match_conditions"], str) else rule["match_conditions"]
            if isinstance(conds, list) and conds:
                return conds
        except (json.JSONDecodeError, ValueError):
            pass
    # Legacy fallback
    if rule.get("match_type") and rule.get("match_value"):
        return [{"type": rule["match_type"], "value": rule["match_value"]}]
    return []


def _serialize_rule(rule: dict) -> dict:
    """Serialize a rule row for the API, ensuring match_conditions is always a list."""
    d = dict(rule)
    d["match_conditions"] = _parse_conditions(rule)
    return d


@router.get("/")
async def list_rules():
    db = await connect_db()
    try:
        async with db.execute("SELECT * FROM encoding_rules ORDER BY priority ASC") as cur:
            rows = await cur.fetchall()
            print(f"[RULES] Raw row count: {len(rows)}", flush=True)
            result = []
            for r in rows:
                try:
                    rule = dict(r)
                    # Parse stored conditions — handle both old and new format
                    raw = rule.get("match_conditions")
                    if raw:
                        parsed = json.loads(raw) if isinstance(raw, str) else raw
                        if isinstance(parsed, dict) and "conditions" in parsed:
                            # New format: {match_mode, conditions}
                            rule["match_mode"] = parsed.get("match_mode", "any")
                            rule["conditions"] = parsed.get("conditions", [])
                        elif isinstance(parsed, list):
                            # Old format — list of conditions, convert
                            rule["match_mode"] = "any"
                            rule["conditions"] = parsed
                        else:
                            rule["match_mode"] = "any"
                            rule["conditions"] = []
                    else:
                        # Legacy fallback from match_type/match_value columns
                        if rule.get("match_type") and rule.get("match_value"):
                            rule["match_mode"] = "any"
                            rule["conditions"] = [{"type": rule["match_type"], "operator": "is", "value": rule["match_value"]}]
                        else:
                            rule["match_mode"] = "any"
                            rule["conditions"] = []
                    # Also keep match_conditions as the conditions list for backward compat
                    rule["match_conditions"] = rule["conditions"]
                    result.append(rule)
                except Exception as exc:
                    print(f"[RULES] Failed to serialize rule: {exc}", flush=True)
                    try:
                        result.append(dict(r))
                    except Exception:
                        pass
            print(f"[RULES] Returning {len(result)} rules", flush=True)
            return result
    finally:
        await db.close()


@router.post("/")
async def create_rule(payload: RuleCreate):
    if not payload.match_conditions:
        raise HTTPException(400, "At least one match condition required")
    valid_types = (
        "directory", "label", "collection", "genre", "library",
        "source", "resolution", "video_codec", "audio_codec",
        "media_type", "release_group", "arr_tag",
    )
    for cond in payload.match_conditions:
        if cond.type not in valid_types:
            raise HTTPException(400, f"Invalid match type: {cond.type}")
    if payload.action not in ("encode", "ignore", "skip"):
        raise HTTPException(400, "action must be encode, ignore, or skip")

    db = await connect_db()
    try:
        async with db.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM encoding_rules") as cur:
            row = await cur.fetchone()
            next_priority = row[0]

        conditions_json = json.dumps({
            "match_mode": payload.match_mode,
            "conditions": [c.model_dump() for c in payload.match_conditions]
        })
        # Store first condition in legacy columns for backward compat
        first = payload.match_conditions[0]
        now = datetime.now(timezone.utc).isoformat()
        async with db.execute(
            """INSERT INTO encoding_rules
               (name, match_type, match_value, match_conditions, priority, action, enabled,
                encoder, nvenc_preset, nvenc_cq, libx265_crf, libx265_preset, target_resolution,
                audio_codec, audio_bitrate, queue_priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload.name, first.type, first.value, conditions_json, next_priority,
             payload.action, int(payload.enabled), payload.encoder, payload.nvenc_preset,
             payload.nvenc_cq, payload.libx265_crf, payload.libx265_preset, payload.target_resolution,
             payload.audio_codec, payload.audio_bitrate, payload.queue_priority, now),
        ) as cur:
            rule_id = cur.lastrowid
        await db.commit()
        # Verify the row was persisted
        async with db.execute("SELECT COUNT(*) FROM encoding_rules") as cur:
            count = (await cur.fetchone())[0]
        print(f"[RULES] Created rule {rule_id}, total rules in DB: {count}", flush=True)
        return {"id": rule_id, "priority": next_priority}
    finally:
        await db.close()


@router.put("/reorder")
async def reorder_rules(payload: ReorderRequest):
    db = await connect_db()
    try:
        for idx, rule_id in enumerate(payload.rule_ids):
            await db.execute(
                "UPDATE encoding_rules SET priority = ? WHERE id = ?",
                (idx, rule_id),
            )
        await db.commit()
        return {"status": "reordered"}
    finally:
        await db.close()


@router.put("/{rule_id}")
async def update_rule(rule_id: int, payload: RuleUpdate):
    db = await connect_db()
    try:
        updates = []
        values = []
        data = payload.dict(exclude_unset=True)
        for key, val in data.items():
            if key == "enabled":
                updates.append("enabled = ?")
                values.append(int(val))
            elif key == "match_mode":
                # Handled together with match_conditions below
                pass
            elif key == "match_conditions":
                conditions = val
                if conditions is not None:
                    # Determine match_mode: use from this update payload, or fetch existing
                    match_mode = data.get("match_mode")
                    if match_mode is None:
                        # Fetch existing match_mode from stored data
                        async with db.execute(
                            "SELECT match_conditions FROM encoding_rules WHERE id = ?", (rule_id,)
                        ) as cur2:
                            existing = await cur2.fetchone()
                            if existing and existing["match_conditions"]:
                                try:
                                    stored = json.loads(existing["match_conditions"])
                                    if isinstance(stored, dict):
                                        match_mode = stored.get("match_mode", "any")
                                    else:
                                        match_mode = "any"
                                except (json.JSONDecodeError, ValueError):
                                    match_mode = "any"
                            else:
                                match_mode = "any"
                    conditions_list = [c if isinstance(c, dict) else c.model_dump() for c in conditions]
                    conditions_json = json.dumps({
                        "match_mode": match_mode,
                        "conditions": conditions_list
                    })
                    updates.append("match_conditions = ?")
                    values.append(conditions_json)
                    # Keep legacy columns in sync
                    if conditions_list:
                        first = conditions_list[0]
                        updates.append("match_type = ?")
                        values.append(first["type"])
                        updates.append("match_value = ?")
                        values.append(first["value"])
            else:
                updates.append(f"{key} = ?")
                values.append(val)

        # Handle match_mode-only update (no match_conditions provided)
        if "match_mode" in data and "match_conditions" not in data:
            new_mode = data["match_mode"]
            async with db.execute(
                "SELECT match_conditions FROM encoding_rules WHERE id = ?", (rule_id,)
            ) as cur2:
                existing = await cur2.fetchone()
                if existing and existing["match_conditions"]:
                    try:
                        stored = json.loads(existing["match_conditions"])
                        if isinstance(stored, dict) and "conditions" in stored:
                            stored["match_mode"] = new_mode
                        elif isinstance(stored, list):
                            stored = {"match_mode": new_mode, "conditions": stored}
                        else:
                            stored = {"match_mode": new_mode, "conditions": []}
                    except (json.JSONDecodeError, ValueError):
                        stored = {"match_mode": new_mode, "conditions": []}
                else:
                    stored = {"match_mode": new_mode, "conditions": []}
                updates.append("match_conditions = ?")
                values.append(json.dumps(stored))

        if not updates:
            raise HTTPException(400, "No fields to update")

        values.append(rule_id)
        await db.execute(
            f"UPDATE encoding_rules SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        await db.commit()
        return {"status": "updated"}
    finally:
        await db.close()


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int):
    db = await connect_db()
    try:
        await db.execute("DELETE FROM encoding_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return {"status": "deleted"}
    finally:
        await db.close()


@router.post("/sync-plex")
async def sync_plex():
    from backend.plex import sync_plex_metadata_cache
    try:
        result = await sync_plex_metadata_cache()
        return {"status": "synced", **result}
    except Exception as exc:
        raise HTTPException(500, f"Plex sync failed: {exc}")


@router.get("/plex-options")
async def plex_options():
    from backend.plex import get_available_plex_options
    try:
        return await get_available_plex_options()
    except Exception as exc:
        raise HTTPException(500, f"Failed to fetch Plex options: {exc}")


@router.get("/condition-options")
async def get_condition_options():
    """Return available values for each condition type, populated from scan data."""
    db = await connect_db()
    try:
        sources = set()
        release_groups: dict[str, int] = {}
        video_codecs = set()
        audio_codecs = set()

        async with db.execute(
            "SELECT file_path, video_codec, audio_tracks_json FROM scan_results WHERE removed_from_list = 0 LIMIT 50000"
        ) as cur:
            for row in await cur.fetchall():
                fp = row["file_path"]
                fname = os.path.basename(fp).lower()

                # Detect source from filename
                if "remux" in fname:
                    sources.add("Remux")
                elif "web-dl" in fname or "webdl" in fname:
                    sources.add("WEB-DL")
                elif "webrip" in fname:
                    sources.add("WEBRip")
                elif "bluray" in fname or "blu-ray" in fname or "bdrip" in fname:
                    sources.add("Blu-ray")
                elif "hdtv" in fname:
                    sources.add("HDTV")
                elif "dvdrip" in fname or "dvd" in fname:
                    sources.add("DVD")

                # Video codec
                vc = (row["video_codec"] or "").lower()
                if vc:
                    video_codecs.add(vc)

                # Audio codecs from tracks
                try:
                    tracks = json.loads(row["audio_tracks_json"] or "[]")
                    for t in tracks:
                        ac = (t.get("codec") or "").lower()
                        if ac:
                            audio_codecs.add(ac)
                except Exception:
                    pass

                # Release group — last hyphen-separated token before extension
                name = os.path.splitext(os.path.basename(fp))[0]
                m = re.search(r'-([A-Za-z0-9]+)$', name)
                if m:
                    g = m.group(1)
                    release_groups[g] = release_groups.get(g, 0) + 1

        # Sonarr/Radarr tags
        arr_tags = []
        try:
            settings = {}
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('sonarr_url','sonarr_api_key','radarr_url','radarr_api_key')"
            ) as cur:
                for row in await cur.fetchall():
                    settings[row["key"]] = row["value"]

            import httpx

            if settings.get("sonarr_url") and settings.get("sonarr_api_key"):
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{settings['sonarr_url'].rstrip('/')}/api/v3/tag",
                        headers={"X-Api-Key": settings["sonarr_api_key"]}
                    )
                    if resp.status_code == 200:
                        for t in resp.json():
                            arr_tags.append({"label": t["label"], "source": "sonarr"})

            if settings.get("radarr_url") and settings.get("radarr_api_key"):
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{settings['radarr_url'].rstrip('/')}/api/v3/tag",
                        headers={"X-Api-Key": settings["radarr_api_key"]}
                    )
                    if resp.status_code == 200:
                        for t in resp.json():
                            if not any(existing["label"] == t["label"] for existing in arr_tags):
                                arr_tags.append({"label": t["label"], "source": "radarr"})
        except Exception:
            pass

        # NZBGet categories from settings
        nzbget_categories = []
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'nzbget_categories'") as cur:
                row = await cur.fetchone()
                if row and row["value"]:
                    nzbget_categories = json.loads(row["value"])
        except Exception:
            pass

        return {
            "sources": sorted(sources),
            "resolutions": ["4K", "1080p", "720p", "SD"],
            "video_codecs": sorted(video_codecs),
            "audio_codecs": sorted(audio_codecs),
            "media_types": ["movie", "tv"],
            "release_groups": [g for g, _ in sorted(release_groups.items(), key=lambda x: -x[1])][:200],
            "arr_tags": arr_tags,
            "nzbget_categories": nzbget_categories,
        }
    finally:
        await db.close()


@router.post("/test")
async def test_rule(payload: dict):
    file_path = payload.get("file_path")
    if not file_path:
        raise HTTPException(400, "file_path required")
    from backend.rule_resolver import resolve_rules_for_batch
    results = await resolve_rules_for_batch([file_path])
    return {"file_path": file_path, "matched_rule": results.get(file_path)}
