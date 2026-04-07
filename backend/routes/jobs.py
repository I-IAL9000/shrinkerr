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
        if active_filter != "all":
            # Use enrichment + filter matching to only include files matching the filter
            import aiosqlite
            from backend.database import DB_PATH
            from backend.routes.scan import _build_enrichment_context, _enrich_row, _matches_filter, _SCAN_SELECT_COLS, _SCAN_WHERE
            db_resolve = await aiosqlite.connect(DB_PATH)
            db_resolve.row_factory = aiosqlite.Row
            try:
                ctx = await _build_enrichment_context(db_resolve)
                for fp in folder_paths:
                    async with db_resolve.execute(
                        f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} AND file_path LIKE ?",
                        (fp + "%",),
                    ) as cur:
                        rows = await cur.fetchall()
                    matched = 0
                    for row in rows:
                        enriched = _enrich_row(dict(row), ctx)
                        if _matches_filter(enriched, active_filter):
                            file_paths.append(enriched["file_path"])
                            matched += 1
                    print(f"[QUEUE] Folder '{fp}' resolved to {matched}/{len(rows)} files (filter: {active_filter})", flush=True)
            finally:
                await db_resolve.close()
        else:
            db_resolve = await connect_db()
            try:
                for fp in folder_paths:
                    async with db_resolve.execute(
                        "SELECT file_path FROM scan_results WHERE file_path LIKE ? AND removed_from_list = 0",
                        (fp + "%",),
                    ) as cur:
                        rows = await cur.fetchall()
                        print(f"[QUEUE] Folder '{fp}' resolved to {len(rows)} files", flush=True)
                        file_paths.extend(r["file_path"] for r in rows)
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

        job_ids = []
        ignored_by_rule = 0
        for fp in file_paths:
            rule = rule_results.get(fp)

            # "skip" = do nothing at all, skip entirely
            if rule and rule["action"] == "skip":
                ignored_by_rule += 1
                print(f"[QUEUE] Skipped {fp} entirely (rule: {rule['rule_name']})", flush=True)
                continue

            # "ignore" = skip video conversion, still do audio/sub cleanup
            skip_conversion = rule and rule["action"] == "ignore"
            if skip_conversion:
                ignored_by_rule += 1

            async with db.execute(
                "SELECT file_size, needs_conversion, audio_tracks_json, subtitle_tracks_json, duration, COALESCE(video_height, 0) as video_height FROM scan_results WHERE file_path = ?",
                (fp,),
            ) as cur:
                row = await cur.fetchone()
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
            if payload.audio_codec_override is not None:
                audio_codec = payload.audio_codec_override
            if payload.audio_bitrate_override is not None:
                audio_bitrate = payload.audio_bitrate_override
            if payload.target_resolution_override is not None:
                target_resolution = payload.target_resolution_override

            job_id = await _queue.add_job(
                file_path=fp,
                job_type=job_type,
                encoder=encoder,
                audio_tracks_to_remove=audio_remove,
                subtitle_tracks_to_remove=sub_remove,
                original_size=row["file_size"],
                nvenc_preset=nvenc_preset,
                nvenc_cq=nvenc_cq,
                libx265_crf=libx265_crf,
                target_resolution=target_resolution,
                audio_codec=audio_codec,
                audio_bitrate=audio_bitrate,
                priority=max(
                    payload.priority,
                    rule.get("queue_priority") or 0 if rule else 0,
                    1 if plex_prioritize and any(fp.startswith(uf) for uf in unwatched_folders) else 0,
                ),
            )
            job_ids.append(job_id)

        if ignored_by_rule > 0:
            await db.commit()

        return {"job_ids": job_ids, "added": len(job_ids), "ignored_by_rule": ignored_by_rule}
    finally:
        await db.close()


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


# --- Add by path (for NZBGet/external integrations) ---

import os as _os

class AddByPathRequest(BaseModel):
    file_paths: list[str]
    priority: int = 1
    force_reencode: bool = False
    skip_arr_rescan: bool = False
    insert_next: bool = False


