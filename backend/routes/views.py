"""Saved views (v0.10.0): named Scanner filters — pills and Advanced Search
conditions — kept on the server (they were in one browser's storage, and
Advanced Search conditions only). The Scanner's Views menu, the rules'
"Saved view" condition and the auto-queue's limit read them."""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.api_errors import ApiError
from backend.database import connect_db
from backend.scan_filters import parse_filter

router = APIRouter(prefix="/api/views")


class SaveViewRequest(BaseModel):
    name: str
    filter: str


async def get_view(view_id) -> Optional[dict]:
    try:
        view_id = int(view_id)
    except (TypeError, ValueError):
        return None
    db = await connect_db()
    try:
        async with db.execute("SELECT id, name, filter FROM saved_views WHERE id = ?", (view_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def paths_in_view(view_id, file_paths: list[str]) -> set[str]:
    """Which of `file_paths` the view lists — none if it's gone."""
    view = await get_view(view_id)
    if view is None or not file_paths:
        return set()
    from backend.routes.scan import _paths_matching
    return set(await _paths_matching(view["filter"], files=file_paths))


@router.get("")
async def list_views():
    db = await connect_db()
    try:
        async with db.execute("SELECT id, name, filter FROM saved_views ORDER BY name COLLATE NOCASE") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


@router.post("")
async def save_view(req: SaveViewRequest):
    """Save the filter under `name`, replacing a view of that name."""
    name, text = req.name.strip(), req.filter.strip()
    if not name:
        raise ApiError(status_code=400, detail="Give the view a name.", code="views.nameRequired")
    if parse_filter(text).is_all:
        raise ApiError(status_code=400, detail="Choose a filter to save first.", code="views.emptyFilter")
    db = await connect_db()
    try:
        await db.execute(
            "INSERT INTO saved_views (name, filter, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET filter = excluded.filter",
            (name, text, datetime.now(timezone.utc).isoformat()))
        await db.commit()
        async with db.execute("SELECT id, name, filter FROM saved_views WHERE name = ?", (name,)) as cur:
            return dict(await cur.fetchone())
    finally:
        await db.close()


@router.delete("/{view_id}")
async def delete_view(view_id: int):
    """Refused while the auto-queue or a rule uses the view: without it they
    would act on everything (or nothing) instead."""
    from backend.rule_resolver import _parse_rule_conditions
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'auto_queue_view'") as cur:
            row = await cur.fetchone()
        if row and str(row["value"] or "") == str(view_id):
            raise ApiError(status_code=409, detail="The auto-queue is limited to this view.",
                           code="views.inUseByAutoQueue")
        rules = []
        async with db.execute("SELECT * FROM encoding_rules") as cur:
            for rule in [dict(r) for r in await cur.fetchall()]:
                try:
                    _, conds = _parse_rule_conditions(rule)
                except (ValueError, TypeError):
                    continue
                if any(isinstance(c, dict) and c.get("type") == "saved_view" and str(c.get("value")) == str(view_id)
                       for c in conds):
                    rules.append(rule["name"])
        if rules:
            raise ApiError(status_code=409, detail=f"Rules use this view: {', '.join(rules)}.",
                           code="views.inUseByRules", params={"rules": ", ".join(rules)})
        cur = await db.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
        await db.commit()
        if cur.rowcount == 0:
            raise ApiError(status_code=404, detail="View not found", code="views.notFound")
        return {"deleted": view_id}
    finally:
        await db.close()
