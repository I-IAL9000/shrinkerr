import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db
from backend.queue import JobQueue, QueueWorker

router = APIRouter(prefix="/api/jobs")

_worker: Optional[QueueWorker] = None
_queue: Optional[JobQueue] = None


def init_job_routes(worker: QueueWorker, queue: JobQueue) -> None:
    global _worker, _queue
    _worker = worker
    _queue = queue


class BulkJobCreate(BaseModel):
    jobs: list[dict]


class ReorderRequest(BaseModel):
    job_ids: list[int]


class BulkUpdateSettingsRequest(BaseModel):
    job_ids: list[int]
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_preset: Optional[str] = None
    libx265_crf: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    priority: Optional[int] = None


class BulkMoveRequest(BaseModel):
    job_ids: list[int]
    position: str  # "top", "bottom", "up", "down"


class BulkIgnoreRequest(BaseModel):
    job_ids: list[int]


@router.post("/add")
async def add_job(
    file_path: str,
    job_type: str,
    encoder: Optional[str] = None,
    audio_tracks_to_remove: Optional[list[int]] = None,
):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    job_id = await _queue.add_job(
        file_path=file_path,
        job_type=job_type,
        encoder=encoder,
        audio_tracks_to_remove=audio_tracks_to_remove or [],
    )
    return {"job_id": job_id}


@router.post("/add-bulk")
async def add_bulk_jobs(payload: BulkJobCreate):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    job_ids = []
    for job_data in payload.jobs:
        job_id = await _queue.add_job(
            file_path=job_data["file_path"],
            job_type=job_data["job_type"],
            encoder=job_data.get("encoder"),
            audio_tracks_to_remove=job_data.get("audio_tracks_to_remove", []),
            subtitle_tracks_to_remove=job_data.get("subtitle_tracks_to_remove", []),
            original_size=job_data.get("original_size"),
            nvenc_preset=job_data.get("nvenc_preset"),
            nvenc_cq=job_data.get("nvenc_cq"),
            audio_codec=job_data.get("audio_codec"),
            audio_bitrate=job_data.get("audio_bitrate"),
        )
        job_ids.append(job_id)
    return {"job_ids": job_ids}


class BulkQueueFromScanRequest(BaseModel):
    file_paths: list[str] = []
    priority: int = 0  # 0=Normal, 1=High, 2=Highest
    override_rules: bool = False  # When True, ignore encoding rules
    select_all: bool = False  # When True, resolve matching files server-side
    filter: str = "all"  # Filter to apply when select_all=True
    # Encoding overrides from modal (None = auto)
    encoder_override: str | None = None
    nvenc_preset_override: str | None = None
    nvenc_cq_override: int | None = None
    libx265_crf_override: int | None = None
    libx265_preset_override: str | None = None
    audio_codec_override: str | None = None
    audio_bitrate_override: int | None = None
    target_resolution_override: str | None = None
    force_reencode: bool = False