@router.post("/add-by-path")
async def add_jobs_by_path(payload: AddByPathRequest):
    """Queue files by path — probes files directly without requiring scan_results."""
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    from backend.scanner import probe_file, classify_audio_tracks, classify_subtitle_tracks, detect_native_language, codec_matches_source
    from backend.config import settings

    # Load source codecs from settings
    source_codecs = ["h264"]
    try:
        async with connect_db() as _db:
            async with _db.execute("SELECT value FROM settings WHERE key = 'source_codecs'") as _cur:
                _row = await _cur.fetchone()
                if _row and _row[0]:
                    source_codecs = json.loads(_row[0])
    except Exception:
        pass

    added = 0
    errors = []

    for fp in payload.file_paths:
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

        job_id = await _queue.add_job(
            file_path=fp,
            job_type=job_type,
            encoder="nvenc",
            audio_tracks_to_remove=audio_remove,
            subtitle_tracks_to_remove=sub_remove,
            original_size=probe.get("file_size", 0),
            priority=payload.priority,
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
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
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
    """CQ-based empirical savings curve."""
    if cq <= 15: return 0.10
    if cq <= 18: return 0.15
    if cq <= 20: return 0.25
    if cq <= 22: return 0.35
    if cq <= 24: return 0.45
    if cq <= 26: return 0.55
    if cq <= 28: return 0.60
    return 0.65


@router.post("/estimate")
async def estimate_jobs(payload: EstimateRequest):
    """Estimate savings for a batch of files without creating jobs."""
    from backend.rule_resolver import resolve_rules_for_batch
    from backend.content_detect import detect_content_type_from_path, get_resolution_tier, get_recommended_cq
    import re

    # Resolve folder paths to actual file paths, respecting active filter
    file_paths = list(payload.file_paths)
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        active_filter = payload.filter or "all"
        if active_filter != "all":
            import aiosqlite
            from backend.database import DB_PATH
            from backend.routes.scan import _build_enrichment_context, _enrich_row, _matches_filter, _SCAN_SELECT_COLS, _SCAN_WHERE
            db_r = await aiosqlite.connect(DB_PATH)
            db_r.row_factory = aiosqlite.Row
            try:
                ctx = await _build_enrichment_context(db_r)
                for fp in folder_paths:
                    async with db_r.execute(
                        f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} AND file_path LIKE ?",
                        (fp + "%",),
                    ) as cur:
                        for row in await cur.fetchall():
                            enriched = _enrich_row(dict(row), ctx)
                            if _matches_filter(enriched, active_filter):
                                file_paths.append(enriched["file_path"])
            finally:
                await db_r.close()
        else:
            db_r = await connect_db()
            try:
                for fp in folder_paths:
                    async with db_r.execute(
                        "SELECT file_path FROM scan_results WHERE file_path LIKE ? AND removed_from_list = 0",
                        (fp + "%",),
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
        content_detect_on = est_settings.get("content_type_detection", "true").lower() == "true"
        res_aware = est_settings.get("resolution_aware_cq", "false").lower() == "true"
        res_cq_map = {
            "4k": int(est_settings.get("resolution_cq_4k", "24")),
            "1080p": int(est_settings.get("resolution_cq_1080p", "20")),
            "720p": int(est_settings.get("resolution_cq_720p", "18")),
            "sd": int(est_settings.get("resolution_cq_sd", "16")),
        }

        # Get avg time per job from daily stats
        async with db.execute(
            "SELECT COALESCE(SUM(total_encode_seconds), 0) as total_secs, "
            "COALESCE(SUM(jobs_completed), 0) as total_jobs FROM daily_stats"
        ) as cur:
            stat_row = await cur.fetchone()
        avg_seconds = (stat_row["total_secs"] / stat_row["total_jobs"]) if stat_row["total_jobs"] > 0 else 600

        total_files = 0
        total_size = 0
        estimated_savings = 0
        by_type = {"convert": 0, "audio": 0, "combined": 0}
        by_source = {}
        content_profiles: dict[str, dict] = {}
        resolution_breakdown = {"4k": 0, "1080p": 0, "720p": 0, "sd": 0}
        skipped = 0

        for fp in file_paths:
            rule = rule_results.get(fp)
            if rule and rule["action"] == "skip":
                skipped += 1
            skip_conv = rule and rule["action"] == "ignore"

            async with db.execute(
                "SELECT file_size, needs_conversion, audio_tracks_json, subtitle_tracks_json, COALESCE(video_height, 0) as video_height FROM scan_results WHERE file_path = ?",
                (fp,),
            ) as cur:
                row = await cur.fetchone()
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

            # Smart CQ per file for savings estimation
            if needs_conv:
                vh = row["video_height"] or 0
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

        est_time_seconds = (total_files * avg_seconds) / max(1, parallel)

        return {
            "total_selected": len(file_paths),
            "total_files": total_files,
            "total_size": total_size,
            "estimated_savings": estimated_savings,
            "estimated_time_seconds": round(est_time_seconds),
            "by_type": by_type,
            "by_source": by_source,
            "skipped_by_rules": skipped,
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
            headers={"Content-Disposition": f"attachment; filename=squeezarr-jobs-{today}.csv"},
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
            headers={"Content-Disposition": f"attachment; filename=squeezarr-jobs-{today}.json"},
        )
    finally:
        await db.close()
