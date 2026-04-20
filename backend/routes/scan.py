import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.database import DB_PATH, connect_db
from backend.models import ScanRequest
from backend.scanner import scan_directory
from backend.websocket import ws_manager

router = APIRouter(prefix="/api/scan")

SCAN_BATCH_SIZE = 25

# Module-level scan state
_scan_task: asyncio.Task | None = None
_scan_cancel = asyncio.Event()


def _write_batch_sync(db_path: str, batch: list, now: str, mark_new: bool = False) -> None:
    """Write a batch of ScannedFile results to the database (synchronous, for use in thread executor)."""
    import sqlite3
    import time as _time
    for attempt in range(5):
        try:
            return _write_batch_sync_inner(db_path, batch, now, mark_new)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                print(f"[SCANNER] DB locked on batch write (attempt {attempt+1}/5), retrying in {2*(attempt+1)}s...", flush=True)
                _time.sleep(2 * (attempt + 1))
            else:
                raise

def _write_batch_sync_inner(db_path: str, batch: list, now: str, mark_new: bool = False) -> None:
    import sqlite3
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=60000")
    try:
        is_new_val = 1 if mark_new else 0
        new_detected_at_val = now if mark_new else None
        LOSSLESS_CODECS = {"truehd", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray", "flac", "mlp", "pcm_dvd"}
        DTS_LL = {"dts-hd ma", "dts-hd hra"}

        for scanned in batch:
            audio_json = json.dumps([t.model_dump() for t in scanned.audio_tracks])
            sub_json = json.dumps([t.model_dump() for t in scanned.subtitle_tracks]) if scanned.subtitle_tracks else None

            # Pre-compute flags at scan time (avoids 226K JSON parses per page load)
            has_removable = 1 if any(not t.keep for t in scanned.audio_tracks) else 0
            has_removable_subs = 1 if any(not t.keep for t in (scanned.subtitle_tracks or [])) else 0
            has_lossless = 0
            for t in scanned.audio_tracks:
                c = (t.codec or "").lower()
                if c in LOSSLESS_CODECS or (c == "dts" and (t.profile if hasattr(t, 'profile') else "").lower() in DTS_LL):
                    has_lossless = 1
                    break

            db.execute(
                """INSERT INTO scan_results
                   (file_path, file_size, video_codec, needs_conversion,
                    audio_tracks_json, subtitle_tracks_json, native_language, language_source, scan_timestamp, removed_from_list, is_new, file_mtime, new_detected_at, duration, probe_status, video_height,
                    has_removable_tracks_flag, has_removable_subs_flag, has_lossless_audio_flag, has_external_subs_flag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                       file_size=excluded.file_size,
                       video_codec=excluded.video_codec,
                       needs_conversion=excluded.needs_conversion,
                       audio_tracks_json=excluded.audio_tracks_json,
                       subtitle_tracks_json=excluded.subtitle_tracks_json,
                       native_language=excluded.native_language,
                       language_source=excluded.language_source,
                       scan_timestamp=excluded.scan_timestamp,
                       removed_from_list=0,
                       file_mtime=excluded.file_mtime,
                       -- Only bump new_detected_at when mark_new=True AND this is a re-add
                       -- (existing row had removed_from_list=1). Otherwise preserve the
                       -- original detection time so converted/renamed files don't
                       -- mass-flip to "new" when the watcher re-sees them.
                       new_detected_at = CASE
                           WHEN ? = 1 AND scan_results.removed_from_list = 1 THEN excluded.new_detected_at
                           ELSE scan_results.new_detected_at
                       END,
                       duration=excluded.duration,
                       probe_status=excluded.probe_status,
                       video_height=excluded.video_height,
                       has_removable_tracks_flag=excluded.has_removable_tracks_flag,
                       has_removable_subs_flag=excluded.has_removable_subs_flag,
                       has_lossless_audio_flag=excluded.has_lossless_audio_flag,
                       has_external_subs_flag=excluded.has_external_subs_flag
                """,
                (
                    scanned.file_path,
                    scanned.file_size,
                    scanned.video_codec,
                    1 if scanned.needs_conversion else 0,
                    audio_json,
                    sub_json,
                    scanned.native_language,
                    getattr(scanned, 'language_source', 'heuristic'),
                    now,
                    is_new_val,
                    scanned.file_mtime,
                    new_detected_at_val,
                    scanned.duration,
                    getattr(scanned, 'probe_status', 'ok'),
                    getattr(scanned, 'video_height', 0),
                    1 if (has_removable or getattr(scanned, 'needs_audio_reorder', False)) else 0,
                    has_removable_subs,
                    has_lossless,
                    1 if getattr(scanned, 'has_external_subs', False) else 0,
                    is_new_val,  # CASE expression param in ON CONFLICT clause (? = 1 AND removed_from_list = 1)
                ),
            )
        db.commit()
    finally:
        db.close()


async def _write_batch(db_path_or_db, batch: list, now: str, mark_new: bool = False) -> None:
    """Async wrapper — runs batch write in thread executor to avoid blocking event loop."""
    if isinstance(db_path_or_db, str):
        db_path = db_path_or_db
    else:
        # Legacy: if passed an aiosqlite connection, use DB_PATH
        db_path = DB_PATH
    await asyncio.get_event_loop().run_in_executor(
        None, _write_batch_sync, db_path, list(batch), now, mark_new
    )


_scan_proc = None
_scan_progress_file = "/tmp/shrinkerr_scan_progress.json"
_scan_cancel_file = "/tmp/shrinkerr_scan_cancel"


def _scan_worker_process(paths: list[str], db_path: str, progress_file: str, cancel_file: str) -> None:
    """Runs in a separate process — does all ffprobe/DB work without blocking the main event loop."""
    import os
    import sqlite3

    # Remove stale cancel file
    try:
        os.unlink(cancel_file)
    except FileNotFoundError:
        pass

    now = datetime.now(timezone.utc).isoformat()

    def write_progress(status, current_file="", total=0, probed=0):
        try:
            with open(progress_file, "w") as f:
                json.dump({"status": status, "current_file": current_file, "total": total, "probed": probed}, f)
        except Exception:
            pass

    def is_cancelled():
        return os.path.exists(cancel_file)

    # Delete old scan results
    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=60000")
        try:
            for path in paths:
                db.execute(
                    """DELETE FROM scan_results
                       WHERE file_path LIKE ?
                         AND file_path NOT IN (
                             SELECT file_path FROM jobs WHERE status IN ('pending', 'running')
                         )""",
                    (path.rstrip("/") + "/%",),
                )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        print(f"[SCANNER] Failed to clear old results: {exc}", flush=True)

    # Run the scan synchronously using asyncio.run in this process
    import asyncio as _asyncio

    async def _do_scan():
        from backend.scanner import scan_directory
        batch = []
        total_written = 0

        async def progress_cb(status, current_file="", files_found=0, files_probed=0, total_files=0):
            write_progress(status, current_file, total_files, files_probed)

        async def result_cb(scanned):
            nonlocal batch, total_written
            batch.append(scanned)
            if len(batch) >= SCAN_BATCH_SIZE:
                _write_batch_sync(db_path, list(batch), now)
                total_written += len(batch)
                print(f"[SCANNER] Written {total_written} results to DB", flush=True)
                batch.clear()

        for path in paths:
            if is_cancelled():
                print("[SCANNER] Scan cancelled by user", flush=True)
                break
            try:
                await scan_directory(
                    path,
                    progress_callback=progress_cb,
                    result_callback=result_cb,
                    cancel_check=is_cancelled,
                )
            except Exception as exc:
                print(f"[SCANNER] Error scanning {path}: {exc}", flush=True)
                import traceback; traceback.print_exc()

        # Flush remaining batch
        if batch:
            _write_batch_sync(db_path, list(batch), now)
            total_written += len(batch)
            print(f"[SCANNER] Written {total_written} results to DB (final batch)", flush=True)

        # Restore converted flags
        if not is_cancelled():
            try:
                db = sqlite3.connect(db_path)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=60000")
                try:
                    cur = db.execute(
                        """UPDATE scan_results SET converted = 1
                           WHERE converted = 0 AND (
                               file_path IN (
                                   SELECT file_path FROM jobs
                                   WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0
                               )
                               OR file_path IN (
                                   SELECT original_file_path FROM jobs
                                   WHERE status = 'completed' AND job_type IN ('convert', 'combined')
                                   AND original_file_path IS NOT NULL AND space_saved > 0
                               )
                           )"""
                    )
                    if cur.rowcount > 0:
                        db.commit()
                        print(f"[SCANNER] Restored 'converted' flag on {cur.rowcount} files", flush=True)
                finally:
                    db.close()
            except Exception as exc:
                print(f"[SCANNER] Failed to restore converted flags: {exc}", flush=True)

        # Detect duplicates — multiple files in the same folder (e.g. 4K + 1080p of same movie)
        if not is_cancelled():
            try:
                db = sqlite3.connect(db_path)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA busy_timeout=60000")
                try:
                    # Reset all dup counts
                    db.execute("UPDATE scan_results SET dup_count = 0, dup_group = NULL WHERE removed_from_list = 0")

                    # Find folders with multiple files (potential duplicates)
                    # Group by parent folder — if a movie folder has 2+ video files, they're duplicates
                    rows = db.execute(
                        """SELECT file_path FROM scan_results
                           WHERE removed_from_list = 0
                             AND file_path NOT LIKE '%.converting.%'
                             AND file_path NOT LIKE '%.remuxing.%'"""
                    ).fetchall()

                    from collections import defaultdict
                    folder_files = defaultdict(list)
                    for (fp,) in rows:
                        # Get the title-level folder (one with media ID) or direct parent
                        parts = fp.split("/")
                        parent = "/".join(parts[:-1])
                        folder_files[parent].append(fp)

                    dup_count = 0
                    for folder, files in folder_files.items():
                        if len(files) > 1:
                            # Check if these are actually different versions of the same content
                            # (not just episodes in a season folder)
                            folder_name = folder.split("/")[-1] if "/" in folder else folder
                            is_season = folder_name.lower().startswith("season") or folder_name.lower().startswith("specials")

                            # Also treat as episodic if files have S##E## patterns (episodes without Season subfolder)
                            import re as _re_dup
                            has_episodes = any(_re_dup.search(r'[Ss]\d+[Ee]\d+', fp.split("/")[-1]) for fp in files)

                            if is_season or has_episodes:
                                # For season/episode folders, detect episode duplicates (same episode, different quality)
                                ep_groups = defaultdict(list)
                                for fp in files:
                                    fname = fp.split("/")[-1]
                                    ep_match = _re_dup.search(r'[Ss]\d+[Ee](\d+)', fname)
                                    ep_key = ep_match.group(1) if ep_match else fname
                                    ep_groups[ep_key].append(fp)
                                for ep_key, ep_files in ep_groups.items():
                                    if len(ep_files) > 1:
                                        group_id = f"ep:{folder}/{ep_key}"
                                        for fp in ep_files:
                                            db.execute(
                                                "UPDATE scan_results SET dup_count = ?, dup_group = ? WHERE file_path = ?",
                                                (len(ep_files), group_id, fp)
                                            )
                                            dup_count += 1
                            else:
                                # For movie/non-season folders, all files are duplicates of each other
                                group_id = f"folder:{folder}"
                                for fp in files:
                                    db.execute(
                                        "UPDATE scan_results SET dup_count = ?, dup_group = ? WHERE file_path = ?",
                                        (len(files), group_id, fp)
                                    )
                                    dup_count += 1

                    if dup_count > 0:
                        db.commit()
                        print(f"[SCANNER] Detected {dup_count} duplicate files", flush=True)
                finally:
                    db.close()
            except Exception as exc:
                print(f"[SCANNER] Duplicate detection failed: {exc}", flush=True)

        write_progress("done" if not is_cancelled() else "cancelled", "", total_written, total_written)

    _asyncio.run(_do_scan())


async def _run_scan(paths: list[str]) -> None:
    """Launch scan in a subprocess and poll progress for websocket updates."""
    global _scan_proc
    import multiprocessing
    import os

    _scan_cancel.clear()

    # Remove stale files
    for f in [_scan_progress_file, _scan_cancel_file]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    # Start scan in a separate process
    proc = multiprocessing.Process(
        target=_scan_worker_process,
        args=(paths, DB_PATH, _scan_progress_file, _scan_cancel_file),
        daemon=True,
    )
    proc.start()
    _scan_proc = proc
    print(f"[SCANNER] Started scan subprocess pid={proc.pid}", flush=True)

    # Poll progress file and forward to websocket
    import os
    last_progress = {}
    try:
        while proc.is_alive():
            if _scan_cancel.is_set():
                # Signal the subprocess to stop
                with open(_scan_cancel_file, "w") as f:
                    f.write("cancel")
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.kill()
                break

            # Read progress
            try:
                if os.path.exists(_scan_progress_file):
                    with open(_scan_progress_file, "r") as f:
                        progress = json.load(f)
                    if progress != last_progress:
                        await ws_manager.send_scan_progress(
                            status=progress.get("status", "scanning"),
                            current_file=progress.get("current_file", ""),
                            total=progress.get("total", 0),
                            probed=progress.get("probed", 0),
                        )
                        last_progress = progress
            except (json.JSONDecodeError, FileNotFoundError):
                pass

            await asyncio.sleep(0.5)

        # Process finished — read final progress
        try:
            if os.path.exists(_scan_progress_file):
                with open(_scan_progress_file, "r") as f:
                    progress = json.load(f)
                await ws_manager.send_scan_progress(
                    status=progress.get("status", "done"),
                    current_file="",
                    total=progress.get("total", 0),
                    probed=progress.get("probed", 0),
                )
        except Exception:
            await ws_manager.send_scan_progress(status="done", current_file="", total=0, probed=0)

        # Auto-sync Plex watch status after scan completes
        try:
            from backend.plex import sync_plex_metadata_cache
            result = await sync_plex_metadata_cache()
            if result.get("watched") or result.get("unwatched"):
                print(f"[SCANNER] Plex watch status synced: {result.get('watched', 0)} watched, {result.get('unwatched', 0)} unwatched", flush=True)
        except Exception as exc:
            print(f"[SCANNER] Plex watch status sync skipped: {exc}", flush=True)

        # Auto-start poster prefetch after scan
        try:
            from backend.routes.posters import start_prefetch
            await start_prefetch()
            print(f"[SCANNER] Poster prefetch started", flush=True)
        except Exception as exc:
            print(f"[SCANNER] Poster prefetch skipped: {exc}", flush=True)

        # Auto health-check newly-scanned files inline (NOT via the conversion queue)
        try:
            db_hc = await connect_db()
            try:
                async with db_hc.execute(
                    "SELECT value FROM settings WHERE key = 'health_check_on_scan'"
                ) as cur:
                    row = await cur.fetchone()
                    raw = (str(row["value"]).lower() if row else "off")
                    hc_mode = {"true": "quick", "false": "off"}.get(raw, raw)
                    if hc_mode not in ("quick", "thorough"):
                        hc_mode = "off"
                unchecked: list[str] = []
                if hc_mode != "off":
                    # Only check files DETECTED in the last 24h, capped for safety.
                    HC_BATCH_CAP = 2000
                    async with db_hc.execute(
                        "SELECT file_path FROM scan_results "
                        "WHERE removed_from_list = 0 AND health_status IS NULL "
                        "AND COALESCE(probe_status, 'ok') = 'ok' "
                        "AND new_detected_at IS NOT NULL "
                        "AND new_detected_at > datetime('now', '-1 day') "
                        "ORDER BY new_detected_at DESC LIMIT ?",
                        (HC_BATCH_CAP,),
                    ) as cur:
                        unchecked = [r["file_path"] for r in await cur.fetchall()]
            finally:
                await db_hc.close()

            if hc_mode != "off" and unchecked:
                from backend.health_check import run_check
                from backend.file_events import log_event, EVENT_HEALTH_CHECK
                from datetime import datetime, timezone
                total = len(unchecked)
                print(f"[SCANNER] Running inline {hc_mode} health check on {total} new file(s)", flush=True)
                # Open one DB connection for the whole pass
                hc_db = await connect_db()
                try:
                    for idx, fp in enumerate(unchecked):
                        # Respect scan cancel
                        if os.path.exists(_scan_cancel_file):
                            print("[SCANNER] Health-check phase cancelled", flush=True)
                            break
                        # Stream progress on the same scan_progress channel
                        await ws_manager.send_scan_progress(
                            status=f"health_check_{hc_mode}",
                            current_file=fp,
                            total=total,
                            probed=idx,
                        )
                        try:
                            result = await run_check(fp, mode=hc_mode)
                        except Exception as exc:
                            print(f"[SCANNER] Health check error on {fp}: {exc}", flush=True)
                            continue
                        status = result.get("status", "healthy")
                        errors = result.get("errors", [])
                        now_iso = datetime.now(timezone.utc).isoformat()
                        try:
                            await hc_db.execute(
                                "UPDATE scan_results SET health_status = ?, health_errors_json = ?, "
                                "health_checked_at = ?, health_check_type = ? WHERE file_path = ?",
                                (
                                    status,
                                    json.dumps(errors) if errors else None,
                                    now_iso,
                                    hc_mode,
                                    fp,
                                ),
                            )
                            await hc_db.commit()
                        except Exception as exc:
                            print(f"[SCANNER] Failed to persist health status for {fp}: {exc}", flush=True)
                        # Only log corrupt files to the Activity feed — healthy ones are noise
                        if status == "corrupt":
                            try:
                                await log_event(
                                    fp, EVENT_HEALTH_CHECK,
                                    f"Health check: corrupt ({hc_mode})",
                                    {
                                        "status": status, "check_type": hc_mode,
                                        "duration_seconds": result.get("duration_seconds"),
                                        "errors": errors[:5] if errors else None,
                                    },
                                )
                            except Exception:
                                pass
                    # Final progress ping
                    await ws_manager.send_scan_progress(
                        status="health_check_complete",
                        current_file="",
                        total=total,
                        probed=total,
                    )
                    print(f"[SCANNER] Health-check phase complete ({total} file(s))", flush=True)
                finally:
                    await hc_db.close()
        except Exception as exc:
            print(f"[SCANNER] Inline health-check skipped: {exc}", flush=True)

    except asyncio.CancelledError:
        with open(_scan_cancel_file, "w") as f:
            f.write("cancel")
        proc.join(timeout=10)
        if proc.is_alive():
            proc.kill()
        await ws_manager.send_scan_progress(status="cancelled", current_file="", total=0, probed=0)
    except Exception as exc:
        print(f"[SCANNER] Error monitoring scan: {exc}", flush=True)
    finally:
        _scan_proc = None
        global _scan_task
        _scan_task = None
        # Cleanup temp files
        for f in [_scan_progress_file, _scan_cancel_file]:
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass


@router.post("/start")
async def start_scan(request: ScanRequest):
    global _scan_task
    if _scan_task and not _scan_task.done():
        raise HTTPException(status_code=409, detail="Scan already in progress")
    _scan_task = asyncio.create_task(_run_scan(request.paths))
    return {"status": "started", "paths": request.paths}


@router.post("/cancel")
async def cancel_scan():
    global _scan_task
    if _scan_task is None or _scan_task.done():
        return {"status": "no_scan_running"}
    _scan_cancel.set()
    # Also cancel the asyncio task to interrupt any in-flight awaits (metadata lookups, probes)
    _scan_task.cancel()
    return {"status": "cancelling"}


@router.post("/cleanup-temp")
async def cleanup_temp_scan_results():
    """Remove .converting.mkv and .remuxing.mkv entries from scan_results."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        result = await db.execute(
            "DELETE FROM scan_results WHERE file_path LIKE '%.converting.%' OR file_path LIKE '%.remuxing.%'"
        )
        await db.commit()
        return {"status": "cleaned", "removed": result.rowcount}
    finally:
        await db.close()


@router.get("/status")
async def scan_status():
    return {"scanning": _scan_task is not None and not _scan_task.done()}


@router.get("/new-count")
async def new_file_count(request: Request):
    """Get count of new files found by the watcher since last scanner visit."""
    watcher = getattr(request.app.state, "watcher", None)
    if watcher is None:
        return {"count": 0}
    return {"count": watcher.new_files_count}


@router.post("/clear-new")
async def clear_new_count(request: Request):
    """Clear the nav badge counter (called when user visits scanner page).

    Does NOT clear new_detected_at in DB — files stay in the "New" filter
    until they age out after 24 hours.
    """
    watcher = getattr(request.app.state, "watcher", None)
    if watcher:
        watcher.clear_new_count()
    return {"status": "cleared"}


@router.get("/scan-stats")
async def get_scan_stats():
    """Lightweight endpoint returning all filter counts + summary stats server-side.

    Replaces 2.37M frontend array iterations with a single SQL query.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        LOW_BR = 3_000_000

        # Main counts via SQL (pre-computed flags avoid JSON parsing)
        async with db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(needs_conversion) as needs_conversion_raw,
                SUM(has_removable_tracks_flag) as audio_cleanup,
                SUM(has_removable_subs_flag) as sub_cleanup,
                SUM(has_lossless_audio_flag) as lossless_audio,
                SUM(converted) as converted,
                SUM(CASE WHEN dup_count > 1 THEN 1 ELSE 0 END) as duplicates,
                SUM(CASE WHEN COALESCE(probe_status, 'ok') != 'ok' OR health_status = 'corrupt' THEN 1 ELSE 0 END) as corrupt,
                SUM(CASE WHEN video_height >= 2000 THEN 1 ELSE 0 END) as res_4k,
                SUM(CASE WHEN video_height >= 900 AND video_height < 2000 THEN 1 ELSE 0 END) as res_1080p,
                SUM(CASE WHEN video_height >= 600 AND video_height < 900 THEN 1 ELSE 0 END) as res_720p,
                SUM(CASE WHEN video_height > 0 AND video_height < 600 THEN 1 ELSE 0 END) as res_sd_probed,
                SUM(CASE WHEN video_codec LIKE '%264%' OR video_codec LIKE '%avc%' THEN 1 ELSE 0 END) as x264,
                SUM(CASE WHEN video_codec LIKE '%265%' OR video_codec LIKE '%hevc%' THEN 1 ELSE 0 END) as x265,
                SUM(CASE WHEN video_codec LIKE '%av1%' THEN 1 ELSE 0 END) as av1,
                SUM(CASE WHEN new_detected_at > ? THEN 1 ELSE 0 END) as new_count,
                SUM(CASE WHEN file_size > 10737418240 THEN 1 ELSE 0 END) as large_files,
                SUM(file_size) as total_size,
                SUM(CASE WHEN vmaf_score IS NOT NULL AND vmaf_score >= 93 THEN 1 ELSE 0 END) as vmaf_excellent,
                SUM(CASE WHEN vmaf_score IS NOT NULL AND vmaf_score >= 87 AND vmaf_score < 93 THEN 1 ELSE 0 END) as vmaf_good,
                SUM(CASE WHEN vmaf_score IS NOT NULL AND vmaf_score < 87 THEN 1 ELSE 0 END) as vmaf_poor,
                SUM(CASE WHEN converted = 1 AND vmaf_score IS NULL THEN 1 ELSE 0 END) as vmaf_pending
            FROM scan_results WHERE removed_from_list = 0
            AND file_path NOT LIKE '%%.converting.%%'
            AND file_path NOT LIKE '%%.remuxing.%%'""",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
        ) as cur:
            row = dict(await cur.fetchone())

        total = row["total"] or 0
        x264 = row["x264"] or 0
        x265 = row["x265"] or 0
        av1 = row["av1"] or 0

        # Converted count from jobs table (same logic as dashboard)
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0"
        ) as cur:
            converted_from_jobs = (await cur.fetchone())["cnt"] or 0

        # Counts that need Python-side computation (ignored, queued, bitrate-based)
        ignored_count = 0
        queued_count = 0
        needs_conversion_count = 0
        high_bitrate_count = 0
        low_bitrate_count = 0
        recent_count = 0
        watched_count = 0
        unwatched_count = 0
        watchlist_count = 0
        estimated_savings = 0
        res_sd_fallback = 0
        size_small_count = 0
        size_medium_count = 0
        size_large_count = 0
        src_remux_count = 0
        src_bluray_count = 0
        src_webdl_count = 0
        src_hdtv_count = 0
        src_dvd_count = 0
        type_movie_count = 0
        type_tv_count = 0
        type_other_count = 0

        # Load prefix data for ignore/watch checks
        import bisect
        ignored_paths: set[str] = set()
        ignored_folders_raw: list[str] = []
        rule_exempt_paths: set[str] = set()
        async with db.execute("SELECT file_path, reason FROM ignored_files") as cur:
            for r in await cur.fetchall():
                p = r["file_path"]
                reason = r["reason"] or ""
                if reason in ("plex_label_exempt", "rule_exempt"):
                    rule_exempt_paths.add(p)
                    continue
                ignored_paths.add(p)
                if p.endswith("/"):
                    ignored_folders_raw.append(p)
        ignored_folders_sorted = sorted(set(ignored_folders_raw))

        skip_prefixes_sorted: list[str] = []
        try:
            from backend.rule_resolver import get_skip_prefixes
            raw_pf = await get_skip_prefixes()
            if raw_pf:
                skip_prefixes_sorted = sorted(set(raw_pf))
        except Exception:
            pass

        queued_paths: set[str] = set()
        async with db.execute("SELECT file_path FROM jobs WHERE status IN ('pending', 'running')") as cur:
            queued_paths = {r["file_path"] for r in await cur.fetchall()}

        watched_sorted: list[str] = []
        unwatched_sorted: list[str] = []
        watchlist_sorted: list[str] = []
        try:
            async with db.execute("SELECT folder_path, metadata_value FROM plex_metadata_cache WHERE metadata_type='watch_status'") as cur:
                for r in await cur.fetchall():
                    if r["metadata_value"] == "watched":
                        watched_sorted.append(r["folder_path"])
                    elif r["metadata_value"] == "watchlist":
                        watchlist_sorted.append(r["folder_path"])
                    else:
                        unwatched_sorted.append(r["folder_path"])
            watched_sorted.sort()
            unwatched_sorted.sort()
            watchlist_sorted.sort()
        except Exception:
            pass

        # Get CQ for savings estimation
        async with db.execute("SELECT value FROM settings WHERE key='nvenc_cq'") as cur:
            cq_row = await cur.fetchone()
            cq_val = int(cq_row["value"]) if cq_row else 20
        if cq_val <= 15: est_pct = 0.10
        elif cq_val <= 18: est_pct = 0.15
        elif cq_val <= 20: est_pct = 0.25
        elif cq_val <= 22: est_pct = 0.35
        elif cq_val <= 24: est_pct = 0.45
        elif cq_val <= 26: est_pct = 0.55
        elif cq_val <= 28: est_pct = 0.60
        else: est_pct = 0.65

        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_24h = now_ts - 86400

        import re as _re_mod
        re_src = _re_mod.compile(r"blu[\-\s]?ray|bdremux|bdrip|bdmv", _re_mod.IGNORECASE)
        # Single pass through file paths for prefix-based counts
        async with db.execute(
            "SELECT file_path, file_size, duration, needs_conversion, video_height, file_mtime "
            "FROM scan_results WHERE removed_from_list = 0 "
            "AND file_path NOT LIKE '%%.converting.%%' AND file_path NOT LIKE '%%.remuxing.%%'"
        ) as cur:
            async for r in cur:
                fp = r["file_path"]
                sz = r["file_size"] or 0
                dur = r["duration"] or 0

                # Ignored check
                is_ignored = fp in ignored_paths
                if not is_ignored and ignored_folders_sorted:
                    idx = bisect.bisect_right(ignored_folders_sorted, fp) - 1
                    if idx >= 0 and fp.startswith(ignored_folders_sorted[idx]):
                        is_ignored = True
                if not is_ignored:
                    is_exempt = fp in rule_exempt_paths
                    if not is_exempt:
                        parent = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
                        while parent and not is_exempt:
                            if parent in rule_exempt_paths:
                                is_exempt = True
                            elif "/" in parent.rstrip("/"):
                                parent = parent.rstrip("/").rsplit("/", 1)[0] + "/"
                            else:
                                break
                    if not is_exempt and skip_prefixes_sorted:
                        idx = bisect.bisect_right(skip_prefixes_sorted, fp) - 1
                        if idx >= 0 and fp.startswith(skip_prefixes_sorted[idx]):
                            is_ignored = True

                if is_ignored:
                    ignored_count += 1

                if fp in queued_paths:
                    queued_count += 1

                # Bitrate-based counts
                bitrate = (sz * 8 / dur) if dur > 0 else 0
                low_br = dur > 0 and bitrate < LOW_BR
                high_br = r["needs_conversion"] and not is_ignored and dur > 0 and bitrate > 15_000_000

                if r["needs_conversion"] and not low_br and not is_ignored:
                    needs_conversion_count += 1
                    estimated_savings += int(sz * est_pct)
                if low_br and not is_ignored:
                    low_bitrate_count += 1
                if high_br:
                    high_bitrate_count += 1

                # Recent
                mtime = r["file_mtime"]
                if mtime and mtime > cutoff_24h:
                    recent_count += 1

                # Resolution fallback for files without video_height
                vh = r["video_height"] or 0
                if vh == 0:
                    fn = fp.lower()
                    if not ("2160p" in fn or "4k" in fn or "uhd" in fn or "1080" in fn or "720p" in fn):
                        res_sd_fallback += 1

                # Watch status
                if watched_sorted:
                    idx = bisect.bisect_right(watched_sorted, fp) - 1
                    if idx >= 0 and fp.startswith(watched_sorted[idx]):
                        watched_count += 1
                if unwatched_sorted:
                    idx = bisect.bisect_right(unwatched_sorted, fp) - 1
                    if idx >= 0 and fp.startswith(unwatched_sorted[idx]):
                        unwatched_count += 1
                if watchlist_sorted:
                    idx = bisect.bisect_right(watchlist_sorted, fp) - 1
                    if idx >= 0 and fp.startswith(watchlist_sorted[idx]):
                        watchlist_count += 1

                # Size buckets
                if sz < 5 * (1024 ** 3): size_small_count += 1
                elif sz <= 10 * (1024 ** 3): size_medium_count += 1
                else: size_large_count += 1

                # Source detection
                fn = fp.lower()
                if "remux" in fn: src_remux_count += 1
                elif re_src.search(fn): src_bluray_count += 1
                elif "web-dl" in fn or "webdl" in fn or "webrip" in fn: src_webdl_count += 1
                elif "hdtv" in fn: src_hdtv_count += 1
                elif "dvd" in fn: src_dvd_count += 1

                # Type detection
                if "[tvdb-" in fn:
                    type_tv_count += 1
                elif "[tt" in fn:
                    type_movie_count += 1
                else:
                    type_other_count += 1

        return {
            "counts": {
                "all": total,
                "new": row["new_count"] or 0,
                "needs_conversion": needs_conversion_count,
                "large_files": row["large_files"] or 0,
                "high_bitrate": high_bitrate_count,
                "low_bitrate": low_bitrate_count,
                "sub_cleanup": row["sub_cleanup"] or 0,
                "ignored": ignored_count,
                "duplicates": row["duplicates"] or 0,
                "corrupt": row["corrupt"] or 0,
                "recent": recent_count,
                "converted": converted_from_jobs,
                "queued": queued_count,
                "x264": x264,
                "x265": x265,
                "av1": av1,
                "misc_codec": total - x264 - x265 - av1,
                "res_4k": row["res_4k"] or 0,
                "res_1080p": row["res_1080p"] or 0,
                "res_720p": row["res_720p"] or 0,
                "res_sd": (row["res_sd_probed"] or 0) + res_sd_fallback,
                "audio_cleanup": row["audio_cleanup"] or 0,
                "lossless_audio": row["lossless_audio"] or 0,
                "lossy_audio": total - (row["lossless_audio"] or 0),
                "plex_watched": watched_count,
                "plex_unwatched": unwatched_count,
                "plex_watchlist": watchlist_count,
                "vmaf_excellent": row["vmaf_excellent"] or 0,
                "vmaf_good": row["vmaf_good"] or 0,
                "vmaf_poor": row["vmaf_poor"] or 0,
                "size_small": size_small_count,
                "size_medium": size_medium_count,
                "size_large": size_large_count,
                "src_remux": src_remux_count,
                "src_bluray": src_bluray_count,
                "src_webdl": src_webdl_count,
                "src_hdtv": src_hdtv_count,
                "src_dvd": src_dvd_count,
                "type_movie": type_movie_count,
                "type_tv": type_tv_count,
                "type_other": type_other_count,
            },
            "summary": {
                "files_to_convert": needs_conversion_count,
                "audio_cleanup": row["audio_cleanup"] or 0,
                "ignored_count": ignored_count,
                "estimated_savings_bytes": estimated_savings,
                "total_size": row["total_size"] or 0,
            },
        }
    finally:
        await db.close()


async def _build_enrichment_context(db) -> dict:
    """Build shared context for enriching scan results (used by results, tree, files endpoints)."""
    import bisect
    from datetime import datetime, timedelta, timezone

    LOW_BITRATE_THRESHOLD = 3_000_000  # 3 Mbps
    HIGH_BITRATE_THRESHOLD = 15_000_000  # 15 Mbps

    # Ignored paths/folders
    ignored_paths: set[str] = set()
    ignored_folders_raw: list[str] = []
    rule_exempt_paths: set[str] = set()
    async with db.execute("SELECT file_path, reason FROM ignored_files") as cur:
        for r in await cur.fetchall():
            p = r["file_path"]
            reason = r["reason"] or ""
            if reason in ("plex_label_exempt", "rule_exempt"):
                rule_exempt_paths.add(p)
                continue
            ignored_paths.add(p)
            if p.endswith("/"):
                ignored_folders_raw.append(p)
    ignored_folders_sorted = sorted(set(ignored_folders_raw))

    # Rule-based skip prefixes
    skip_prefixes_sorted: list[str] = []
    try:
        from backend.rule_resolver import get_skip_prefixes
        raw_pf = await get_skip_prefixes()
        if raw_pf:
            skip_prefixes_sorted = sorted(set(raw_pf))
    except Exception:
        pass

    # Queued file paths
    queued_paths: set[str] = set()
    async with db.execute("SELECT file_path FROM jobs WHERE status IN ('pending', 'running')") as cur:
        queued_paths = {r["file_path"] for r in await cur.fetchall()}

    # Converted: collect both exact paths and parent folders from jobs with savings
    converted_paths: set[str] = set()
    converted_folders: set[str] = set()
    async with db.execute(
        "SELECT file_path, original_file_path FROM jobs WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0"
    ) as cur:
        for r in await cur.fetchall():
            fp = r["file_path"]
            converted_paths.add(fp)
            converted_folders.add(fp.rsplit("/", 1)[0] + "/" if "/" in fp else "")
            if r["original_file_path"]:
                ofp = r["original_file_path"]
                converted_paths.add(ofp)
                converted_folders.add(ofp.rsplit("/", 1)[0] + "/" if "/" in ofp else "")

    # Plex watch status
    watched_sorted: list[str] = []
    unwatched_sorted: list[str] = []
    watchlist_sorted: list[str] = []
    try:
        async with db.execute(
            "SELECT folder_path, metadata_value FROM plex_metadata_cache WHERE metadata_type='watch_status'"
        ) as cur:
            for r in await cur.fetchall():
                if r["metadata_value"] == "watched":
                    watched_sorted.append(r["folder_path"])
                elif r["metadata_value"] == "watchlist":
                    watchlist_sorted.append(r["folder_path"])
                else:
                    unwatched_sorted.append(r["folder_path"])
        watched_sorted.sort()
        unwatched_sorted.sort()
        watchlist_sorted.sort()
    except Exception:
        pass

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    return {
        "ignored_paths": ignored_paths,
        "ignored_folders_sorted": ignored_folders_sorted,
        "rule_exempt_paths": rule_exempt_paths,
        "skip_prefixes_sorted": skip_prefixes_sorted,
        "queued_paths": queued_paths,
        "converted_paths": converted_paths,
        "converted_folders": converted_folders,
        "watched_sorted": watched_sorted,
        "unwatched_sorted": unwatched_sorted,
        "watchlist_sorted": watchlist_sorted,
        "cutoff_24h": cutoff_24h,
        "LOW_BITRATE_THRESHOLD": LOW_BITRATE_THRESHOLD,
        "HIGH_BITRATE_THRESHOLD": HIGH_BITRATE_THRESHOLD,
    }


def _check_ignored(fp: str, ctx: dict) -> bool:
    """Check if a file path is ignored (manual, folder-level, or rule-based)."""
    import bisect
    if fp in ctx["ignored_paths"]:
        return True
    ifs = ctx["ignored_folders_sorted"]
    if ifs:
        idx = bisect.bisect_right(ifs, fp) - 1
        if idx >= 0 and fp.startswith(ifs[idx]):
            return True
    # Check rule exemption before skip prefixes
    is_exempt = fp in ctx["rule_exempt_paths"]
    if not is_exempt:
        parent = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
        while parent and not is_exempt:
            if parent in ctx["rule_exempt_paths"]:
                is_exempt = True
            elif "/" in parent.rstrip("/"):
                parent = parent.rstrip("/").rsplit("/", 1)[0] + "/"
            else:
                break
    if not is_exempt:
        sps = ctx["skip_prefixes_sorted"]
        if sps:
            idx = bisect.bisect_right(sps, fp) - 1
            if idx >= 0 and fp.startswith(sps[idx]):
                return True
    return False


def _get_watch_status(fp: str, ctx: dict) -> str | None:
    """Get Plex watch status via prefix matching."""
    import bisect
    ws = ctx["watched_sorted"]
    if ws:
        idx = bisect.bisect_right(ws, fp) - 1
        if idx >= 0 and fp.startswith(ws[idx]):
            return "watched"
    us = ctx["unwatched_sorted"]
    if us:
        idx = bisect.bisect_right(us, fp) - 1
        if idx >= 0 and fp.startswith(us[idx]):
            return "unwatched"
    wl = ctx.get("watchlist_sorted", [])
    if wl:
        idx = bisect.bisect_right(wl, fp) - 1
        if idx >= 0 and fp.startswith(wl[idx]):
            return "watchlist"
    return None


def _enrich_row_minimal(row: dict, ctx: dict) -> dict:
    """Like _enrich_row but skips expensive json.loads on audio/subtitle track JSON.

    Use this when you only need the filter-relevant fields (no track lists), e.g.
    when resolving folder selections into file paths before queueing/estimating.
    """
    fp = row["file_path"]
    sz = row["file_size"] or 0
    dur = row["duration"] or 0

    is_ignored = _check_ignored(fp, ctx)
    bitrate = (sz * 8 / dur) if dur > 0 else 0
    low_bitrate = bool(row.get("needs_conversion") and dur > 0 and bitrate < ctx["LOW_BITRATE_THRESHOLD"])

    detected_at = row.get("new_detected_at")

    return {
        "id": row["id"],
        "file_path": fp,
        "file_size": sz,
        "video_codec": row.get("video_codec"),
        "needs_conversion": bool(row.get("needs_conversion")),
        "native_language": row.get("native_language"),
        "has_removable_tracks": bool(row.get("has_removable_tracks")),
        "has_removable_subs": bool(row.get("has_removable_subs")),
        "has_lossless_audio": bool(row.get("has_lossless_audio")),
        "ignored": is_ignored,
        "is_new": bool(detected_at and detected_at > ctx["cutoff_24h"]),
        "queued": fp in ctx["queued_paths"],
        "converted": fp in ctx["converted_paths"] or (
            not row.get("needs_conversion") and
            (fp.rsplit("/", 1)[0] + "/" if "/" in fp else "") in ctx["converted_folders"]
        ),
        "low_bitrate": low_bitrate,
        "duration": dur,
        "file_mtime": row.get("file_mtime"),
        "probe_status": row.get("probe_status", "ok"),
        "video_height": row.get("video_height", 0),
        "plex_watch_status": _get_watch_status(fp, ctx),
        "duplicate_count": row.get("duplicate_count", 0),
        "duplicate_group": row.get("duplicate_group"),
        "vmaf_score": row.get("vmaf_score"),
        "language_source": row.get("language_source", "heuristic"),
        "health_status": row.get("health_status"),
        "health_check_type": row.get("health_check_type"),
        "health_checked_at": row.get("health_checked_at"),
    }


def _enrich_row(row: dict, ctx: dict) -> dict:
    """Enrich a scan_results row with computed fields (ignored, queued, watch status, etc.)."""
    fp = row["file_path"]
    sz = row["file_size"] or 0
    dur = row["duration"] or 0

    is_ignored = _check_ignored(fp, ctx)
    bitrate = (sz * 8 / dur) if dur > 0 else 0
    low_bitrate = bool(row.get("needs_conversion") and dur > 0 and bitrate < ctx["LOW_BITRATE_THRESHOLD"])

    detected_at = row.get("new_detected_at")

    return {
        "id": row["id"],
        "file_path": fp,
        "file_size": sz,
        "video_codec": row.get("video_codec"),
        "needs_conversion": bool(row.get("needs_conversion")),
        "native_language": row.get("native_language"),
        "has_removable_tracks": bool(row.get("has_removable_tracks")),
        "has_removable_subs": bool(row.get("has_removable_subs")),
        "has_lossless_audio": bool(row.get("has_lossless_audio")),
        "ignored": is_ignored,
        "is_new": bool(detected_at and detected_at > ctx["cutoff_24h"]),
        "queued": fp in ctx["queued_paths"],
        "converted": fp in ctx["converted_paths"] or (
            not row.get("needs_conversion") and
            (fp.rsplit("/", 1)[0] + "/" if "/" in fp else "") in ctx["converted_folders"]
        ),
        "low_bitrate": low_bitrate,
        "duration": dur,
        "file_mtime": row.get("file_mtime"),
        "probe_status": row.get("probe_status", "ok"),
        "video_height": row.get("video_height", 0),
        "plex_watch_status": _get_watch_status(fp, ctx),
        "duplicate_count": row.get("duplicate_count", 0),
        "duplicate_group": row.get("duplicate_group"),
        "vmaf_score": row.get("vmaf_score"),
        "audio_tracks": json.loads(row.get("audio_tracks_json") or "[]"),
        "subtitle_tracks": json.loads(row.get("subtitle_tracks_json") or "[]"),
        "language_source": row.get("language_source", "heuristic"),
    }


# Standard columns used by tree/files/results endpoints
_SCAN_SELECT_COLS = """id, file_path, file_size, video_codec, needs_conversion,
    native_language, language_source, new_detected_at, converted, file_mtime, duration,
    audio_tracks_json, subtitle_tracks_json,
    COALESCE(probe_status, 'ok') as probe_status,
    COALESCE(video_height, 0) as video_height,
    COALESCE(has_removable_tracks_flag, 0) as has_removable_tracks,
    COALESCE(has_removable_subs_flag, 0) as has_removable_subs,
    COALESCE(has_lossless_audio_flag, 0) as has_lossless_audio,
    vmaf_score,
    health_status, health_check_type, health_checked_at,
    COALESCE(dup_count, 0) as duplicate_count,
    dup_group as duplicate_group"""

_SCAN_WHERE = """removed_from_list = 0
    AND file_path NOT LIKE '%%.converting.%%'
    AND file_path NOT LIKE '%%.remuxing.%%'
    AND file_path NOT LIKE '%%/._%%'"""


def _matches_filter(enriched: dict, filter_name: str) -> bool:
    """Check if an enriched file matches a given filter (supports comma-separated AND logic)."""
    if filter_name == "all":
        return True
    # Multi-filter: comma-separated = AND logic (file must match ALL filters)
    if "," in filter_name:
        return all(_matches_single_filter(enriched, f.strip()) for f in filter_name.split(","))
    return _matches_single_filter(enriched, filter_name)


def _matches_single_filter(enriched: dict, filter_name: str) -> bool:
    """Check if an enriched file matches a single filter."""
    if filter_name == "all":
        return True
    f = enriched
    vc = (f.get("video_codec") or "").lower()
    vh = f.get("video_height", 0) or 0
    HIGH_BR = 15_000_000
    if filter_name == "new":
        return f["is_new"]
    if filter_name == "needs_conversion":
        return f["needs_conversion"] and not f["low_bitrate"] and not f["ignored"]
    if filter_name == "high_bitrate":
        dur = f.get("duration", 0) or 0
        return f["needs_conversion"] and not f["ignored"] and dur > 0 and (f["file_size"] * 8 / dur) > HIGH_BR
    if filter_name == "low_bitrate":
        return f["low_bitrate"] and not f["ignored"]
    if filter_name == "audio_cleanup":
        return f["has_removable_tracks"] and not f["ignored"]
    if filter_name == "sub_cleanup":
        return f["has_removable_subs"] and not f["ignored"]
    if filter_name == "ignored":
        return f["ignored"]
    if filter_name == "converted":
        return f["converted"]
    if filter_name == "queued":
        return f["queued"]
    if filter_name == "x264":
        return "264" in vc or "avc" in vc
    if filter_name == "x265":
        return "265" in vc or "hevc" in vc
    if filter_name == "av1":
        return "av1" in vc
    if filter_name == "misc_codec":
        return not ("264" in vc or "avc" in vc or "265" in vc or "hevc" in vc or "av1" in vc)
    if filter_name == "lossless_audio":
        return f["has_lossless_audio"]
    if filter_name == "lossy_audio":
        return not f["has_lossless_audio"]
    if filter_name == "large_files":
        return f["file_size"] > 10 * 1024**3
    if filter_name == "duplicates":
        return (f.get("duplicate_count") or 0) > 1
    if filter_name == "corrupt":
        return f.get("probe_status", "ok") != "ok" or f.get("health_status") == "corrupt"
    if filter_name == "recent":
        mt = f.get("file_mtime")
        if mt:
            import time
            return (time.time() - mt) < 86400
        return False
    if filter_name == "res_4k":
        if vh >= 1400:
            return True
        if vh == 0:
            fn = f.get("file_path", "").lower()
            return "2160p" in fn or "4k" in fn or "uhd" in fn
        return False
    if filter_name == "res_1080p":
        if 900 <= vh < 1400:
            return True
        # 2.40:1 BluRays stored as 1920x800 have vh < 900 but filename says "1080p"
        fn = f.get("file_path", "").lower()
        return "1080p" in fn and vh < 1400
    if filter_name == "res_720p":
        if not (600 <= vh < 900):
            return False
        # Exclude HD-labeled files that happen to have vh < 900 due to aspect ratio
        fn = f.get("file_path", "").lower()
        return "1080p" not in fn and "2160p" not in fn and "4k" not in fn and "uhd" not in fn
    if filter_name == "res_sd":
        if not (0 < vh < 600):
            return False
        fn = f.get("file_path", "").lower()
        return ("720p" not in fn and "1080p" not in fn
                and "2160p" not in fn and "4k" not in fn and "uhd" not in fn)
    if filter_name == "plex_watched":
        return f.get("plex_watch_status") == "watched"
    if filter_name == "plex_unwatched":
        return f.get("plex_watch_status") == "unwatched"
    if filter_name == "plex_watchlist":
        return f.get("plex_watch_status") == "watchlist"
    # VMAF quality filters
    vs = f.get("vmaf_score")
    if filter_name == "vmaf_excellent":
        return vs is not None and vs >= 93
    if filter_name == "vmaf_good":
        return vs is not None and 87 <= vs < 93
    if filter_name == "vmaf_poor":
        return vs is not None and vs < 87
    # Size filters
    file_size = f.get("file_size") or 0
    if filter_name == "size_small":
        return file_size < 5 * (1024 ** 3)
    if filter_name == "size_medium":
        return 5 * (1024 ** 3) <= file_size <= 10 * (1024 ** 3)
    if filter_name == "size_large":
        return file_size > 10 * (1024 ** 3)
    # Source filters (match against file path)
    fp_lower = f.get("file_path", "").lower()
    if filter_name == "src_remux":
        return "remux" in fp_lower
    if filter_name == "src_bluray":
        import re as _re
        return bool(_re.search(r"blu[\-\s]?ray|bdrip|bdmv", fp_lower)) and "remux" not in fp_lower
    if filter_name == "src_webdl":
        return "web-dl" in fp_lower or "webdl" in fp_lower or "webrip" in fp_lower
    if filter_name == "src_hdtv":
        return "hdtv" in fp_lower
    if filter_name == "src_dvd":
        return "dvd" in fp_lower
    # Type filters — detect from folder structure
    if filter_name == "type_movie":
        return "[tt" in fp_lower and "[tvdb-" not in fp_lower
    if filter_name == "type_tv":
        return "[tvdb-" in fp_lower
    if filter_name == "type_other":
        return "[tt" not in fp_lower and "[tvdb-" not in fp_lower
    return True


# Filters that can be pushed into SQL WHERE clauses for the tree endpoint.
# These avoid loading+enriching every row just to discard most of them.
def _build_tree_sql_filter(filter_name: str) -> tuple[str, list, set]:
    """Build a SQL WHERE fragment for a single filter token.

    Returns (sql_fragment, params, python_filters_still_needed).
    Any filter not pushed into SQL is added to python_filters_still_needed
    and will be applied in Python after the query runs.
    """
    sql = ""
    params: list = []
    needs_python: set = set()

    f = filter_name.strip()
    if f in ("all", ""):
        return "", [], set()

    # Simple single-column filters (all have supporting indexes)
    if f == "converted":
        # Handled specially in the endpoint — requires folder set from jobs table
        needs_python = {f}
        return "", [], needs_python
    elif f == "x264":
        sql = "AND (LOWER(video_codec) LIKE '%264%' OR LOWER(video_codec) LIKE '%avc%')"
    elif f == "x265":
        sql = "AND (LOWER(video_codec) LIKE '%265%' OR LOWER(video_codec) LIKE '%hevc%')"
    elif f == "av1":
        sql = "AND LOWER(video_codec) LIKE '%av1%'"
    elif f == "misc_codec":
        sql = ("AND LOWER(video_codec) NOT LIKE '%264%' "
               "AND LOWER(video_codec) NOT LIKE '%avc%' "
               "AND LOWER(video_codec) NOT LIKE '%265%' "
               "AND LOWER(video_codec) NOT LIKE '%hevc%' "
               "AND LOWER(video_codec) NOT LIKE '%av1%'")
    elif f == "res_4k":
        # SQL handles >= 1400; fall back to filename heuristic in Python for vh=0 rows
        sql = "AND (video_height >= 1400 OR video_height IS NULL OR video_height = 0)"
        needs_python = {f}  # still refine in Python for vh=0 heuristic
    elif f == "res_1080p":
        # A file is "1080p" if either:
        #   - video_height is in the 1080p range (900–1399), OR
        #   - the filename says "1080p" and height is below 4K
        # This catches 2.40:1 BluRays stored as 1920x800.
        sql = ("AND (video_height BETWEEN 900 AND 1399 "
               "OR (LOWER(file_path) LIKE '%1080p%' AND (video_height IS NULL OR video_height < 1400)))")
    elif f == "res_720p":
        # 720p range, but EXCLUDE files whose filename clearly says 1080p/2160p/4K
        # (these are shorter-aspect HD films stored with height <900)
        sql = ("AND video_height BETWEEN 600 AND 899 "
               "AND LOWER(file_path) NOT LIKE '%1080p%' "
               "AND LOWER(file_path) NOT LIKE '%2160p%' "
               "AND LOWER(file_path) NOT LIKE '%4k%' "
               "AND LOWER(file_path) NOT LIKE '%uhd%'")
    elif f == "res_sd":
        # SD: below 720p, exclude any HD-labeled files
        sql = ("AND video_height > 0 AND video_height < 600 "
               "AND LOWER(file_path) NOT LIKE '%720p%' "
               "AND LOWER(file_path) NOT LIKE '%1080p%' "
               "AND LOWER(file_path) NOT LIKE '%2160p%' "
               "AND LOWER(file_path) NOT LIKE '%4k%' "
               "AND LOWER(file_path) NOT LIKE '%uhd%'")
    elif f == "large_files":
        sql = "AND file_size > ?"
        params.append(10 * 1024 ** 3)
    elif f == "size_small":
        sql = "AND file_size < ?"
        params.append(5 * 1024 ** 3)
    elif f == "size_medium":
        sql = "AND file_size BETWEEN ? AND ?"
        params.extend([5 * 1024 ** 3, 10 * 1024 ** 3])
    elif f == "size_large":
        sql = "AND file_size > ?"
        params.append(10 * 1024 ** 3)
    elif f == "duplicates":
        sql = "AND COALESCE(dup_count, 0) > 1"
    elif f == "lossless_audio":
        sql = "AND COALESCE(has_lossless_audio_flag, 0) = 1"
    elif f == "lossy_audio":
        sql = "AND COALESCE(has_lossless_audio_flag, 0) = 0"
    elif f == "audio_cleanup":
        sql = "AND COALESCE(has_removable_tracks_flag, 0) = 1"
        needs_python = {f}  # still need to exclude ignored
    elif f == "sub_cleanup":
        sql = "AND COALESCE(has_removable_subs_flag, 0) = 1"
        needs_python = {f}  # still need to exclude ignored
    elif f == "corrupt":
        sql = "AND (COALESCE(probe_status, 'ok') != 'ok' OR health_status = 'corrupt')"
    elif f == "recent":
        # file_mtime is a unix timestamp (seconds). 24h = 86400s.
        import time
        sql = "AND file_mtime > ?"
        params.append(time.time() - 86400)
    elif f == "vmaf_excellent":
        sql = "AND vmaf_score IS NOT NULL AND vmaf_score >= 93"
    elif f == "vmaf_good":
        sql = "AND vmaf_score IS NOT NULL AND vmaf_score >= 87 AND vmaf_score < 93"
    elif f == "vmaf_poor":
        sql = "AND vmaf_score IS NOT NULL AND vmaf_score < 87"
    elif f == "needs_conversion":
        # "needs_conversion AND NOT low_bitrate AND NOT ignored" — SQL filters the base,
        # Python removes low-bitrate + ignored exceptions
        sql = "AND needs_conversion != 0"
        needs_python = {f}
    elif f == "low_bitrate":
        # Requires duration + bitrate calc — SQL can approximate
        sql = "AND duration > 0 AND needs_conversion != 0"
        needs_python = {f}
    elif f == "high_bitrate":
        sql = "AND duration > 0 AND needs_conversion != 0"
        needs_python = {f}

    # Source filters (filename-based, case-insensitive LIKE)
    elif f == "src_remux":
        sql = "AND LOWER(file_path) LIKE '%remux%'"
    elif f == "src_bluray":
        sql = ("AND (LOWER(file_path) LIKE '%bluray%' "
               "OR LOWER(file_path) LIKE '%blu-ray%' "
               "OR LOWER(file_path) LIKE '%blu.ray%' "
               "OR LOWER(file_path) LIKE '%bdrip%' "
               "OR LOWER(file_path) LIKE '%bdmv%') "
               "AND LOWER(file_path) NOT LIKE '%remux%'")
    elif f == "src_webdl":
        sql = ("AND (LOWER(file_path) LIKE '%web-dl%' "
               "OR LOWER(file_path) LIKE '%webdl%' "
               "OR LOWER(file_path) LIKE '%webrip%')")
    elif f == "src_hdtv":
        sql = "AND LOWER(file_path) LIKE '%hdtv%'"
    elif f == "src_dvd":
        sql = "AND LOWER(file_path) LIKE '%dvd%'"

    # Type filters (detect via bracketed ID conventions in path)
    elif f == "type_movie":
        sql = "AND LOWER(file_path) LIKE '%[tt%' AND LOWER(file_path) NOT LIKE '%[tvdb-%'"
    elif f == "type_tv":
        sql = "AND LOWER(file_path) LIKE '%[tvdb-%'"
    elif f == "type_other":
        sql = "AND LOWER(file_path) NOT LIKE '%[tt%' AND LOWER(file_path) NOT LIKE '%[tvdb-%'"

    else:
        # Filters that need Python enrichment (is_new, ignored, queued, plex_*)
        needs_python = {f}

    return sql, params, needs_python


# Filters that require the expensive enrichment context (ignored/queued/plex tables)
_ENRICHMENT_FILTERS = {
    "new", "ignored", "queued", "plex_watched", "plex_unwatched", "plex_watchlist",
    "needs_conversion", "audio_cleanup", "sub_cleanup", "low_bitrate", "high_bitrate",
}


async def _get_converted_folders(db) -> set[str]:
    """Return the set of parent folder paths (with trailing slash) where Shrinkerr
    has successfully converted at least one file. Used to infer that other HEVC
    files in the same folder are 'already converted'."""
    folders: set[str] = set()
    async with db.execute(
        "SELECT file_path, original_file_path FROM jobs "
        "WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        for fp in (row["file_path"], row["original_file_path"]):
            if fp and "/" in fp:
                folders.add(fp.rsplit("/", 1)[0] + "/")
    return folders


@router.get("/tree")
async def get_scan_tree(filter: str = "all"):
    """Return folder hierarchy with aggregated counts/sizes.

    Fast path: pushes simple filters (codec, resolution, size, converted, etc.) into
    SQL, skips JSON parsing, and only builds the enrichment context when a filter
    actually needs it (ignored/queued/plex_*).
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # Build SQL WHERE + figure out which filters still need Python
        tokens = [t.strip() for t in filter.split(",") if t.strip() and t.strip() != "all"]
        sql_extras = []
        sql_params: list = []
        python_filters: set = set()
        for tok in tokens:
            frag, params, py = _build_tree_sql_filter(tok)
            if frag:
                sql_extras.append(frag)
                sql_params.extend(params)
            python_filters |= py

        need_ctx = bool(python_filters & _ENRICHMENT_FILTERS)
        ctx = await _build_enrichment_context(db) if need_ctx else None

        # Special handling for 'converted' — requires folder set from jobs table.
        converted_folders: set[str] | None = None
        if "converted" in python_filters:
            converted_folders = await _get_converted_folders(db)
            # Narrow in SQL: only rows where converted=1 OR the file's already in target format.
            # The Python loop below does the folder membership check for the needs_conversion=0 case.
            sql_extras.append("AND (converted = 1 OR needs_conversion = 0)")
            # Keep 'converted' in python_filters so the loop applies the folder check

        # Minimal column set — tree aggregation only needs path/size/mtime,
        # plus any columns still referenced by remaining python_filters.
        cols = """id, file_path, file_size, file_mtime, video_height, video_codec,
                  needs_conversion, converted, duration,
                  COALESCE(has_removable_tracks_flag, 0) as has_removable_tracks,
                  COALESCE(has_removable_subs_flag, 0) as has_removable_subs,
                  COALESCE(has_lossless_audio_flag, 0) as has_lossless_audio,
                  new_detected_at"""
        where_extra = (" " + " ".join(sql_extras)) if sql_extras else ""

        query = f"SELECT {cols} FROM scan_results WHERE {_SCAN_WHERE}{where_extra}"
        async with db.execute(query, sql_params) as cur:
            rows = await cur.fetchall()

        # Group by parent folder, applying any remaining Python filters
        folders: dict[str, dict] = {}
        LOW_BR = ctx["LOW_BITRATE_THRESHOLD"] if ctx else 0
        cutoff_24h = ctx["cutoff_24h"] if ctx else ""
        HIGH_BR = 15_000_000

        for row in rows:
            r = dict(row)
            fp = r["file_path"]
            sz = r["file_size"] or 0
            dur = r["duration"] or 0

            # Python-side filter checks (only for tokens SQL couldn't handle)
            if python_filters:
                bitrate = (sz * 8 / dur) if dur > 0 else 0
                low_bitrate = bool(r.get("needs_conversion") and dur > 0 and bitrate < LOW_BR)
                is_ignored = _check_ignored(fp, ctx) if ctx else False
                skip = False
                for pf in python_filters:
                    if pf == "converted":
                        # Shrinkerr converted it directly, OR it's already in target format
                        # AND lives in a folder where at least one file was converted
                        if r.get("converted"):
                            continue
                        parent = fp.rsplit("/", 1)[0] + "/" if "/" in fp else ""
                        if not (not r.get("needs_conversion") and converted_folders and parent in converted_folders):
                            skip = True; break
                        continue
                    if pf == "new":
                        detected_at = r.get("new_detected_at")
                        if not (detected_at and detected_at > cutoff_24h):
                            skip = True; break
                    elif pf == "ignored":
                        if not is_ignored:
                            skip = True; break
                    elif pf == "queued":
                        if fp not in ctx["queued_paths"]:
                            skip = True; break
                    elif pf == "needs_conversion":
                        if not (r.get("needs_conversion") and not low_bitrate and not is_ignored):
                            skip = True; break
                    elif pf == "low_bitrate":
                        if not (low_bitrate and not is_ignored):
                            skip = True; break
                    elif pf == "high_bitrate":
                        if not (r.get("needs_conversion") and not is_ignored and bitrate > HIGH_BR):
                            skip = True; break
                    elif pf == "audio_cleanup":
                        if not (r.get("has_removable_tracks") and not is_ignored):
                            skip = True; break
                    elif pf == "sub_cleanup":
                        if not (r.get("has_removable_subs") and not is_ignored):
                            skip = True; break
                    elif pf == "res_4k":
                        vh = r.get("video_height") or 0
                        if vh >= 1400:
                            continue
                        fn = fp.lower()
                        if not ("2160p" in fn or "4k" in fn or "uhd" in fn):
                            skip = True; break
                    elif pf in ("plex_watched", "plex_unwatched", "plex_watchlist"):
                        want = pf.split("_", 1)[1]
                        status = _get_watch_status(fp, ctx) if ctx else None
                        if status != want:
                            skip = True; break
                if skip:
                    continue

            parent = fp.rsplit("/", 1)[0] if "/" in fp else ""
            if parent not in folders:
                folders[parent] = {
                    "path": parent,
                    "file_count": 0,
                    "total_size": 0,
                    "newest_mtime": 0,
                }
            fd = folders[parent]
            fd["file_count"] += 1
            fd["total_size"] += sz
            mt = r.get("file_mtime") or 0
            if mt > fd["newest_mtime"]:
                fd["newest_mtime"] = mt

        return {"folders": list(folders.values())}
    finally:
        await db.close()


@router.get("/files-by-title")
async def get_files_by_title(prefix: str, filter: str = "all"):
    """Return all enriched files under a title prefix (all seasons). Single DB call."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)
        title_prefix = prefix.rstrip("/") + "/"
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS} FROM scan_results
                WHERE {_SCAN_WHERE}
                  AND file_path LIKE ?
                ORDER BY file_path ASC""",
            (title_prefix + "%",),
        ) as cur:
            rows = await cur.fetchall()

        results = []
        for row in rows:
            enriched = _enrich_row(dict(row), ctx)
            if _matches_filter(enriched, filter):
                results.append(enriched)
        return results
    finally:
        await db.close()