@router.post("/add-from-scan")
async def add_jobs_from_scan(payload: BulkQueueFromScanRequest):
    """Create jobs from scan results — resolves track data from DB automatically."""
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    file_paths = list(payload.file_paths)

    # Resolve folder paths (ending with /) to actual file paths, respecting active filter
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        active_filter = payload.filter or "all"
        # Single query with OR'd LIKE clauses — much faster than N separate queries
        like_clause = " OR ".join(["file_path LIKE ?" for _ in folder_paths])
        like_args = [fp + "%" for fp in folder_paths]
        if active_filter != "all":
            import aiosqlite
            from backend.database import DB_PATH
            from backend.routes.scan import _build_enrichment_context, _enrich_row_minimal, _matches_filter, _SCAN_SELECT_COLS, _SCAN_WHERE
            db_resolve = await aiosqlite.connect(DB_PATH)
            db_resolve.row_factory = aiosqlite.Row
            try:
                ctx = await _build_enrichment_context(db_resolve)
                async with db_resolve.execute(
                    f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} AND ({like_clause})",
                    like_args,
                ) as cur:
                    rows = await cur.fetchall()
                total_rows = len(rows)
                for row in rows:
                    enriched = _enrich_row_minimal(dict(row), ctx)
                    if _matches_filter(enriched, active_filter):
                        file_paths.append(enriched["file_path"])
                print(f"[QUEUE] Resolved {len(folder_paths)} folder(s): {len(file_paths)}/{total_rows} files matched filter '{active_filter}'", flush=True)
            finally:
                await db_resolve.close()
        else:
            db_resolve = await connect_db()
            try:
                async with db_resolve.execute(
                    f"SELECT file_path FROM scan_results WHERE removed_from_list = 0 AND ({like_clause})",
                    like_args,
                ) as cur:
                    rows = await cur.fetchall()
                    file_paths.extend(r["file_path"] for r in rows)
                print(f"[QUEUE] Resolved {len(folder_paths)} folder(s) -> {len(rows)} files", flush=True)
            finally:
                await db_resolve.close()
    print(f"[QUEUE] Total file paths: {len(file_paths)}", flush=True)

    # Server-side select-all: resolve file paths matching the filter
    if payload.select_all and not file_paths:
        import aiosqlite
        from backend.database import DB_PATH
        from backend.routes.scan import (
            _build_enrichment_context, _enrich_row, _matches_filter,
            _SCAN_SELECT_COLS, _SCAN_WHERE,
        )
        sdb = await aiosqlite.connect(DB_PATH)
        sdb.row_factory = aiosqlite.Row
        try:
            ctx = await _build_enrichment_context(sdb)
            async with sdb.execute(
                f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                enriched = _enrich_row(dict(row), ctx)
                if _matches_filter(enriched, payload.filter):
                    file_paths.append(enriched["file_path"])
        finally:
            await sdb.close()

    if not file_paths:
        return {"job_ids": [], "added": 0}

    from backend.rule_resolver import resolve_rules_for_batch

    # Resolve encoding rules for all files in batch (unless overridden)
    if payload.override_rules:
        rule_results = {}
    else:
        rule_results = await resolve_rules_for_batch(file_paths)

    db = await connect_db()
    try:
        # Load conversion filter settings + smart encoding settings
        smart_keys = (
            'min_bitrate_mbps', 'max_bitrate_mbps', 'min_file_size_mb',
            'content_type_detection', 'resolution_aware_cq',
            'resolution_cq_4k', 'resolution_cq_1080p', 'resolution_cq_720p', 'resolution_cq_sd',
            'default_encoder',
        )
        filter_settings = {}
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in smart_keys)})",
            smart_keys,
        ) as cur:
            for row in await cur.fetchall():
                filter_settings[row["key"]] = row["value"]
        min_bitrate_bps = int(filter_settings.get("min_bitrate_mbps", "0")) * 1_000_000
        max_bitrate_bps = int(filter_settings.get("max_bitrate_mbps", "0")) * 1_000_000
        min_file_size_bytes = int(filter_settings.get("min_file_size_mb", "0")) * 1024 * 1024
        content_detect_enabled = filter_settings.get("content_type_detection", "true").lower() == "true"
        resolution_aware = filter_settings.get("resolution_aware_cq", "false").lower() == "true"
        res_cq = {
            "4k": int(filter_settings.get("resolution_cq_4k", "24")),
            "1080p": int(filter_settings.get("resolution_cq_1080p", "20")),
            "720p": int(filter_settings.get("resolution_cq_720p", "18")),
            "sd": int(filter_settings.get("resolution_cq_sd", "16")),
        }
        default_encoder = filter_settings.get("default_encoder", "nvenc")

        # Check if unwatched prioritization is enabled
        plex_prioritize = False
        unwatched_folders: set[str] = set()
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'plex_prioritize_unwatched'") as cur:
                row = await cur.fetchone()
                plex_prioritize = row and row["value"].lower() == "true"
            if plex_prioritize:
                async with db.execute(
                    "SELECT folder_path FROM plex_metadata_cache WHERE metadata_type = 'watch_status' AND metadata_value = 'unwatched'"
                ) as cur:
                    unwatched_folders = {r["folder_path"] for r in await cur.fetchall()}
        except Exception:
            pass

        # Batch-load all scan_results up front (avoids N+1 queries)
        scan_rows: dict[str, dict] = {}
        CHUNK = 900
        for i in range(0, len(file_paths), CHUNK):
            chunk = file_paths[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT file_path, file_size, needs_conversion, audio_tracks_json, "
                f"subtitle_tracks_json, duration, COALESCE(video_height, 0) as video_height "
                f"FROM scan_results WHERE file_path IN ({placeholders})",
                chunk,
            ) as cur:
                for r in await cur.fetchall():
                    scan_rows[r["file_path"]] = dict(r)

        jobs_to_insert: list[dict] = []
        ignored_by_rule = 0
        for fp in file_paths:
            rule = rule_results.get(fp)

            # "skip" = do nothing at all, skip entirely
            if rule and rule["action"] == "skip":
                ignored_by_rule += 1
                continue

            # "ignore" = skip video conversion, still do audio/sub cleanup
            skip_conversion = rule and rule["action"] == "ignore"
            if skip_conversion:
                ignored_by_rule += 1

            row = scan_rows.get(fp)
            if not row:
                continue

            # Check conversion filters (bitrate ceiling, min file size)
            if not payload.override_rules:
                file_size = row["file_size"] or 0
                duration = row["duration"] or 0

                if min_file_size_bytes > 0 and file_size < min_file_size_bytes:
                    continue
                if max_bitrate_bps > 0 and duration > 0:
                    bitrate = file_size * 8 / duration
                    if bitrate > max_bitrate_bps:
                        continue  # Above ceiling — don't convert (already high quality)
                if min_bitrate_bps > 0 and duration > 0:
                    bitrate = file_size * 8 / duration
                    if bitrate < min_bitrate_bps:
                        continue  # Below minimum — savings too small

            # Determine tracks to remove from stored classifications
            audio_remove = []
            sub_remove = []
            try:
                for t in json.loads(row["audio_tracks_json"] or "[]"):
                    if not t.get("keep", True) and not t.get("locked", False):
                        audio_remove.append(t["stream_index"])
            except (json.JSONDecodeError, ValueError):
                pass
            try:
                for t in json.loads(row["subtitle_tracks_json"] or "[]"):
                    if not t.get("keep", True) and not t.get("locked", False):
                        sub_remove.append(t["stream_index"])
            except (json.JSONDecodeError, ValueError):
                pass

            has_audio_work = len(audio_remove) > 0 or len(sub_remove) > 0

            # Also treat "native language not first" as audio work (reorder-only job)
            if not has_audio_work:
                try:
                    from backend.scanner import languages_match, _is_cleanup_enabled
                    if _is_cleanup_enabled("reorder_native_audio"):
                        all_tracks = json.loads(row["audio_tracks_json"] or "[]")
                        if len(all_tracks) > 1:
                            native = row.get("native_language") or ""
                            first_lang = (all_tracks[0].get("language") or "").lower()
                            if native and native.lower() != "und" and first_lang != native.lower():
                                if not languages_match(first_lang, native.lower()):
                                    has_audio_work = True
                except Exception:
                    pass

            # force_reencode overrides both needs_conversion AND skip rules
            needs_conv = bool(row["needs_conversion"]) or payload.force_reencode
            if skip_conversion and not payload.force_reencode:
                needs_conv = False

            if needs_conv and has_audio_work:
                job_type = "combined"
            elif needs_conv:
                job_type = "convert"
            elif has_audio_work:
                job_type = "audio"
            else:
                if skip_conversion:
                    print(f"[QUEUE] Skipped {fp} (ignore rule: {rule['rule_name']}), no audio/sub work", flush=True)
                    continue
                job_type = "audio"

            # Apply encoding rule overrides (if any)
            encoder = (rule.get("encoder") if rule else None) or default_encoder
            nvenc_preset = rule.get("nvenc_preset") if rule else None
            nvenc_cq = rule.get("nvenc_cq") if rule else None
            libx265_crf = rule.get("libx265_crf") if rule else None
            libx265_preset = rule.get("libx265_preset") if rule else None
            target_resolution = rule.get("target_resolution") if rule else None
            audio_codec = rule.get("audio_codec") if rule else None
            audio_bitrate = rule.get("audio_bitrate") if rule else None

            # Use global defaults when no rule sets CQ (no auto-override)
            # Content detection and resolution-aware CQ are informational only (shown in estimate)

            # Modal encoding overrides take highest precedence
            if payload.encoder_override is not None:
                encoder = payload.encoder_override
            if payload.nvenc_preset_override is not None:
                nvenc_preset = payload.nvenc_preset_override
            if payload.nvenc_cq_override is not None:
                nvenc_cq = payload.nvenc_cq_override
            if payload.libx265_crf_override is not None:
                libx265_crf = payload.libx265_crf_override
            if payload.libx265_preset_override is not None:
                libx265_preset = payload.libx265_preset_override
            if payload.audio_codec_override is not None:
                audio_codec = payload.audio_codec_override
            if payload.audio_bitrate_override is not None:
                audio_bitrate = payload.audio_bitrate_override
            if payload.target_resolution_override is not None:
                target_resolution = payload.target_resolution_override

            jobs_to_insert.append({
                "file_path": fp,
                "job_type": job_type,
                "encoder": encoder,
                "audio_tracks_to_remove": audio_remove,
                "subtitle_tracks_to_remove": sub_remove,
                "original_size": row["file_size"],
                "nvenc_preset": nvenc_preset,
                "nvenc_cq": nvenc_cq,
                "libx265_crf": libx265_crf,
                "libx265_preset": libx265_preset,
                "target_resolution": target_resolution,
                "audio_codec": audio_codec,
                "audio_bitrate": audio_bitrate,
                "priority": max(
                    payload.priority,
                    rule.get("queue_priority") or 0 if rule else 0,
                    1 if plex_prioritize and any(fp.startswith(uf) for uf in unwatched_folders) else 0,
                ),
            })

        if ignored_by_rule > 0:
            await db.commit()
    finally:
        await db.close()

    # Bulk-insert all queued jobs in a single transaction (huge perf win)
    all_ids = await _queue.add_jobs_bulk(jobs_to_insert)
    job_ids = [jid for jid in all_ids if jid]
    return {"job_ids": job_ids, "added": len(job_ids), "ignored_by_rule": ignored_by_rule}


@router.get("/")
async def list_jobs(status: Optional[str] = None, limit: int = 0, offset: int = 0):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    if status:
        return await _queue.get_jobs_by_status(status, limit=limit, offset=offset)
    return await _queue.get_all_jobs(limit=limit, offset=offset)


@router.get("/stats")
async def get_stats():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    return await _queue.get_stats()


@router.post("/start")
async def start_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    try:
        print(f"[API] Starting worker, running={_worker._running}, paused={_worker._paused}", flush=True)
        _worker.start()
        print("[API] Worker started", flush=True)
        return {"status": "started"}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[API] Worker start FAILED: {exc}", flush=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/pause")
async def pause_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    _worker.pause()
    return {"status": "paused"}


@router.post("/resume")
async def resume_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    _worker.resume()
    return {"status": "resumed"}


@router.post("/cancel-current")
async def cancel_current_job(job_id: Optional[int] = None):
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    cancelled_id = await _worker.cancel_current(job_id)
    if cancelled_id is None:
        return {"status": "no_job_running"}
    return {"status": "cancelled", "job_id": cancelled_id}


@router.post("/reorder")
async def reorder_jobs(payload: ReorderRequest):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.reorder_jobs(payload.job_ids)
    return {"status": "reordered"}


