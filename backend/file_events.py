"""Per-file event log.

Records key decisions and outcomes for each media file:
    scanned, queued, started, completed, failed, skipped, ignored,
    unignored, health_check, reverted, rescanned

Used by:
- Per-file "History" tab in the FileDetail panel
- Global /activity page

Use ``log_event`` from anywhere — it's fire-and-forget and never raises.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from backend.database import DB_PATH


# Canonical event types — single source of truth for filters/UI
EVENT_SCANNED = "scanned"
EVENT_RESCANNED = "rescanned"
EVENT_QUEUED = "queued"
EVENT_STARTED = "started"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"
EVENT_SKIPPED = "skipped"        # skipped by rule (no work done)
EVENT_IGNORED = "ignored"        # user manually ignored
EVENT_UNIGNORED = "unignored"
EVENT_HEALTH_CHECK = "health_check"
EVENT_VMAF = "vmaf"
EVENT_REVERTED = "reverted"

EVENT_TYPES = (
    EVENT_SCANNED, EVENT_RESCANNED, EVENT_QUEUED, EVENT_STARTED,
    EVENT_COMPLETED, EVENT_FAILED, EVENT_SKIPPED, EVENT_IGNORED,
    EVENT_UNIGNORED, EVENT_HEALTH_CHECK, EVENT_VMAF, EVENT_REVERTED,
)


async def log_event(
    file_path: str,
    event_type: str,
    summary: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Append a row to file_events. Never raises — failures are logged & swallowed."""
    try:
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=10000")
            await db.execute(
                "INSERT INTO file_events (file_path, event_type, occurred_at, summary, details_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    file_path,
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    summary,
                    json.dumps(details) if details else None,
                ),
            )
            await db.commit()
        finally:
            await db.close()
    except Exception as exc:
        # Never let event logging break the caller
        print(f"[FILE_EVENTS] Failed to log {event_type} for {file_path}: {exc}", flush=True)


async def backfill_from_jobs(max_jobs: int = 5000) -> int:
    """One-time backfill: walk completed/failed jobs and synthesize events.

    Bounded by ``max_jobs`` so a runaway jobs table can't lock the DB. Skips
    health_check jobs entirely — they're noise in the timeline. Idempotent via
    a single sentinel row.
    """
    inserted = 0
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row

        # Skip if we've already backfilled (sentinel row check)
        async with db.execute(
            "SELECT 1 FROM file_events WHERE event_type = 'scanned' "
            "AND summary = '[backfill] sentinel' LIMIT 1"
        ) as cur:
            if await cur.fetchone():
                return 0

        # Most-recent first so users see useful history if we hit the cap
        async with db.execute(
            "SELECT id, file_path, job_type, status, encoder, "
            "space_saved, original_size, error_log, completed_at, started_at, created_at, "
            "health_status "
            "FROM jobs WHERE status IN ('completed', 'failed') "
            "AND job_type <> 'health_check' "
            "ORDER BY id DESC LIMIT ?",
            (max_jobs,),
        ) as cur:
            jobs = await cur.fetchall()

        # Write the sentinel first so partial completion still counts
        from datetime import datetime, timezone
        await db.execute(
            "INSERT INTO file_events (file_path, event_type, occurred_at, summary, details_json) "
            "VALUES ('', 'scanned', ?, '[backfill] sentinel', NULL)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()

        # Insert in chunks of 500 with a commit in between to keep WAL small
        CHUNK = 500
        for i, j in enumerate(jobs):
            occurred = j["completed_at"] or j["started_at"] or j["created_at"]
            if not occurred:
                continue
            status = j["status"]
            jt = j["job_type"]
            fp = j["file_path"]
            if status == "completed":
                saved = j["space_saved"] or 0
                if saved > 0:
                    gb = saved / (1024 ** 3)
                    pct = (saved / j["original_size"] * 100) if j["original_size"] else 0
                    summary = f"[backfill] Converted: saved {gb:.2f} GB ({pct:.0f}%)"
                else:
                    summary = f"[backfill] {jt.capitalize()} completed"
                etype = EVENT_COMPLETED
            elif status == "failed":
                err = (j["error_log"] or "")[:120]
                summary = f"[backfill] Failed: {err}" if err else "[backfill] Failed"
                etype = EVENT_FAILED
            else:
                continue

            await db.execute(
                "INSERT INTO file_events (file_path, event_type, occurred_at, summary, details_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (fp, etype, occurred, summary, json.dumps({"job_id": j["id"]})),
            )
            inserted += 1
            if (i + 1) % CHUNK == 0:
                await db.commit()

        await db.commit()
    finally:
        await db.close()
    if inserted:
        print(f"[FILE_EVENTS] Backfilled {inserted} events from jobs table", flush=True)
    return inserted
