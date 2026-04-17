"""API endpoints for the file_events log.

- GET /api/files/history?path=...    Per-file timeline (FileDetail "History" tab)
- GET /api/activity                  Global feed (Activity page)
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Query

from backend.database import connect_db


router = APIRouter(prefix="/api")


def _row_to_event(row) -> dict:
    details = None
    if row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except Exception:
            details = None
    return {
        "id": row["id"],
        "file_path": row["file_path"],
        "event_type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "summary": row["summary"],
        "details": details,
    }


@router.get("/files/history")
async def file_history(
    path: str = Query(..., description="Absolute file path"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return events for a single file path, newest first.

    Also matches the file's pre-rename path by joining via the jobs table —
    so a converted file's history shows the events from when it was the
    original .h264 too.
    """
    db = await connect_db()
    try:
        # Build the set of paths to look up: the path itself + any "original_file_path"
        # from completed jobs whose final file_path equals `path`.
        paths = {path}
        async with db.execute(
            "SELECT DISTINCT original_file_path FROM jobs "
            "WHERE file_path = ? AND original_file_path IS NOT NULL AND original_file_path <> ''",
            (path,),
        ) as cur:
            for r in await cur.fetchall():
                paths.add(r["original_file_path"])
        # And the reverse: if `path` is an original, include its converted file_path
        async with db.execute(
            "SELECT DISTINCT file_path FROM jobs "
            "WHERE original_file_path = ? AND file_path IS NOT NULL AND file_path <> ''",
            (path,),
        ) as cur:
            for r in await cur.fetchall():
                paths.add(r["file_path"])

        placeholders = ",".join("?" * len(paths))
        async with db.execute(
            f"SELECT id, file_path, event_type, occurred_at, summary, details_json "
            f"FROM file_events WHERE file_path IN ({placeholders}) "
            f"ORDER BY occurred_at DESC LIMIT ?",
            (*paths, limit),
        ) as cur:
            rows = await cur.fetchall()
        return {"events": [_row_to_event(r) for r in rows]}
    finally:
        await db.close()


@router.get("/activity")
async def activity_feed(
    event_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Substring match against file_path"),
    since: Optional[str] = Query(None, description="ISO timestamp lower bound"),
    until: Optional[str] = Query(None, description="ISO timestamp upper bound"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Global activity feed with filters."""
    where = []
    args: list = []
    if event_type and event_type != "all":
        # Support comma-separated list (e.g. event_type=completed,failed)
        types = [t.strip() for t in event_type.split(",") if t.strip()]
        if types:
            where.append(f"event_type IN ({','.join('?' * len(types))})")
            args.extend(types)
    if search:
        where.append("file_path LIKE ?")
        args.append(f"%{search}%")
    if since:
        where.append("occurred_at >= ?")
        args.append(since)
    if until:
        where.append("occurred_at <= ?")
        args.append(until)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    db = await connect_db()
    try:
        async with db.execute(f"SELECT COUNT(*) AS n FROM file_events {where_sql}", args) as cur:
            total_row = await cur.fetchone()
            total = total_row["n"] if total_row else 0

        async with db.execute(
            f"SELECT id, file_path, event_type, occurred_at, summary, details_json "
            f"FROM file_events {where_sql} ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "events": [_row_to_event(r) for r in rows],
        }
    finally:
        await db.close()