@router.post("/bulk-update-settings")
async def bulk_update_settings(payload: BulkUpdateSettingsRequest):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    # Build SET clause dynamically from provided fields
    updates = []
    params = []
    if payload.nvenc_preset is not None:
        updates.append("nvenc_preset = ?")
        params.append(payload.nvenc_preset)
    if payload.nvenc_cq is not None:
        updates.append("nvenc_cq = ?")
        params.append(payload.nvenc_cq)
    if payload.libx265_preset is not None:
        updates.append("libx265_preset = ?")
        params.append(payload.libx265_preset)
    if payload.libx265_crf is not None:
        updates.append("libx265_crf = ?")
        params.append(payload.libx265_crf)
    if payload.audio_codec is not None:
        updates.append("audio_codec = ?")
        params.append(payload.audio_codec)
    if payload.audio_bitrate is not None:
        updates.append("audio_bitrate = ?")
        params.append(payload.audio_bitrate)
    if payload.priority is not None:
        updates.append("priority = ?")
        params.append(max(0, min(2, payload.priority)))
    if not updates:
        return {"updated": 0}
    placeholders = ", ".join(["?"] * len(payload.job_ids))
    sql = (
        f"UPDATE jobs SET {', '.join(updates)} "
        f"WHERE id IN ({placeholders}) AND status = 'pending'"
    )
    params.extend(payload.job_ids)
    db = await connect_db()
    try:
        async with db.execute(sql, params) as cur:
            count = cur.rowcount
        await db.commit()
        return {"updated": count}
    finally:
        await db.close()


@router.post("/bulk-move")
async def bulk_move(payload: BulkMoveRequest):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    if payload.position not in ("top", "bottom", "up", "down"):
        raise HTTPException(status_code=400, detail="position must be top, bottom, up, or down")
    db = await connect_db()
    try:
        # Get all pending jobs ordered by queue_order
        async with db.execute(
            "SELECT id, queue_order FROM jobs WHERE status = 'pending' ORDER BY queue_order ASC"
        ) as cur:
            rows = await cur.fetchall()
        all_ids = [r["id"] for r in rows]
        selected = set(payload.job_ids)
        # Filter to only pending job ids that exist
        selected_pending = [jid for jid in all_ids if jid in selected]
        rest = [jid for jid in all_ids if jid not in selected]

        if not selected_pending:
            return {"status": "no_pending_jobs_matched"}

        if payload.position == "top":
            new_order = selected_pending + rest
        elif payload.position == "bottom":
            new_order = rest + selected_pending
        elif payload.position == "up":
            # Move each selected job up by 1 position
            new_order = list(all_ids)
            for jid in selected_pending:
                idx = new_order.index(jid)
                if idx > 0 and new_order[idx - 1] not in selected:
                    new_order[idx - 1], new_order[idx] = new_order[idx], new_order[idx - 1]
        elif payload.position == "down":
            # Move each selected job down by 1 position (iterate in reverse)
            new_order = list(all_ids)
            for jid in reversed(selected_pending):
                idx = new_order.index(jid)
                if idx < len(new_order) - 1 and new_order[idx + 1] not in selected:
                    new_order[idx], new_order[idx + 1] = new_order[idx + 1], new_order[idx]

        for order, jid in enumerate(new_order, start=1):
            await db.execute(
                "UPDATE jobs SET queue_order = ? WHERE id = ?", (order, jid)
            )
        await db.commit()
        return {"status": "moved", "new_order": new_order}
    finally:
        await db.close()


@router.post("/bulk-ignore")
async def bulk_ignore(payload: BulkIgnoreRequest):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    db = await connect_db()
    try:
        ignored = 0
        now = datetime.now(timezone.utc).isoformat()
        for job_id in payload.job_ids:
            # Get the job's file_path
            async with db.execute("SELECT file_path FROM jobs WHERE id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                continue
            file_path = row["file_path"]
            # Add to ignored_files
            await db.execute(
                "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
                (file_path, "user_ignored", now),
            )
            # Delete the job
            await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            ignored += 1
        await db.commit()
        return {"ignored": ignored}
    finally:
        await db.close()


# --- Health check ---

class HealthCheckRequest(BaseModel):
    file_paths: list[str] = []
    mode: str = "quick"  # "quick" | "thorough"
    select_all: bool = False
    filter: str = "all"


@router.post("/health-check")
async def queue_health_checks(payload: HealthCheckRequest):
    """Queue health_check jobs for the given files (or current filter when select_all=True)."""
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    mode = payload.mode.lower()
    if mode not in ("quick", "thorough"):
        raise HTTPException(status_code=400, detail="mode must be 'quick' or 'thorough'")

    file_paths = list(payload.file_paths)

    # Resolve folder paths (same pattern used elsewhere in this file)
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        like_clause = " OR ".join(["file_path LIKE ?" for _ in folder_paths])
        like_args = [fp + "%" for fp in folder_paths]
        active_filter = payload.filter or "all"
        if active_filter != "all":
            import aiosqlite
            from backend.database import DB_PATH
            from backend.routes.scan import _build_enrichment_context, _enrich_row_minimal, _matches_filter, _SCAN_SELECT_COLS, _SCAN_WHERE
            db_r = await aiosqlite.connect(DB_PATH)
            db_r.row_factory = aiosqlite.Row
            try:
                ctx = await _build_enrichment_context(db_r)
                async with db_r.execute(
                    f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} AND ({like_clause})",
                    like_args,
                ) as cur:
                    for row in await cur.fetchall():
                        enriched = _enrich_row_minimal(dict(row), ctx)
                        if _matches_filter(enriched, active_filter):
                            file_paths.append(enriched["file_path"])
            finally:
                await db_r.close()
        else:
            db_r = await connect_db()
            try:
                async with db_r.execute(
                    f"SELECT file_path FROM scan_results WHERE removed_from_list = 0 AND ({like_clause})",
                    like_args,
                ) as cur:
                    file_paths.extend(r["file_path"] for r in await cur.fetchall())
            finally:
                await db_r.close()

    # select_all path (whole library matching filter)
    if payload.select_all and not file_paths:
        import aiosqlite
        from backend.database import DB_PATH
        from backend.routes.scan import (
            _build_enrichment_context, _enrich_row_minimal, _matches_filter,
            _SCAN_SELECT_COLS, _SCAN_WHERE,
        )
        sdb = await aiosqlite.connect(DB_PATH)
        sdb.row_factory = aiosqlite.Row
        try:
            ctx = await _build_enrichment_context(sdb)
            async with sdb.execute(
                f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE}"
            ) as cur:
                for row in await cur.fetchall():
                    enriched = _enrich_row_minimal(dict(row), ctx)
                    if _matches_filter(enriched, payload.filter):
                        file_paths.append(enriched["file_path"])
        finally:
            await sdb.close()

    if not file_paths:
        return {"added": 0, "job_ids": []}

    # Build bulk-insert payload. Store mode in the 'encoder' column (re-uses existing schema).
    jobs_to_insert = [
        {
            "file_path": fp,
            "job_type": "health_check",
            "encoder": mode,
            "priority": 0,
        }
        for fp in file_paths
    ]
    all_ids = await _queue.add_jobs_bulk(jobs_to_insert)
    job_ids = [jid for jid in all_ids if jid]

    # Auto-start the worker if idle
    if job_ids and _worker is not None:
        if not _worker._running or _worker._paused:
            _worker.start()

    return {"added": len(job_ids), "job_ids": job_ids, "mode": mode}


# --- Add by path (for NZBGet/external integrations) ---

import os as _os

class AddByPathRequest(BaseModel):
    file_paths: list[str]
    priority: int = 1
    force_reencode: bool = False
    skip_arr_rescan: bool = False
    insert_next: bool = False
    nzbget_category: str | None = None


class ResetHealthRequest(BaseModel):
    file_paths: list[str] = []
    reset_all_corrupt: bool = False
    unignore: bool = True


