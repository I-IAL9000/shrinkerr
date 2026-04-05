import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db

router = APIRouter(prefix="/api/rules")


class MatchCondition(BaseModel):
    type: str   # 'directory', 'label', 'collection', 'genre', 'library'
    value: str


class RuleCreate(BaseModel):
    name: str
    match_conditions: list[MatchCondition]
    action: str = "encode"  # 'encode', 'ignore', or 'skip'
    enabled: bool = True
    encoder: Optional[str] = None
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
    target_resolution: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    queue_priority: Optional[int] = None


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    match_conditions: Optional[list[MatchCondition]] = None
    action: Optional[str] = None
    enabled: Optional[bool] = None
    encoder: Optional[str] = None
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
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
                    d = dict(r)
                    d["match_conditions"] = _parse_conditions(d)
                    result.append(d)
                except Exception as exc:
                    print(f"[RULES] Failed to serialize rule: {exc}", flush=True)
                    # Return raw dict as fallback
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
    for cond in payload.match_conditions:
        if cond.type not in ("directory", "label", "collection", "genre", "library"):
            raise HTTPException(400, f"Invalid match type: {cond.type}")
    if payload.action not in ("encode", "ignore", "skip"):
        raise HTTPException(400, "action must be encode, ignore, or skip")

    db = await connect_db()
    try:
        async with db.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM encoding_rules") as cur:
            row = await cur.fetchone()
            next_priority = row[0]

        conditions_json = json.dumps([c.dict() for c in payload.match_conditions])
        # Store first condition in legacy columns for backward compat
        first = payload.match_conditions[0]
        now = datetime.now(timezone.utc).isoformat()
        async with db.execute(
            """INSERT INTO encoding_rules
               (name, match_type, match_value, match_conditions, priority, action, enabled,
                encoder, nvenc_preset, nvenc_cq, libx265_crf, target_resolution,
                audio_codec, audio_bitrate, queue_priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payload.name, first.type, first.value, conditions_json, next_priority,
             payload.action, int(payload.enabled), payload.encoder, payload.nvenc_preset,
             payload.nvenc_cq, payload.libx265_crf, payload.target_resolution,
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
            elif key == "match_conditions":
                conditions = val
                if conditions is not None:
                    conditions_json = json.dumps([c if isinstance(c, dict) else c.dict() for c in conditions])
                    updates.append("match_conditions = ?")
                    values.append(conditions_json)
                    # Keep legacy columns in sync
                    if conditions:
                        first = conditions[0] if isinstance(conditions[0], dict) else conditions[0].dict()
                        updates.append("match_type = ?")
                        values.append(first["type"])
                        updates.append("match_value = ?")
                        values.append(first["value"])
            else:
                updates.append(f"{key} = ?")
                values.append(val)

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


@router.post("/test")
async def test_rule(payload: dict):
    file_path = payload.get("file_path")
    if not file_path:
        raise HTTPException(400, "file_path required")
    from backend.rule_resolver import resolve_rules_for_batch
    results = await resolve_rules_for_batch([file_path])
    return {"file_path": file_path, "matched_rule": results.get(file_path)}