class _FilesByPathsBody(BaseModel):
    file_paths: list[str]
    filter: str = "all"


@router.post("/files-by-paths")
async def get_scan_files_by_paths(body: _FilesByPathsBody):
    """Return enriched files for a given list of exact file paths.

    Designed for advanced search: one HTTP call + one enrichment-context build,
    instead of N parallel /files requests per folder. Each file is returned only
    if it passes the given filter.
    """
    if not body.file_paths:
        return []

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)

        # Chunk paths into batches of 500 to stay within SQLite variable limits
        results = []
        paths = list(body.file_paths)
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT {_SCAN_SELECT_COLS} FROM scan_results "
                f"WHERE {_SCAN_WHERE} AND file_path IN ({placeholders})",
                chunk,
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                enriched = _enrich_row(dict(row), ctx)
                if _matches_filter(enriched, body.filter):
                    results.append(enriched)
        return results
    finally:
        await db.close()


@router.get("/files")
async def get_scan_files(folder: str, filter: str = "all"):
    """Return enriched files for a single folder. Typically 5-50 files per call."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)

        # Get direct children only (files in this folder, not subfolders)
        folder_prefix = folder.rstrip("/") + "/"
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS} FROM scan_results
                WHERE {_SCAN_WHERE}
                  AND file_path LIKE ?
                  AND file_path NOT LIKE ?
                ORDER BY file_path ASC""",
            (folder_prefix + "%", folder_prefix + "%/%"),
        ) as cur:
            rows = await cur.fetchall()

        results = []
        for row in rows:
            enriched = _enrich_row(dict(row), ctx)
            if _matches_filter(enriched, filter):
                results.append(enriched)

        return results
    finally:
        await db.close()