@router.post("/health-check/reset")
async def reset_health_status(payload: ResetHealthRequest):
    """Clear stored health_status so a file gets re-checked on the next pass.

    Two modes:
      * reset_all_corrupt=True  → clear every row currently flagged corrupt.
        Used after shipping a classifier fix to invalidate false positives
        en masse (e.g. the "number of reference frames exceeds max" noise).
      * file_paths supplied     → clear only those specific paths.

    By default we also remove them from ignored_files so they return to the
    normal scan views. Set unignore=False to leave ignored_files alone.
    """
    if not payload.reset_all_corrupt and not payload.file_paths:
        raise HTTPException(status_code=400, detail="Provide file_paths or set reset_all_corrupt=true")

    db = await connect_db()
    try:
        if payload.reset_all_corrupt:
            async with db.execute(
                "SELECT file_path FROM scan_results WHERE health_status = 'corrupt'"
            ) as cur:
                targets = [r["file_path"] for r in await cur.fetchall()]
        else:
            targets = list(payload.file_paths)

        if not targets:
            return {"reset": 0, "unignored": 0}

        # SQLite has a parameter limit (~999); chunk to be safe.
        reset_count = 0
        unignored_count = 0
        CHUNK = 500
        for i in range(0, len(targets), CHUNK):
            batch = targets[i:i + CHUNK]
            placeholders = ",".join("?" * len(batch))
            cur = await db.execute(
                f"UPDATE scan_results SET health_status = NULL, health_errors_json = NULL, "
                f"health_checked_at = NULL, health_check_type = NULL "
                f"WHERE file_path IN ({placeholders})",
                batch,
            )
            reset_count += cur.rowcount or 0

            if payload.unignore:
                cur2 = await db.execute(
                    f"DELETE FROM ignored_files WHERE file_path IN ({placeholders})",
                    batch,
                )
                unignored_count += cur2.rowcount or 0

        await db.commit()
        return {"reset": reset_count, "unignored": unignored_count, "targeted": len(targets)}
    finally:
        await db.close()


@router.post("/health-check/clear-pending")
async def clear_pending_health_checks():
    """Emergency cleanup: delete all PENDING health_check jobs.

    Useful when auto-queue has flooded the queue. Running jobs and other job
    types are untouched.
    """
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE job_type = 'health_check' AND status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
            n = row["n"] if row else 0
        await db.execute(
            "DELETE FROM jobs WHERE job_type = 'health_check' AND status = 'pending'"
        )
        # Clean up the file_events queued entries too so the Activity feed isn't drowned
        await db.execute(
            "DELETE FROM file_events WHERE event_type = 'queued' AND summary LIKE '%health check%'"
        )
        await db.commit()
        return {"deleted": n}
    finally:
        await db.close()


@router.post("/add-by-path")
async def add_jobs_by_path(payload: AddByPathRequest):
    """Queue files by path — probes files directly without requiring scan_results."""
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    from backend.scanner import probe_file, classify_audio_tracks, classify_subtitle_tracks, detect_native_language, codec_matches_source
    from backend.rule_resolver import resolve_rules_for_batch
    from backend.config import settings
    from backend.media_paths import load_media_dirs, is_in_any, _resolve

    # Load source codecs and default encoder from settings
    source_codecs = ["h264"]
    default_encoder = "nvenc"
    try:
        async with connect_db() as _db:
            async with _db.execute("SELECT key, value FROM settings WHERE key IN ('source_codecs', 'default_encoder')") as _cur:
                for _row in await _cur.fetchall():
                    if _row["key"] == "source_codecs" and _row["value"]:
                        source_codecs = json.loads(_row["value"])
                    elif _row["key"] == "default_encoder" and _row["value"]:
                        default_encoder = _row["value"]
    except Exception:
        pass

    # Containment check — stops callers from queuing `/etc/hostname` etc.
    allowed_dirs = await load_media_dirs()
    if not allowed_dirs:
        raise HTTPException(
            status_code=400,
            detail="No media directories configured",
        )
    safe_file_paths: list[str] = []
    early_errors: list[str] = []
    for raw_fp in payload.file_paths:
        resolved = _resolve(raw_fp)
        if not is_in_any(resolved, allowed_dirs):
            early_errors.append(f"Outside media dirs: {raw_fp}")
            continue
        safe_file_paths.append(resolved)

    # Resolve encoding rules for allowlisted paths only (skip any that
    # failed the containment check so rules aren't evaluated against
    # attacker-supplied paths).
    extra_context = {}
    if payload.nzbget_category:
        extra_context["nzbget_category"] = payload.nzbget_category
    rule_results = await resolve_rules_for_batch(safe_file_paths, extra_context=extra_context)

    added = 0
    errors = list(early_errors)

    for fp in safe_file_paths:
        if not _os.path.exists(fp):
            errors.append(f"File not found: {fp}")
            continue

        probe = await probe_file(fp)
        if not probe:
            errors.append(f"Probe failed: {fp}")
            continue

        video_codec = (probe.get("video_codec") or "").lower()
        needs_conversion = codec_matches_source(video_codec, source_codecs)
        if payload.force_reencode:
            needs_conversion = True

        print(f"[API] add-by-path: {_os.path.basename(fp)} codec={video_codec} source_codecs={source_codecs} needs_conversion={needs_conversion}", flush=True)

        native_lang = detect_native_language(probe.get("audio_tracks", []))
        audio_tracks = classify_audio_tracks(probe.get("audio_tracks", []), native_lang, probe.get("duration", 0))
        sub_tracks = classify_subtitle_tracks(probe.get("subtitle_tracks", []), native_lang)

        audio_remove = [t.stream_index for t in audio_tracks if not t.keep and not t.locked]
        sub_remove = [t.stream_index for t in sub_tracks if not t.keep and not t.locked]
        has_audio_work = len(audio_remove) > 0 or len(sub_remove) > 0

        if needs_conversion and has_audio_work:
            job_type = "combined"
        elif needs_conversion:
            job_type = "convert"
        elif has_audio_work:
            job_type = "audio"
        else:
            print(f"[API] add-by-path: SKIPPED {_os.path.basename(fp)} — no conversion or audio work needed", flush=True)
            continue

        # Apply encoding rule overrides
        rule = rule_results.get(fp)
        if rule and rule["action"] == "skip":
            print(f"[API] add-by-path: SKIPPED {_os.path.basename(fp)} — rule '{rule['rule_name']}' says skip", flush=True)
            continue

        encoder = (rule.get("encoder") if rule else None) or default_encoder
        nvenc_preset = rule.get("nvenc_preset") if rule else None
        nvenc_cq = rule.get("nvenc_cq") if rule else None
        libx265_crf = rule.get("libx265_crf") if rule else None
        libx265_preset = rule.get("libx265_preset") if rule else None
        target_resolution = rule.get("target_resolution") if rule else None
        audio_codec = rule.get("audio_codec") if rule else None
        audio_bitrate = rule.get("audio_bitrate") if rule else None

        job_id = await _queue.add_job(
            file_path=fp,
            job_type=job_type,
            encoder=encoder,
            audio_tracks_to_remove=audio_remove,
            subtitle_tracks_to_remove=sub_remove,
            original_size=probe.get("file_size", 0),
            nvenc_preset=nvenc_preset,
            nvenc_cq=nvenc_cq,
            libx265_crf=libx265_crf,
            libx265_preset=libx265_preset,
            target_resolution=target_resolution,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            priority=max(payload.priority, rule.get("queue_priority") or 0 if rule else 0),
            insert_next=payload.insert_next,
        )
        added += 1
        print(f"[API] Queued by path: {_os.path.basename(fp)} ({job_type}, priority={payload.priority}, insert_next={payload.insert_next})", flush=True)

    # Auto-start queue if items were added and worker is idle
    if added > 0 and _worker is not None:
        if not _worker._running or _worker._paused:
            print(f"[API] Auto-starting queue for {added} new job(s) from add-by-path", flush=True)
            _worker.start()

    return {"added": added, "errors": errors}


# --- Test Encode (must be before /{job_id} routes to avoid path conflicts) ---

class TestEncodeRequest(BaseModel):
    file_path: str
    encoder: str | None = "nvenc"
    cq: int | None = 20
    preset: str | None = "p6"
    sample_seconds: int = 30


@router.post("/test-encode")
async def start_test_encode(payload: TestEncodeRequest):
    """Run a test encode on a sample segment. Returns result directly."""
    from backend.test_encode import run_test_encode
    from backend.websocket import ws_manager

    # If file_path is a folder, pick the largest file in it
    test_file = payload.file_path
    if test_file.endswith("/"):
        db_t = await connect_db()
        try:
            async with db_t.execute(
                "SELECT file_path FROM scan_results WHERE file_path LIKE ? AND removed_from_list = 0 ORDER BY file_size DESC LIMIT 1",
                (test_file + "%",),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    test_file = row["file_path"]
                else:
                    raise HTTPException(status_code=400, detail="No files found in folder")
        finally:
            await db_t.close()

    try:
        result = await run_test_encode(
            file_path=test_file,
            encoder=payload.encoder or "nvenc",
            cq=payload.cq or 20,
            preset=payload.preset or "p6",
            sample_seconds=payload.sample_seconds,
            ws_manager=ws_manager,
        )
        return result
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/test-encode/{task_id}")
async def get_test_encode(task_id: str):
    """Get status/result of a test encode task."""
    from backend.test_encode import get_task
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/vmaf-status")
async def vmaf_status():
    """Check if VMAF is available in the installed ffmpeg."""
    from backend.test_encode import check_vmaf_available
    available = await check_vmaf_available()
    return {"vmaf_available": available}


# Module-level state for the remeasure background task. We keep a single
# in-flight task at a time — re-running while one's already going is almost
# always an accident, and the second run wouldn't have anything new to look
# at anyway.
_remeasure_task: dict = {"running": False, "started_at": None}


@router.get("/vmaf-remeasure/status")
async def vmaf_remeasure_status():
    """Report whether a remeasure pass is currently running, plus a count
    of jobs that would be candidates for remeasure right now."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        # Candidates: completed jobs with a score that's either flagged
        # uncertain OR landed below "Excellent" tier (≤92). Also need a
        # post-rename file (file_path) and a separate pre-rename source
        # (original_file_path) — without both, we have nothing to compare.
        async with db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE status='completed' AND vmaf_score IS NOT NULL "
            "  AND (vmaf_uncertain = 1 OR vmaf_score < 93) "
            "  AND original_file_path IS NOT NULL "
            "  AND original_file_path <> file_path"
        ) as cur:
            row = await cur.fetchone()
        candidates = (row["n"] if row else 0)
    finally:
        await db.close()
    return {
        "running": _remeasure_task["running"],
        "started_at": _remeasure_task["started_at"],
        "candidates": candidates,
    }


@router.post("/vmaf-remeasure")
async def start_vmaf_remeasure():
    """Re-run VMAF on completed jobs whose recorded score is suspect.

    Iterates jobs flagged `vmaf_uncertain=1` or scored below 93 (the
    "Excellent" tier cut), provided both the original (pre-rename) file
    and the encoded (post-rename) file still exist on disk. If the user
    deletes originals after conversion (the common default), this skips
    those jobs — there's nothing to compare against without re-encoding.

    Returns immediately with the candidate count; progress events stream
    over the websocket as `{type: "vmaf_remeasure_progress", ...}`.
    """
    if _remeasure_task["running"]:
        raise HTTPException(409, "A VMAF re-measure pass is already running.")

    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT id, file_path, original_file_path, vmaf_score, vmaf_uncertain "
            "FROM jobs "
            "WHERE status='completed' AND vmaf_score IS NOT NULL "
            "  AND (vmaf_uncertain = 1 OR vmaf_score < 93) "
            "  AND original_file_path IS NOT NULL "
            "  AND original_file_path <> file_path "
            "ORDER BY id DESC"
        ) as cur:
            candidate_rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    if not candidate_rows:
        return {"started": False, "total": 0, "message": "No remeasure candidates."}

    import asyncio as _asyncio
    from backend.websocket import ws_manager
    from datetime import datetime, timezone

    async def _run_remeasure_pass(rows: list[dict]) -> None:
        from backend.converter import remeasure_vmaf
        _remeasure_task["running"] = True
        _remeasure_task["started_at"] = datetime.now(timezone.utc).isoformat()
        total = len(rows)
        rescued = 0
        skipped = 0
        unchanged = 0
        try:
            for idx, row in enumerate(rows, start=1):
                job_id = row["id"]
                src = row["original_file_path"]
                dst = row["file_path"]
                file_name = (dst or src or "").rsplit("/", 1)[-1]
                old_score = row["vmaf_score"]
                await ws_manager.broadcast({
                    "type": "vmaf_remeasure_progress",
                    "done": idx - 1, "total": total,
                    "current_file": file_name,
                    "stage": "starting",
                })
                try:
                    res = await remeasure_vmaf(src, dst)
                except Exception as exc:
                    print(f"[REMEASURE] job {job_id} crashed: {exc}", flush=True)
                    skipped += 1
                    continue

                if res.get("error"):
                    print(f"[REMEASURE] job {job_id} skipped — {res['error']}", flush=True)
                    skipped += 1
                else:
                    new_score = res["score"]
                    new_uncertain = res["uncertain"]
                    db2 = await connect_db()
                    try:
                        await db2.execute(
                            "UPDATE jobs SET vmaf_score = ?, vmaf_uncertain = ? WHERE id = ?",
                            (new_score, 1 if new_uncertain else 0, job_id),
                        )
                        await db2.execute(
                            "UPDATE scan_results SET vmaf_score = ?, vmaf_uncertain = ? WHERE file_path = ?",
                            (new_score, 1 if new_uncertain else 0, dst),
                        )
                        await db2.commit()
                    finally:
                        await db2.close()
                    if new_score is not None and old_score is not None and abs(new_score - old_score) >= 5:
                        rescued += 1
                        print(f"[REMEASURE] job {job_id}: {old_score} → {new_score} (rescued)", flush=True)
                    else:
                        unchanged += 1
                        print(f"[REMEASURE] job {job_id}: {old_score} → {new_score}", flush=True)

                await ws_manager.broadcast({
                    "type": "vmaf_remeasure_progress",
                    "done": idx, "total": total,
                    "current_file": file_name,
                    "stage": "done",
                    "rescued": rescued, "skipped": skipped, "unchanged": unchanged,
                })
        finally:
            _remeasure_task["running"] = False
            _remeasure_task["started_at"] = None
            await ws_manager.broadcast({
                "type": "vmaf_remeasure_complete",
                "total": total,
                "rescued": rescued,
                "skipped": skipped,
                "unchanged": unchanged,
            })
            print(
                f"[REMEASURE] Pass complete: {total} candidates, "
                f"{rescued} rescued, {unchanged} unchanged, {skipped} skipped.",
                flush=True,
            )

    _asyncio.create_task(_run_remeasure_pass(candidate_rows))
    return {"started": True, "total": len(candidate_rows)}


# --- Per-job operations (dynamic {job_id} routes must come AFTER static routes) ---

@router.delete("/{job_id}")
async def remove_job(job_id: int):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.remove_job(job_id)
    return {"status": "removed", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.update_status(job_id, "cancelled")
    return {"status": "cancelled", "job_id": job_id}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    """Retry a failed job.

    Special handling: if the file on disk has been renamed since the job was
    recorded (a previous partial conversion: x264 → x265 on disk succeeded but
    a later step failed), the conversion is effectively done. Don't re-run it
    — repoint scan_results to the new file, mark the original job completed,
    and return a note so the frontend can tell the user to rescan if they still
    want tracks removed.
    """
    import os as _os
    from backend.converter import rename_x264_to_x265

    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    # Fetch job to check its file_path
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT file_path, job_type, audio_tracks_to_remove, subtitle_tracks_to_remove "
            "FROM jobs WHERE id = ?", (job_id,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    if row:
        fp = row["file_path"]
        if fp and not _os.path.exists(fp):
            # Try common renames: x264 → x265 in the filename
            candidates = []
            try:
                d = _os.path.dirname(fp)
                orig_name = _os.path.basename(fp)
                renamed_name = rename_x264_to_x265(orig_name)
                if renamed_name != orig_name:
                    candidates.append(_os.path.join(d, renamed_name))
            except Exception:
                pass

            for candidate in candidates:
                if _os.path.exists(candidate):
                    # File was already converted in a previous run. Mark this job
                    # completed (conversion part done), update scan_results to the
                    # new path, and return a note so the user can decide whether
                    # to rescan for track-removal.
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat()
                    try:
                        new_size = _os.path.getsize(candidate)
                    except OSError:
                        new_size = None

                    had_track_removal = False
                    try:
                        import json as _json
                        a = _json.loads(row["audio_tracks_to_remove"] or "[]")
                        s = _json.loads(row["subtitle_tracks_to_remove"] or "[]")
                        had_track_removal = bool(a) or bool(s)
                    except Exception:
                        pass

                    db2 = await connect_db()
                    try:
                        # Mark the failed job as completed (conversion happened)
                        await db2.execute(
                            "UPDATE jobs SET status = 'completed', completed_at = ?, "
                            "file_path = ?, error_log = NULL WHERE id = ?",
                            (now, candidate, job_id),
                        )
                        # Update scan_results to point to the new file
                        if new_size:
                            await db2.execute(
                                "UPDATE scan_results SET file_path = ?, file_size = ?, "
                                "video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                                "WHERE file_path = ?",
                                (candidate, new_size, fp),
                            )
                        else:
                            await db2.execute(
                                "UPDATE scan_results SET file_path = ?, "
                                "video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                                "WHERE file_path = ?",
                                (candidate, fp),
                            )
                        await db2.commit()
                        print(f"[RETRY] Job {job_id}: file was already converted to {candidate} in a prior run — marking completed", flush=True)
                    finally:
                        await db2.close()

                    if had_track_removal:
                        msg = ("The file was already converted in a previous run. "
                               "Marked the job as completed and updated the path. "
                               "To remove the tracks, rescan the folder and queue a new audio-cleanup job.")
                    else:
                        msg = "The file was already converted in a previous run. Marked the job as completed."
                    return {"status": "completed", "job_id": job_id, "message": msg, "new_path": candidate}

    await _queue.update_status(job_id, "pending")
    return {"status": "pending", "job_id": job_id}


@router.post("/clear-completed")
async def clear_completed():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.clear_completed()
    return {"status": "cleared"}


@router.post("/clear-pending")
async def clear_pending():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.clear_pending()
    return {"status": "cleared"}


# --- Undo / Recent Conversions ---

@router.get("/recent-conversions")
async def recent_conversions(limit: int = 20):
    """Return last N completed conversions that have backup files available."""
    import os
    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, original_file_path, backup_path, space_saved,
                      original_size, completed_at, job_type, encoder, nvenc_cq, nvenc_preset
               FROM jobs
               WHERE status = 'completed' AND backup_path IS NOT NULL
                 AND job_type IN ('convert', 'combined')
               ORDER BY completed_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            bp = row["backup_path"]
            results.append({
                **dict(row),
                "file_name": (row["file_path"] or "").rsplit("/", 1)[-1],
                "backup_exists": os.path.exists(bp) if bp else False,
            })
        return results
    finally:
        await db.close()


@router.post("/{job_id}/undo")
async def undo_conversion(job_id: int):
    """Restore original file from backup, reverting a conversion."""
    import os
    from pathlib import Path

    db = await connect_db()
    try:
        async with db.execute(
            "SELECT file_path, original_file_path, backup_path, status, job_type FROM jobs WHERE id = ?",
            (job_id,),
        ) as cur:
            job = await cur.fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] not in ("completed", "reverted"):
            raise HTTPException(status_code=400, detail=f"Cannot undo job with status '{job['status']}'")

        backup_path = job["backup_path"]
        if not backup_path or not os.path.exists(backup_path):
            raise HTTPException(status_code=400, detail="Backup file not found on disk")

        converted_path = job["file_path"]
        original_path = job["original_file_path"] or job["file_path"]

        # Delete the converted file
        if converted_path and os.path.exists(converted_path):
            os.unlink(converted_path)
            print(f"[UNDO] Deleted converted file: {converted_path}", flush=True)

        # Move backup back to original location
        dest = Path(original_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        Path(backup_path).rename(dest)
        print(f"[UNDO] Restored original: {backup_path} → {original_path}", flush=True)

        # Update job status
        await db.execute(
            "UPDATE jobs SET status = 'reverted' WHERE id = ?", (job_id,)
        )

        # Reset scan_results for this file
        await db.execute(
            """UPDATE scan_results SET converted = 0, needs_conversion = 1,
                   video_codec = 'h264', file_path = ?, file_size = ?
               WHERE file_path = ? OR file_path = ?""",
            (original_path, os.path.getsize(original_path), converted_path, original_path),
        )
        await db.commit()

        try:
            from backend.file_events import log_event, EVENT_REVERTED
            await log_event(original_path, EVENT_REVERTED, "Restored original from backup", {"job_id": job_id, "converted_path": converted_path})
        except Exception:
            pass

        return {
            "status": "reverted",
            "restored_path": original_path,
            "size": os.path.getsize(original_path),
        }
    finally:
        await db.close()


@router.get("/{job_id}/log")
async def get_job_log(job_id: int):
    """Return detailed conversion log for a job."""
    db = await connect_db()
    try:
        async with db.execute(
            """SELECT ffmpeg_command, ffmpeg_log, encoding_stats, vmaf_score,
                      space_saved, original_size, started_at, completed_at,
                      encoder, nvenc_preset, nvenc_cq, audio_codec, audio_bitrate,
                      libx265_crf, target_resolution
               FROM jobs WHERE id = ?""",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        result = dict(row)
        # Parse encoding_stats JSON
        if result.get("encoding_stats"):
            try:
                result["encoding_stats"] = json.loads(result["encoding_stats"])
            except (json.JSONDecodeError, ValueError):
                pass
        return result
    finally:
        await db.close()


# --- Estimation ---

class EstimateRequest(BaseModel):
    file_paths: list[str]
    override_rules: bool = False
    filter: str = "all"
    # Encoding overrides (same as queue request)
    nvenc_cq_override: int | None = None
    libx265_crf_override: int | None = None
    force_reencode: bool = False


def _cq_to_savings_pct(cq: int) -> float:
    """CQ-based empirical savings curve for x264→x265 conversion.

    Calibrated from real-world NVENC results:
      CQ 23 → ~54% actual, CQ 27 → ~77% actual
    """
    if cq <= 15: return 0.25
    if cq <= 18: return 0.35
    if cq <= 20: return 0.45
    if cq <= 22: return 0.50
    if cq <= 23: return 0.55
    if cq <= 24: return 0.60
    if cq <= 25: return 0.65
    if cq <= 26: return 0.70
    if cq <= 27: return 0.75
    if cq <= 28: return 0.77
    return 0.80


@router.post("/estimate")
async def estimate_jobs(payload: EstimateRequest):
    """Estimate savings for a batch of files without creating jobs."""
    try:
        return await _estimate_jobs_impl(payload)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ESTIMATE] Fatal error: {exc}", flush=True)
        # Return a minimal valid response so the frontend shows *something*
        return {
            "total_selected": len(payload.file_paths),
            "total_files": 0,
            "total_size": 0,
            "estimated_savings": 0,
            "estimated_time_seconds": 0,
            "by_type": {"convert": 0, "audio": 0, "combined": 0},
            "by_source": {},
            "skipped_by_rules": 0,
            "ignored_files": 0,
            "cq": 20,
            "savings_pct": 0,
            "content_profiles": {},
            "resolution_breakdown": {},
            "smart_encoding": False,
            "error": str(exc),
        }


async def _estimate_jobs_impl(payload: EstimateRequest):
    from backend.rule_resolver import resolve_rules_for_batch
    from backend.content_detect import detect_content_type_from_path, get_resolution_tier, get_recommended_cq
    import re

    # Resolve folder paths to actual file paths, respecting active filter
    file_paths = list(payload.file_paths)
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        active_filter = payload.filter or "all"
        like_clause = " OR ".join(["file_path LIKE ?" for _ in folder_paths])
        like_args = [fp + "%" for fp in folder_paths]
        if active_filter != "all":
            import aiosqlite
            from backend.database import DB_PATH
            from backend.routes.scan import _build_enrichment_context, _enrich_row_minimal, _matches_filter, _SCAN_SELECT_COLS, _SCAN_WHERE
            db_r = await aiosqlite.connect(DB_PATH)
            db_r.row_factory = aiosqlite.Row
            try:
                ctx = await _build_enrichment_context(db_r)
                async with db_r.execute(
                    f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} AND ({like_clause})",
                    like_args,
                ) as cur:
                    for row in await cur.fetchall():
                        enriched = _enrich_row_minimal(dict(row), ctx)
                        if _matches_filter(enriched, active_filter):
                            file_paths.append(enriched["file_path"])
            finally:
                await db_r.close()
        else:
            db_r = await connect_db()
            try:
                async with db_r.execute(
                    f"SELECT file_path FROM scan_results WHERE removed_from_list = 0 AND ({like_clause})",
                    like_args,
                ) as cur:
                    file_paths.extend(r["file_path"] for r in await cur.fetchall())
            finally:
                await db_r.close()

    if payload.override_rules:
        rule_results = {}
    else:
        rule_results = await resolve_rules_for_batch(file_paths)

    db = await connect_db()
    try:
        # Load settings for smart CQ
        est_keys = ('nvenc_cq', 'content_type_detection', 'resolution_aware_cq',
                     'resolution_cq_4k', 'resolution_cq_1080p', 'resolution_cq_720p', 'resolution_cq_sd')
        est_settings = {}
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in est_keys)})", est_keys
        ) as cur:
            for row in await cur.fetchall():
                est_settings[row["key"]] = row["value"]

        global_cq = int(est_settings.get("nvenc_cq", "20"))

        # Read default encoder for per-file time estimate
        async with db.execute("SELECT value FROM settings WHERE key = 'default_encoder'") as cur:
            _enc_row = await cur.fetchone()
        default_encoder = (_enc_row["value"] if _enc_row else "nvenc").lower()
        content_detect_on = est_settings.get("content_type_detection", "true").lower() == "true"
        res_aware = est_settings.get("resolution_aware_cq", "false").lower() == "true"
        res_cq_map = {
            "4k": int(est_settings.get("resolution_cq_4k", "24")),
            "1080p": int(est_settings.get("resolution_cq_1080p", "20")),
            "720p": int(est_settings.get("resolution_cq_720p", "18")),
            "sd": int(est_settings.get("resolution_cq_sd", "16")),
        }

        # Compute a speed factor: encoding-seconds per content-second, derived from
        # recent completed jobs (last 30 days) so a few slow CPU-encoded outliers
        # in the full history don't skew small-file estimates.
        # Also compute a fallback avg_seconds for files whose duration is unknown.
        async with db.execute(
            "SELECT COALESCE(SUM(total_encode_seconds), 0) as total_secs, "
            "COALESCE(SUM(jobs_completed), 0) as total_jobs FROM daily_stats "
            "WHERE date >= date('now', '-30 days')"
        ) as cur:
            stat_row = await cur.fetchone()
        avg_seconds = (stat_row["total_secs"] / stat_row["total_jobs"]) if stat_row["total_jobs"] > 0 else 600

        # Per-encoder speed factors: encoding-seconds per content-second.
        # Uses jobs.started_at/completed_at directly rather than daily_stats aggregates
        # so we can filter by encoder (GPU vs CPU have very different speeds).
        # Each factor is the MEDIAN ratio across the last ~50 completed jobs of that
        # encoder — more robust than mean against occasional slow outliers.
        def _median(vals: list[float]) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

        async def _speed_factor_for(encoder_filter: str) -> float:
            # Prefer jobs.encoding_stats JSON which contains the actual ffmpeg
            # encode_seconds + duration (no post-processing overhead in it).
            # Fall back to started_at/completed_at elapsed time if stats are missing.
            async with db.execute(
                "SELECT j.encoding_stats, j.started_at, j.completed_at, sr.duration "
                "FROM jobs j JOIN scan_results sr ON sr.file_path = j.file_path "
                f"WHERE j.status = 'completed' "
                f"  AND j.job_type IN ('convert', 'combined') "
                f"  AND {encoder_filter} "
                "ORDER BY j.completed_at DESC LIMIT 100"
            ) as cur:
                rows = await cur.fetchall()
            ratios: list[float] = []
            for r in rows:
                enc_secs: float = 0.0
                dur: float = 0.0
                # Try encoding_stats JSON first (most accurate)
                raw_stats = r["encoding_stats"]
                if raw_stats:
                    try:
                        st = json.loads(raw_stats)
                        enc_secs = float(st.get("encode_seconds") or 0)
                        dur = float(st.get("duration") or 0)
                    except Exception:
                        pass
                # Fallback: elapsed time from started_at/completed_at
                if enc_secs <= 0 or dur <= 0:
                    try:
                        if r["started_at"] and r["completed_at"]:
                            t0 = datetime.fromisoformat(r["started_at"])
                            t1 = datetime.fromisoformat(r["completed_at"])
                            enc_secs = (t1 - t0).total_seconds()
                            dur = float(r["duration"] or 0)
                    except Exception:
                        pass
                if enc_secs > 0 and dur > 0:
                    ratios.append(enc_secs / dur)
                if len(ratios) >= 50:
                    break  # 50 data points is plenty
            med = _median(ratios)
            # Clamp: [0.02 (50x realtime), 3.0 (3x slower than realtime)]
            return max(0.02, min(3.0, med)) if med > 0 else 0.0

        try:
            nvenc_speed = await _speed_factor_for("LOWER(j.encoder) IN ('nvenc', 'hevc_nvenc')")
            libx265_speed = await _speed_factor_for("LOWER(j.encoder) IN ('libx265', 'x265', 'cpu')")
        except Exception as exc:
            print(f"[ESTIMATE] Speed factor query failed (using defaults): {exc}", flush=True)
            nvenc_speed = 0.0
            libx265_speed = 0.0

        # Fallbacks: typical realistic defaults
        if nvenc_speed == 0.0:
            nvenc_speed = 0.08  # ~12x realtime for NVENC p3 at 1080p
        if libx265_speed == 0.0:
            libx265_speed = 0.4  # ~2.5x realtime for libx265 medium on a decent CPU

        total_files = 0
        total_size = 0
        total_est_time = 0.0  # accumulates per-file encoding-seconds (sequential sum)
        estimated_savings = 0
        by_type = {"convert": 0, "audio": 0, "combined": 0}
        by_source = {}
        content_profiles: dict[str, dict] = {}
        resolution_breakdown = {"4k": 0, "1080p": 0, "720p": 0, "sd": 0}
        skipped = 0
        ignored_count = 0

        # Check which files are in the ignored_files table — batched
        ignored_set = set()
        scan_rows: dict[str, dict] = {}
        if file_paths:
            # Chunk IN clauses to stay under SQLite's 999-variable limit
            CHUNK = 900
            try:
                for i in range(0, len(file_paths), CHUNK):
                    chunk = file_paths[i:i + CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    async with db.execute(
                        f"SELECT file_path FROM ignored_files WHERE file_path IN ({placeholders})",
                        chunk,
                    ) as cur:
                        for r in await cur.fetchall():
                            ignored_set.add(r["file_path"])
            except Exception:
                pass

            # Batch-load scan_results for all file paths (avoids N+1 queries)
            for i in range(0, len(file_paths), CHUNK):
                chunk = file_paths[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT file_path, file_size, needs_conversion, audio_tracks_json, "
                    f"subtitle_tracks_json, COALESCE(video_height, 0) as video_height, "
                    f"COALESCE(duration, 0) as duration, native_language "
                    f"FROM scan_results WHERE file_path IN ({placeholders})",
                    chunk,
                ) as cur:
                    for r in await cur.fetchall():
                        scan_rows[r["file_path"]] = dict(r)

        print(f"[ESTIMATE] {len(file_paths)} file(s) to estimate, {len(scan_rows)} found in scan_results, {len(ignored_set)} ignored", flush=True)
        if len(file_paths) > 0 and len(scan_rows) == 0:
            print(f"[ESTIMATE] No scan_results found for paths: {file_paths[:5]}", flush=True)

        for fp in file_paths:
            rule = rule_results.get(fp)
            if rule and rule["action"] == "skip":
                skipped += 1
            if fp in ignored_set:
                ignored_count += 1
            skip_conv = rule and rule["action"] == "ignore"

            row = scan_rows.get(fp)
            if not row:
                continue

            has_audio = False
            try:
                tracks = json.loads(row["audio_tracks_json"] or "[]")
                has_audio = any(not t.get("keep", True) and not t.get("locked", False) for t in tracks)
            except Exception:
                pass
            has_subs = False
            try:
                stracks = json.loads(row["subtitle_tracks_json"] or "[]")
                has_subs = any(not t.get("keep", True) and not t.get("locked", False) for t in stracks)
            except Exception:
                pass

            # force_reencode overrides both needs_conversion AND skip rules
            needs_conv = bool(row["needs_conversion"]) or payload.force_reencode
            if skip_conv and not payload.force_reencode:
                needs_conv = False
            has_work = has_audio or has_subs

            if needs_conv and has_work:
                jt = "combined"
            elif needs_conv:
                jt = "convert"
            elif has_work:
                jt = "audio"
            elif skip_conv:
                continue
            else:
                jt = "audio"

            total_files += 1
            total_size += row["file_size"]
            by_type[jt] = by_type.get(jt, 0) + 1

            # Per-file time estimate — pick speed factor based on target encoder
            file_dur = float(row.get("duration") or 0)
            if jt == "audio":
                # Remux only — fast, ~30 seconds regardless of content length
                per_file_seconds = 30
            elif file_dur > 0:
                # Determine which encoder this file will use
                target_enc = (rule.get("encoder") if rule else None) or default_encoder
                is_cpu = (target_enc or "").lower() in ("libx265", "x265", "cpu")
                sf = libx265_speed if is_cpu else nvenc_speed
                per_file_seconds = file_dur * sf
            else:
                per_file_seconds = avg_seconds
            total_est_time += per_file_seconds

            # Smart CQ per file for savings estimation
            if needs_conv:
                vh = row["video_height"] or 0
                # Use filename resolution as fallback or override if DB value seems wrong
                fn = fp.lower()
                if "2160p" in fn or "4k" in fn or "uhd" in fn:
                    fn_vh = 2160
                elif "1080p" in fn or "1080i" in fn:
                    fn_vh = 1080
                elif "720p" in fn:
                    fn_vh = 720
                elif "480p" in fn:
                    fn_vh = 480
                else:
                    fn_vh = 0
                # Trust filename if DB has no data or filename suggests higher res
                if fn_vh > 0 and (vh == 0 or fn_vh > vh):
                    vh = fn_vh
                tier = get_resolution_tier(vh)
                resolution_breakdown[tier] = resolution_breakdown.get(tier, 0) + 1

                # Determine effective CQ for this file
                file_cq = payload.nvenc_cq_override  # Modal override highest
                if file_cq is None:
                    rule_cq = rule.get("nvenc_cq") if rule else None
                    if rule_cq is not None:
                        file_cq = rule_cq
                    elif content_detect_on:
                        ctype = detect_content_type_from_path(fp)
                        file_cq = get_recommended_cq(ctype, tier)
                        # Track content profiles
                        if ctype not in content_profiles:
                            content_profiles[ctype] = {"count": 0, "cq": file_cq}
                        content_profiles[ctype]["count"] += 1
                    elif res_aware:
                        file_cq = res_cq_map.get(tier, global_cq)
                    else:
                        file_cq = global_cq

                pct = _cq_to_savings_pct(file_cq)
                estimated_savings += int(row["file_size"] * pct)

            # Source type
            name = fp.rsplit("/", 1)[-1].lower()
            src = "Other"
            if re.search(r"blu[\-\s]?ray|bdremux|bdrip", name): src = "Blu-ray"
            elif "web-dl" in name or "webdl" in name: src = "WEB-DL"
            elif "webrip" in name: src = "WEBRip"
            elif "hdtv" in name: src = "HDTV"
            elif "dvd" in name: src = "DVD"
            elif "remux" in name: src = "Remux"
            by_source[src] = by_source.get(src, 0) + 1

        # Get parallel_jobs for time estimate
        async with db.execute("SELECT value FROM settings WHERE key = 'parallel_jobs'") as cur:
            prow = await cur.fetchone()
            parallel = int(prow["value"]) if prow else 1

        # Per-file estimates summed above; divide by parallel for wall-clock estimate
        est_time_seconds = total_est_time / max(1, parallel)

        return {
            "total_selected": len(file_paths),
            "total_files": total_files,
            "total_size": total_size,
            "estimated_savings": estimated_savings,
            "estimated_time_seconds": round(est_time_seconds),
            "by_type": by_type,
            "by_source": by_source,
            "skipped_by_rules": skipped,
            "ignored_files": ignored_count,
            "cq": payload.nvenc_cq_override or global_cq,
            "savings_pct": round((estimated_savings / total_size * 100) if total_size > 0 else 0),
            "content_profiles": content_profiles,
            "resolution_breakdown": resolution_breakdown,
            "smart_encoding": content_detect_on or res_aware,
        }
    finally:
        await db.close()


# --- Failed count (for sidebar badge) ---

@router.get("/failed-count")
async def get_failed_count():
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'") as cur:
            row = await cur.fetchone()
            return {"count": row["c"] if row else 0}
    finally:
        await db.close()


# --- Export ---

@router.get("/export/csv")
async def export_csv():
    """Export completed jobs as CSV."""
    from fastapi.responses import StreamingResponse
    import csv, io

    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, job_type, encoder, nvenc_preset, nvenc_cq,
                      space_saved, original_size, created_at, started_at, completed_at
               FROM jobs WHERE status = 'completed' ORDER BY completed_at DESC"""
        ) as cur:
            rows = await cur.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "file_path", "job_type", "encoder", "preset", "cq",
                         "space_saved_bytes", "original_size_bytes", "created_at", "started_at", "completed_at"])
        for r in rows:
            writer.writerow([r["id"], r["file_path"], r["job_type"], r["encoder"],
                             r["nvenc_preset"], r["nvenc_cq"], r["space_saved"], r["original_size"],
                             r["created_at"], r["started_at"], r["completed_at"]])

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=shrinkerr-jobs-{today}.csv"},
        )
    finally:
        await db.close()


@router.get("/export/json")
async def export_json():
    """Export completed jobs as JSON."""
    from fastapi.responses import StreamingResponse

    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, job_type, encoder, nvenc_preset, nvenc_cq,
                      space_saved, original_size, created_at, started_at, completed_at
               FROM jobs WHERE status = 'completed' ORDER BY completed_at DESC"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return StreamingResponse(
            iter([json.dumps(rows, indent=2)]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=shrinkerr-jobs-{today}.json"},
        )
    finally:
        await db.close()