@router.get("/results-version")
async def get_scan_results_version():
    """Lightweight check: returns count + max_id so frontend can skip full re-fetch."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT COUNT(*) as cnt, COALESCE(MAX(id), 0) as max_id "
            "FROM scan_results WHERE removed_from_list = 0 "
            "AND file_path NOT LIKE '%.converting.%' "
            "AND file_path NOT LIKE '%.remuxing.%'"
        ) as cur:
            row = await cur.fetchone()
            return {"count": row["cnt"], "max_id": row["max_id"]}
    finally:
        await db.close()


@router.get("/results")
async def get_scan_results():
    """Return scan results. Track JSON is omitted for performance — use /tracks-by-path for details."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)
        async with db.execute(
            f"SELECT {_SCAN_SELECT_COLS} FROM scan_results WHERE {_SCAN_WHERE} ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [_enrich_row(dict(row), ctx) for row in rows]
    finally:
        await db.close()


_metadata_task: asyncio.Task | None = None
_metadata_cancel = asyncio.Event()


async def _run_metadata_refresh() -> None:
    """Background task: refresh API metadata for files with heuristic language detection."""
    _metadata_cancel.clear()

    try:
        # Clear failed cache entries and load file list (short DB connection)
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout=60000")
            await db.execute("DELETE FROM metadata_cache WHERE original_language IS NULL")
            await db.commit()
            print("[METADATA] Cleared stale NULL cache entries for retry", flush=True)

            async with db.execute(
                "SELECT id, file_path, native_language FROM scan_results "
                "WHERE language_source = 'heuristic' AND removed_from_list = 0 ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()

        from backend.metadata import lookup_original_language

        total = len(rows)
        updated = 0
        skipped = 0
        pending_updates = []

        for idx, row in enumerate(rows):
            if _metadata_cancel.is_set():
                print(f"[METADATA] Refresh cancelled after {updated} updates", flush=True)
                break

            file_path = row["file_path"]

            try:
                api_lang = await asyncio.wait_for(
                    lookup_original_language(file_path),
                    timeout=10,
                )
            except (asyncio.TimeoutError, Exception):
                api_lang = None

            if not api_lang:
                skipped += 1
            else:
                pending_updates.append((api_lang, row["id"]))
                updated += 1

            # Batch-write updates every 25 files (short DB connection)
            if len(pending_updates) >= 25:
                db2 = await aiosqlite.connect(DB_PATH)
                try:
                    await db2.execute("PRAGMA busy_timeout=60000")
                    for lang, rid in pending_updates:
                        await db2.execute(
                            "UPDATE scan_results SET native_language = ?, language_source = 'api' WHERE id = ?",
                            (lang, rid),
                        )
                    await db2.commit()
                finally:
                    await db2.close()
                pending_updates.clear()
                print(f"[METADATA] Progress: {idx+1}/{total} checked, {updated} updated", flush=True)

            # Send progress via WebSocket
            if idx % 20 == 0:
                await ws_manager.send_scan_progress(
                    status="metadata",
                    current_file=file_path,
                    total=total,
                    probed=idx + 1,
                )

            # Yield to event loop
            await asyncio.sleep(0.05)

        # Flush remaining updates
        if pending_updates:
            db3 = await aiosqlite.connect(DB_PATH)
            try:
                await db3.execute("PRAGMA busy_timeout=60000")
                for lang, rid in pending_updates:
                    await db3.execute(
                        "UPDATE scan_results SET native_language = ?, language_source = 'api' WHERE id = ?",
                        (lang, rid),
                    )
                await db3.commit()
            finally:
                await db3.close()

        print(f"[METADATA] Refresh complete: {updated} updated, {skipped} no API data, {total} total", flush=True)
        await ws_manager.send_scan_progress(status="done", current_file="", total=total, probed=total)

    except Exception as exc:
        print(f"[METADATA] Refresh error: {exc}", flush=True)
        import traceback; traceback.print_exc()
    finally:
        await db.close()
        global _metadata_task
        _metadata_task = None


@router.post("/refresh-metadata")
async def refresh_metadata():
    global _metadata_task
    if _metadata_task and not _metadata_task.done():
        raise HTTPException(status_code=409, detail="Metadata refresh already in progress")
    _metadata_task = asyncio.create_task(_run_metadata_refresh())
    return {"status": "started"}


@router.post("/cancel-metadata")
async def cancel_metadata():
    global _metadata_task
    if _metadata_task is None or _metadata_task.done():
        return {"status": "not_running"}
    _metadata_cancel.set()
    return {"status": "cancelling"}


class UpdateTracksRequest(BaseModel):
    audio_tracks_json: str


@router.put("/results/{result_id}/tracks")
async def update_audio_tracks(result_id: int, req: UpdateTracksRequest):
    """Persist audio track keep/remove changes to the DB."""
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE scan_results SET audio_tracks_json = ? WHERE id = ?",
            (req.audio_tracks_json, result_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "updated", "id": result_id}


class UpdateSubTracksRequest(BaseModel):
    subtitle_tracks_json: str


@router.put("/results/{result_id}/subtitle-tracks")
async def update_subtitle_tracks(result_id: int, req: UpdateSubTracksRequest):
    """Persist subtitle track keep/remove changes to the DB."""
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE scan_results SET subtitle_tracks_json = ? WHERE id = ?",
            (req.subtitle_tracks_json, result_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "updated", "id": result_id}


@router.get("/tracks-by-path")
async def get_tracks_by_path(file_path: str):
    """Get audio/subtitle tracks for a single file by path. Lightweight endpoint for queue page."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT audio_tracks_json, subtitle_tracks_json FROM scan_results WHERE file_path = ?",
            (file_path,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return {"audio_tracks": [], "subtitle_tracks": []}
            audio = []
            subs = []
            try:
                audio = json.loads(row["audio_tracks_json"] or "[]")
            except (json.JSONDecodeError, ValueError):
                pass
            try:
                subs = json.loads(row["subtitle_tracks_json"] or "[]")
            except (json.JSONDecodeError, ValueError):
                pass
            return {"audio_tracks": audio, "subtitle_tracks": subs}
    finally:
        await db.close()


@router.post("/rescan-folder")
async def rescan_folder(request: ScanRequest):
    """Rescan a specific folder (e.g. a single movie or TV show directory)."""
    global _scan_task
    if _scan_task and not _scan_task.done():
        raise HTTPException(status_code=409, detail="Scan already in progress")
    _scan_task = asyncio.create_task(_run_scan(request.paths))
    return {"status": "started", "paths": request.paths}


@router.delete("/results/{result_id}")
async def delete_scan_result(result_id: int):
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE scan_results SET removed_from_list = 1 WHERE id = ?", (result_id,)
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted", "id": result_id}


class DeleteFileRequest(BaseModel):
    file_path: str


@router.post("/delete-file")
async def delete_file_from_disk(req: DeleteFileRequest):
    """Delete a file from disk AND remove from scan_results. Use with caution."""
    import os
    file_path = req.file_path

    # Safety: only allow deleting files under configured media directories
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            dirs = [r["path"] for r in await cur.fetchall()]
    finally:
        await db.close()

    if not any(file_path.startswith(d.rstrip("/") + "/") for d in dirs):
        raise HTTPException(403, "File is not under a configured media directory")

    # Check file exists
    if not os.path.isfile(file_path):
        # Still remove from DB even if file doesn't exist on disk
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("DELETE FROM scan_results WHERE file_path = ?", (file_path,))
            await db.commit()
        finally:
            await db.close()
        return {"status": "removed", "file_deleted": False, "message": "File not found on disk, removed from database"}

    # Move to trash
    try:
        from send2trash import send2trash
        send2trash(file_path)
    except Exception as exc:
        raise HTTPException(500, f"Failed to trash file: {exc}")

    # Remove from scan_results
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("DELETE FROM scan_results WHERE file_path = ?", (file_path,))
        # Also remove any pending jobs for this file
        await db.execute("DELETE FROM jobs WHERE file_path = ? AND status = 'pending'", (file_path,))
        await db.commit()
    finally:
        await db.close()

    # Trigger Plex scan to remove the deleted file
    try:
        from backend.plex import trigger_plex_scan
        await trigger_plex_scan(file_path)
    except Exception:
        pass

    print(f"[SCAN] Moved to trash: {file_path}", flush=True)
    return {"status": "trashed", "file_deleted": True}


