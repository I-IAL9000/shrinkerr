import asyncio
import json
import os
import re
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

# v0.9.66: detached best-effort tasks (post-scan Plex sync / poster prefetch).
# Held in a set so the event loop doesn't garbage-collect them mid-flight.
_bg_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Run a coroutine detached from the caller so it can't keep the scan task
    alive (and pin the UI at "Scanning…") while it works."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)

# v0.9.24: server-side bulk language-detection progress, so the UI can show a
# live N/total count that survives navigation and offer a cancel. Singleton —
# one bulk detect at a time.
_detect_task: asyncio.Task | None = None
_detect_progress: dict = {
    "active": False, "total": 0, "done": 0, "current": "",
    "changed": 0, "failed": 0, "cancelled": False,
}


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

            import json as _json_und
            def _row_has_und(json_str):
                try:
                    return any((t.get("language") or "und").lower() == "und"
                               for t in _json_und.loads(json_str or "[]"))
                except (ValueError, TypeError, AttributeError):
                    return False
            has_und = 1 if (_row_has_und(audio_json) or _row_has_und(sub_json)) else 0

            db.execute(
                """INSERT INTO scan_results
                   (file_path, file_size, video_codec, needs_conversion,
                    audio_tracks_json, subtitle_tracks_json, native_language, language_source, scan_timestamp, removed_from_list, is_new, file_mtime, new_detected_at, duration, probe_status, video_height,
                    has_removable_tracks_flag, has_removable_subs_flag, has_lossless_audio_flag, has_external_subs_flag, disc_type, video_conv_savings_bytes, has_und_tracks_flag, is_dubbed_flag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(file_path) DO UPDATE SET
                       file_size=excluded.file_size,
                       video_codec=excluded.video_codec,
                       needs_conversion=excluded.needs_conversion,
                       audio_tracks_json=excluded.audio_tracks_json,
                       subtitle_tracks_json=excluded.subtitle_tracks_json,
                       -- v0.9.94: never let a re-scan downgrade an authoritative
                       -- native/source (TMDB api, or a user's manual/tmdb-manual
                       -- match) back to the fresh heuristic guess. A re-scan
                       -- re-derives native from the audio tracks (heuristic), so
                       -- without this a folder rescan or the watcher re-seeing a
                       -- file wiped every API/manual match to 'heuristic'.
                       native_language = CASE
                           WHEN scan_results.language_source IN ('api','manual','tmdb-manual')
                           THEN scan_results.native_language ELSE excluded.native_language END,
                       language_source = CASE
                           WHEN scan_results.language_source IN ('api','manual','tmdb-manual')
                           THEN scan_results.language_source ELSE excluded.language_source END,
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
                       has_external_subs_flag=excluded.has_external_subs_flag,
                       disc_type=excluded.disc_type,
                       video_conv_savings_bytes=excluded.video_conv_savings_bytes,
                       has_und_tracks_flag=excluded.has_und_tracks_flag,
                       -- is_dubbed_flag: scan native is always heuristic -> 0; recomputed by refresh/set-language
                       is_dubbed_flag=0
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
                    getattr(scanned, 'disc_type', None),  # v0.6.0
                    getattr(scanned, 'video_conv_savings_bytes', 0),  # v0.6.7
                    has_und,  # v0.8.0 language detection
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

# v0.7.32: if a scan subprocess hangs (e.g. os.walk blocked on a dead /
# slow network mount), `proc.is_alive()` stays True forever, so the
# async monitor in _run_scan never returns and _scan_task never
# completes. Every watcher cycle then logs "Scan in progress, skipping
# cycle" and no new items get picked up — with no actual scan making
# progress. This threshold is how long the scan progress file can go
# without an update before we declare the scan hung and reap it.
# Generous enough not to trip a legitimately-long scan (the worker
# rewrites the progress file on every probed file, so even a slow
# scan updates it every few seconds). Tunable via env.
try:
    STALE_SCAN_MINUTES = max(1, int(os.environ.get("SHRINKERR_STALE_SCAN_MINUTES", "15")))
except ValueError:
    STALE_SCAN_MINUTES = 15


def scan_is_actively_running() -> bool:
    """True if a scan is genuinely in progress.

    Returns False when no scan is running OR when a scan task exists but
    has hung (its progress file hasn't been touched in STALE_SCAN_MINUTES
    minutes). In the hung case, the stuck subprocess is killed and
    `_scan_task` is cleared as a side effect, so callers (the watcher)
    can resume normal operation without a container restart.

    v0.7.32: added to break the "Scan in progress, skipping cycle"
    deadlock that happens when the scan subprocess blocks on a dead
    filesystem mount.
    """
    global _scan_task, _scan_proc
    if _scan_task is None or _scan_task.done():
        return False

    # A task exists and isn't done. Decide whether it's live or hung by
    # the freshness of the progress file (the worker rewrites it per
    # probed file).
    import time as _time
    try:
        age = _time.time() - os.path.getmtime(_scan_progress_file)
    except OSError:
        # No progress file yet — the scan just started (subprocess spins
        # up before writing the first progress). Treat as live; a real
        # hang will fail the mtime check on a later cycle once enough
        # time passes without the file appearing.
        return True

    if age <= STALE_SCAN_MINUTES * 60:
        return True  # fresh progress → genuinely running

    # Stale — the scan has made no progress for too long. Reap it.
    print(
        f"[SCANNER] Scan progress stale for {age/60:.1f} min "
        f"(> {STALE_SCAN_MINUTES} min) — treating as hung, reaping the "
        f"subprocess so the watcher can resume.",
        flush=True,
    )
    try:
        if _scan_proc is not None and _scan_proc.is_alive():
            _scan_proc.kill()
    except Exception as exc:
        print(f"[SCANNER] Failed to kill hung scan subprocess: {exc}", flush=True)
    try:
        if _scan_task is not None and not _scan_task.done():
            _scan_task.cancel()
    except Exception:
        pass
    _scan_proc = None
    _scan_task = None
    return False


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

    # NOTE: pre-v0.3.97 we did a wholesale `DELETE FROM scan_results
    # WHERE file_path LIKE 'path/%'` here, then re-walked. That left the
    # DB partially-wiped any time the subsequent walk silently failed
    # (uncaught ffprobe exception, permission hiccup, etc.) — visible to
    # the user as "click rescan → folder shows zero files → folder
    # disappears entirely a few seconds later when the empty-folder is
    # garbage-collected from the listing".
    #
    # New flow: don't pre-delete. The per-row INSERT uses
    # `ON CONFLICT(file_path) DO UPDATE` so re-scanning is idempotent.
    # After the scan completes, we delete rows for any file_path under
    # a *successfully-walked* path that the scan didn't emit (orphan
    # cleanup — handles renamed / deleted files). If the walk errors
    # mid-way, that path is excluded from cleanup and existing rows
    # survive. v0.3.97+.

    # Run the scan synchronously using asyncio.run in this process
    import asyncio as _asyncio

    async def _do_scan():
        from backend.scanner import scan_directory
        batch = []
        total_written = 0
        seen_paths: set[str] = set()
        completed_paths: list[str] = []

        async def progress_cb(status, current_file="", files_found=0, files_probed=0, total_files=0):
            write_progress(status, current_file, total_files, files_probed)

        async def result_cb(scanned):
            nonlocal batch, total_written
            seen_paths.add(scanned.file_path)
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
                completed_paths.append(path)
            except Exception as exc:
                print(f"[SCANNER] Error scanning {path}: {exc}", flush=True)
                import traceback; traceback.print_exc()

        # Flush remaining batch
        if batch:
            _write_batch_sync(db_path, list(batch), now)
            total_written += len(batch)
            print(f"[SCANNER] Written {total_written} results to DB (final batch)", flush=True)

        # Orphan cleanup — delete rows for files no longer present under
        # paths whose walk completed successfully. Skipped if cancelled
        # or if no paths walked clean (defensive: don't drop rows on a
        # fully-failed scan). v0.3.97+.
        if not is_cancelled() and completed_paths:
            try:
                db = sqlite3.connect(db_path)
                db.execute("PRAGMA journal_mode=WAL")
                # v0.9.64: best-effort maintenance sweep — fail fast (10s) under
                # write contention instead of blocking a folder rescan ~60s per
                # sweep. The file upsert already landed with a full timeout; a
                # skipped sweep is redone by the next full scan.
                db.execute("PRAGMA busy_timeout=10000")
                try:
                    db.execute("DROP TABLE IF EXISTS _seen_paths")
                    db.execute("CREATE TEMP TABLE _seen_paths (file_path TEXT PRIMARY KEY)")
                    if seen_paths:
                        db.executemany(
                            "INSERT OR IGNORE INTO _seen_paths (file_path) VALUES (?)",
                            [(p,) for p in seen_paths],
                        )

                    # v0.7.23: per-subfolder sanity belt — mirror of the
                    # v0.7.22 watcher fix. If any immediate subdirectory
                    # under a walked path would lose >50% of its known
                    # rows in this scan, preserve them (likely partial
                    # mount / unmounted subvolume). User-initiated scans
                    # weren't immune to the partial-mount problem either:
                    # if a user scans /media/M2T2 while a nested TV1
                    # mount is still pending, the walk finds the other
                    # subfolders but not TV1, flagging every TV1 row
                    # stale. Same threshold + log shape as the watcher
                    # belt so behavior is symmetric.
                    from collections import defaultdict as _dd
                    preserved_subs: set[str] = set()
                    for path in completed_paths:
                        path_norm = path.rstrip("/")
                        like_pat = path_norm + "/%"

                        # The "would be deleted" set (same filter the
                        # DELETE below uses), and the full known set.
                        stale_rows = db.execute(
                            """SELECT file_path FROM scan_results
                               WHERE file_path LIKE ?
                                 AND file_path NOT IN (SELECT file_path FROM _seen_paths)
                                 AND file_path NOT IN (
                                     SELECT file_path FROM jobs WHERE status IN ('pending', 'running')
                                 )""",
                            (like_pat,),
                        ).fetchall()
                        known_rows = db.execute(
                            "SELECT file_path FROM scan_results WHERE file_path LIKE ?",
                            (like_pat,),
                        ).fetchall()

                        def _first_sub(p: str, _root: str = path_norm) -> str | None:
                            if not p.startswith(_root + "/"):
                                return None
                            rest = p[len(_root) + 1:]
                            first = rest.split("/", 1)[0]
                            return f"{_root}/{first}" if first else None

                        known_by_sub: dict[str, int] = _dd(int)
                        stale_by_sub: dict[str, int] = _dd(int)
                        for (fp,) in known_rows:
                            s = _first_sub(fp)
                            if s:
                                known_by_sub[s] += 1
                        for (fp,) in stale_rows:
                            s = _first_sub(fp)
                            if s:
                                stale_by_sub[s] += 1

                        # v0.7.26: belt fires only on absolute row-loss
                        # volume — mirrors the watcher's threshold check.
                        # User actions (deletes, even multi-show) flow
                        # through to cleanup; only mount-loss-scale events
                        # (1000+ rows under one subfolder) preserve.
                        from backend.watcher import _belt_stale_trigger
                        belt_trigger = _belt_stale_trigger()
                        for sub, stale_n in stale_by_sub.items():
                            known_n = known_by_sub.get(sub, 0)
                            if stale_n >= belt_trigger:
                                preserved_subs.add(sub)
                                print(
                                    f"[SCANNER] subfolder {sub!r} would lose "
                                    f"{stale_n}/{known_n} rows this scan "
                                    f"(>= {belt_trigger} disaster-trigger); "
                                    f"preserving (likely partial mount / "
                                    f"unmounted subvolume). For legitimate "
                                    f"bulk moves, clean stale rows from the UI.",
                                    flush=True,
                                )

                    deleted_total = 0
                    for path in completed_paths:
                        path_norm = path.rstrip("/")
                        like_pat = path_norm + "/%"
                        # Inject `AND file_path NOT LIKE '<sub>/%'` per
                        # preserved subfolder under this walked path.
                        preserved_here = [
                            s for s in preserved_subs
                            if s.startswith(path_norm + "/")
                        ]
                        not_likes = ""
                        params: list = [like_pat]
                        for s in preserved_here:
                            not_likes += " AND file_path NOT LIKE ?"
                            params.append(s + "/%")
                        cur = db.execute(
                            f"""DELETE FROM scan_results
                               WHERE file_path LIKE ?{not_likes}
                                 AND file_path NOT IN (SELECT file_path FROM _seen_paths)
                                 AND file_path NOT IN (
                                     SELECT file_path FROM jobs WHERE status IN ('pending', 'running')
                                 )""",
                            params,
                        )
                        deleted_total += cur.rowcount
                    db.commit()
                    if deleted_total:
                        print(
                            f"[SCANNER] Orphan cleanup: dropped {deleted_total} stale row(s) "
                            f"under {len(completed_paths)} walked path(s); "
                            f"{len(seen_paths)} files seen this scan",
                            flush=True,
                        )
                finally:
                    db.close()
            except Exception as exc:
                print(f"[SCANNER] Orphan cleanup failed: {exc}", flush=True)
                import traceback; traceback.print_exc()

        # Restore converted flags — scoped to the walked paths. For a full
        # scan completed_paths is every media dir (≡ global); for a single-
        # folder rescan this shrinks a full-table UPDATE to the one folder so
        # it no longer holds the write lock across the whole library while
        # conversions are running. v0.9.59.
        if not is_cancelled() and completed_paths:
            try:
                db = sqlite3.connect(db_path)
                db.execute("PRAGMA journal_mode=WAL")
                # v0.9.64: best-effort maintenance sweep — fail fast (10s) under
                # write contention instead of blocking a folder rescan ~60s per
                # sweep. The file upsert already landed with a full timeout; a
                # skipped sweep is redone by the next full scan.
                db.execute("PRAGMA busy_timeout=10000")
                try:
                    _scope = " OR ".join("file_path LIKE ?" for _ in completed_paths)
                    _scope_params = [p.rstrip("/") + "/%" for p in completed_paths]
                    cur = db.execute(
                        f"""UPDATE scan_results SET converted = 1
                           WHERE converted = 0 AND ({_scope}) AND (
                               file_path IN (
                                   SELECT file_path FROM jobs
                                   WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0
                               )
                               OR file_path IN (
                                   SELECT original_file_path FROM jobs
                                   WHERE status = 'completed' AND job_type IN ('convert', 'combined')
                                   AND original_file_path IS NOT NULL AND space_saved > 0
                               )
                           )""",
                        _scope_params,
                    )
                    if cur.rowcount > 0:
                        db.commit()
                        print(f"[SCANNER] Restored 'converted' flag on {cur.rowcount} files", flush=True)
                finally:
                    db.close()
            except Exception as exc:
                print(f"[SCANNER] Failed to restore converted flags: {exc}", flush=True)

        # Detect duplicates — multiple files in the same folder (e.g. 4K + 1080p of same movie).
        # Scoped to the walked paths (≡ global for a full scan, one folder for
        # a rescan) so a folder rescan no longer resets + reloads the entire
        # scan_results table under the write lock. v0.9.59.
        if not is_cancelled() and completed_paths:
            try:
                db = sqlite3.connect(db_path)
                db.execute("PRAGMA journal_mode=WAL")
                # v0.9.64: best-effort maintenance sweep — fail fast (10s) under
                # write contention instead of blocking a folder rescan ~60s per
                # sweep. The file upsert already landed with a full timeout; a
                # skipped sweep is redone by the next full scan.
                db.execute("PRAGMA busy_timeout=10000")
                try:
                    _scope = " OR ".join("file_path LIKE ?" for _ in completed_paths)
                    _scope_params = [p.rstrip("/") + "/%" for p in completed_paths]
                    # Reset dup counts within the walked paths
                    db.execute(
                        f"UPDATE scan_results SET dup_count = 0, dup_group = NULL "
                        f"WHERE removed_from_list = 0 AND ({_scope})",
                        _scope_params,
                    )

                    # Find folders with multiple files (potential duplicates)
                    # Group by parent folder — if a movie folder has 2+ video files, they're duplicates
                    rows = db.execute(
                        f"""SELECT file_path FROM scan_results
                           WHERE removed_from_list = 0
                             AND file_path NOT LIKE '%.converting.%'
                             AND file_path NOT LIKE '%.remuxing.%'
                             AND ({_scope})""",
                        _scope_params,
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


def _path_scope_clause(paths: list[str]) -> tuple[str, list[str]]:
    """Build an SQL `(file_path LIKE ? OR ...)` fragment + params that scopes a
    query to files under the given folder paths (recursively). Empty paths →
    ('0', []) which matches nothing.

    v0.9.106: used to scope the post-scan inline health-check to the folders the
    scan actually covered. Without it a targeted folder rescan swept the whole
    library's recently-detected backlog (rescanning one folder health-checked
    hundreds of unrelated files)."""
    if not paths:
        return "0", []
    frag = "(" + " OR ".join("file_path LIKE ?" for _ in paths) + ")"
    params = [p.rstrip("/") + "/%" for p in paths]
    return frag, params


async def _run_scan(paths: list[str], is_folder_rescan: bool = False) -> None:
    """Launch scan in a subprocess and poll progress for websocket updates.

    `is_folder_rescan` (v0.9.67): a targeted single-folder rescan skips the
    post-scan library-wide Plex metadata sync + poster prefetch — those are
    full-scan concerns (they refresh rule-referenced labels/collections/watch
    status and posters across the whole library), redundant and wasteful to
    re-run for one folder. The periodic full scan and the watcher keep that
    cache fresh."""
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

    # v0.9.27: the worker walks every configured path to build the file list
    # before it emits any progress — minutes on spun-down disks, during which
    # the UI had no signal and the Scan button could flip back to idle. Push a
    # "discovering" event immediately so the button stays active and the user
    # sees feedback from the first second.
    await ws_manager.send_scan_progress(
        status="discovering", current_file="", total=0, probed=0)

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

        # Post-scan enrichment: Plex watch-status sync + poster prefetch. These
        # are best-effort and can take MINUTES on a large Plex library over the
        # network — run them DETACHED so they don't keep _scan_task alive and
        # pin the UI at "Scanning… 100%" with no feedback long after the files
        # were actually probed. v0.9.66.
        async def _post_scan_enrichment():
            try:
                from backend.plex import sync_plex_metadata_cache
                result = await sync_plex_metadata_cache()
                if result.get("watched") or result.get("unwatched"):
                    print(f"[SCANNER] Plex watch status synced: {result.get('watched', 0)} watched, {result.get('unwatched', 0)} unwatched", flush=True)
            except Exception as exc:
                print(f"[SCANNER] Plex watch status sync skipped: {exc}", flush=True)
            try:
                from backend.routes.posters import start_prefetch
                await start_prefetch()
                print(f"[SCANNER] Poster prefetch started", flush=True)
            except Exception as exc:
                print(f"[SCANNER] Poster prefetch skipped: {exc}", flush=True)
        # v0.9.67: skip the full-library enrichment for a targeted folder
        # rescan — it's redundant to re-sync the whole library for one folder.
        if not is_folder_rescan:
            _fire_and_forget(_post_scan_enrichment())
        else:
            print("[SCANNER] Folder rescan — skipping library-wide Plex sync/prefetch", flush=True)

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
                if hc_mode != "off" and paths:
                    # Files DETECTED in the last 24h, capped for safety, AND
                    # scoped to the paths this scan actually covered (v0.9.106).
                    # Without the path scope a one-folder rescan swept every
                    # recently-detected unchecked file in the whole library.
                    HC_BATCH_CAP = 2000
                    _scope_frag, _scope_params = _path_scope_clause(paths)
                    async with db_hc.execute(
                        "SELECT file_path FROM scan_results "
                        "WHERE removed_from_list = 0 AND health_status IS NULL "
                        "AND COALESCE(probe_status, 'ok') = 'ok' "
                        "AND new_detected_at IS NOT NULL "
                        "AND new_detected_at > datetime('now', '-1 day') "
                        f"AND {_scope_frag} "
                        "ORDER BY new_detected_at DESC LIMIT ?",
                        (*_scope_params, HC_BATCH_CAP),
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
    # v0.7.32: scan_is_actively_running() reaps a hung scan, so hitting
    # "Scan" recovers from the stuck-flag deadlock instead of 409ing.
    if scan_is_actively_running():
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


def _und_flag(audio, subs) -> int:
    """1 if any classified track is still und, else 0.

    Classified tracks are pydantic models exposing `.language`."""
    return 1 if any(
        (t.language or "und").lower() == "und" for t in list(audio) + list(subs)
    ) else 0


async def recompute_is_dubbed_flag(db, file_path: str) -> None:
    """Recompute is_dubbed_flag for one row from its CURRENT persisted state
    (audio_tracks_json, native_language, language_source). Used by the per-file
    write paths (detect, set-language) so we read the real stored result rather
    than mirroring the SQL CASE that preserves authoritative native/source."""
    from backend.scanner import _is_dubbed
    async with db.execute(
        "SELECT audio_tracks_json, native_language, language_source "
        "FROM scan_results WHERE file_path = ?", (file_path,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return
    try:
        audio = json.loads(row[0]) if row[0] else []
    except (ValueError, TypeError):
        audio = []
    langs = [(t.get("language") or "und") for t in audio]
    flag = _is_dubbed(langs, row[1], row[2])
    await db.execute(
        "UPDATE scan_results SET is_dubbed_flag = ? WHERE file_path = ?",
        (flag, file_path))


class DetectLanguagesRequest(BaseModel):
    file_path: str


async def _maybe_notify_plex_lang_change(file_path: str) -> bool:
    """Refresh the Plex folder for a file whose track languages changed.

    Gated on the default-on plex_notify_on_lang_change setting. Fail-open —
    a Plex hiccup never fails the detection that called it.
    """
    try:
        from backend.scanner import _is_cleanup_enabled
        if _is_cleanup_enabled("plex_notify_on_lang_change", default=True):
            from backend.plex import trigger_plex_scan
            return bool(await trigger_plex_scan(file_path))
    except Exception as exc:
        print(f"[LANG-DETECT] Plex notify failed (non-fatal): {exc}", flush=True)
    return False


def _rename_external_sub_with_lang(path: str, iso639_2: str) -> str | None:
    """Rename a sidecar sub to embed its detected language before the ext
    (`Movie.srt` -> `Movie.eng.srt`), the convention media servers and the
    scanner's own detector read the language from. Returns the new path, or
    None if it couldn't rename (missing file, name collision, OS error) —
    caller keeps the original path in that case.
    """
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.is_file():
        return None
    new_path = p.with_name(f"{p.stem}.{iso639_2}{p.suffix}")
    if new_path.exists():
        return None  # don't clobber an existing file
    # VobSub is a pair — rename the .sub partner alongside the .idx so they
    # stay matched (ffmpeg / OCR resolve the .sub from the .idx basename).
    partner_old = partner_new = None
    if p.suffix.lower() == ".idx":
        cand = p.with_suffix(".sub")
        if cand.is_file():
            partner_old = cand
            partner_new = p.with_name(f"{p.stem}.{iso639_2}.sub")
            if partner_new.exists():
                return None  # don't clobber the partner
    try:
        p.rename(new_path)
        if partner_old is not None:
            partner_old.rename(partner_new)
        return str(new_path)
    except OSError as exc:
        print(f"[LANG-DETECT] external sub rename failed ({exc}); DB detection kept", flush=True)
        return None


@router.post("/detect-languages")
async def detect_languages(req: DetectLanguagesRequest, notify_plex: bool = True):
    """Detect languages for a file's und audio + text-subtitle tracks,
    apply above-threshold results, persist re-classified tracks to
    scan_results, return the updated tracks. Fail-open per track."""
    from backend.scanner import (
        probe_file, classify_audio_tracks, classify_subtitle_tracks,
        detect_native_language, _extract_embedded_sub_text,
    )
    from backend.language_detection import (
        detect_audio_language, maybe_detect_subtitle_track_language, _TEXT_SUB_CODECS,
        detect_language_from_title,
    )
    # v0.9.17: detect_und_subs=False — we want the RAW und language so we can
    # detect + PERSIST + write it here. With inline detection on, probe_file
    # would resolve the sub itself and this endpoint would then skip it as
    # "not und", never saving or writing the result.
    probe = await probe_file(req.file_path, detect_und_subs=False)
    if probe is None:
        raise HTTPException(404, "Could not probe file")
    duration = probe.get("duration", 0.0) or 0.0
    raw_audio = probe.get("audio_tracks", []) or []
    raw_subs = probe.get("subtitle_tracks", []) or []
    changed = False
    # v0.8.3: per-type-ordinal lists of NEWLY-detected languages (only
    # tracks upgraded from und), for writing back to the file. Index i =
    # the (i+1)-th audio/subtitle track; None = leave that track alone.
    audio_write: list = [None] * len(raw_audio)
    sub_write: list = [None] * len(raw_subs)
    external_renamed = False
    # v0.9.44: per-(type, stream_index) reason a track stayed und, for the UI.
    detect_notes: dict[tuple[str, int], str] = {}

    # v0.9.7: external sidecar subs (.srt/.ass alongside the video) aren't in
    # probe_file's output — they live in the stored subtitle_tracks_json. Load
    # the und ones so we can detect them AND preserve every external sub when
    # we re-persist (rebuilding subtitle_tracks_json from probe alone would
    # otherwise drop them).
    stored_external_subs: list[dict] = []
    _db0 = await connect_db()
    try:
        async with _db0.execute(
            "SELECT subtitle_tracks_json FROM scan_results WHERE file_path = ?",
            (req.file_path,),
        ) as cur:
            _row0 = await cur.fetchone()
        if _row0 and _row0["subtitle_tracks_json"]:
            try:
                stored_external_subs = [
                    s for s in json.loads(_row0["subtitle_tracks_json"])
                    if s.get("external")
                ]
            except (ValueError, TypeError):
                stored_external_subs = []
    finally:
        await _db0.close()

    # v0.9.98: raw disc-stream containers (.m2ts/.mts/.ts) are Blu-ray/transport
    # streams meant to be converted first (see the Disc/ISO filter). Detecting
    # on them is unreliable AND their PGS subtitles route through pgsrip OCR run
    # in an executor thread that async cancellation can't kill — a full-movie
    # OCR over a network mount hung unkillably for 15+ min. Skip detection for
    # them and record a clear note instead of attempting (and hanging on) it.
    is_stream = req.file_path.lower().endswith((".m2ts", ".mts", ".ts"))
    _STREAM_NOTE = "detection not supported for m2ts/transport-stream — convert to MKV first"

    # Audio: detect und tracks.
    for i, t in enumerate(raw_audio):
        if (t.get("language") or "und").lower() == "und" and t.get("stream_index") is not None:
            if is_stream:
                detect_notes[("audio", t["stream_index"])] = _STREAM_NOTE
                continue
            # v0.9.10: a title that names the language ("English") is cheap and
            # reliable — try it before the (slow) whisper spoken-language ID.
            lang = detect_language_from_title(t.get("title"))
            _note = None
            if not lang:
                try:
                    lang, _c, _note = await detect_audio_language(req.file_path, t["stream_index"], duration=duration)
                except Exception as _dexc:
                    lang = None
                    _note = f"audio detection error: {str(_dexc)[:80]}"
            if lang:
                t["language"] = lang
                audio_write[i] = lang
                changed = True
            else:
                # v0.9.46: always record a reason so the UI never shows a bare
                # und with no explanation.
                detect_notes[("audio", t["stream_index"])] = _note or "could not identify audio language"
                print(f"[LANG-DETECT] audio s{t.get('stream_index')} codec={t.get('codec')} "
                      f"title={t.get('title','')!r}: stayed und", flush=True)

    # Subtitles: detect und text subs (fast, langdetect) and und image
    # subs (PGS/VobSub via OCR — v0.9.0, on-demand only, slower).
    _IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "pgs", "vobsub"}
    for j, t in enumerate(raw_subs):
        if (t.get("language") or "und").lower() == "und" and t.get("stream_index") is not None:
            if is_stream:
                detect_notes[("sub", t["stream_index"])] = _STREAM_NOTE
                continue
            # v0.9.10: prefer a language named in the title ("Traditional
            # Chinese", "Romanian") — forced/SDH subs often have too little
            # text to detect from content but a descriptive title.
            title_lang = detect_language_from_title(t.get("title"))
            if title_lang:
                t["language"] = title_lang
                sub_write[j] = title_lang
                changed = True
                continue
            codec_l = (t.get("codec") or "").lower()
            _sub_note = None
            if codec_l in _TEXT_SUB_CODECS:
                try:
                    txt = await _extract_embedded_sub_text(req.file_path, t["stream_index"])
                    new_lang = maybe_detect_subtitle_track_language("und", codec_l, txt)
                except Exception:
                    txt = None
                    new_lang = "und"
                if new_lang != "und":
                    t["language"] = new_lang
                    sub_write[j] = new_lang
                    changed = True
                else:
                    _sub_note = ("no text in subtitle" if not (txt and txt.strip())
                                 else "subtitle text not confidently identified")
            elif codec_l in _IMAGE_SUB_CODECS:
                try:
                    from backend.image_sub_ocr import detect_image_sub_language
                    # v0.9.1: stream coarse OCR stages to the UI (image-sub
                    # OCR takes minutes).
                    async def _ocr_progress(stage, _fp=req.file_path):
                        await ws_manager.send_detect_progress(_fp, stage)
                    ocr_lang, _c = await detect_image_sub_language(
                        req.file_path, t["stream_index"], codec_l,
                        progress_cb=_ocr_progress)
                except Exception as exc:
                    # v0.9.78: surface the reason. A raise here (setup failure
                    # before the OCR helpers' own [IMG-OCR] logging kicks in —
                    # e.g. an import or tempdir error) previously went to und
                    # with no log line at all, making it undiagnosable.
                    import traceback as _tb
                    print(f"[IMG-OCR] image-sub detection raised for "
                          f"s{t.get('stream_index')} ({codec_l}): {exc!r}\n"
                          f"{_tb.format_exc()}", flush=True)
                    ocr_lang = None
                if ocr_lang:
                    t["language"] = ocr_lang
                    sub_write[j] = ocr_lang
                    changed = True
                else:
                    _sub_note = "image subtitle OCR found no usable text"
            else:
                _sub_note = "unsupported subtitle format for detection"
            # Per-track outcome (title path returned earlier via `continue`).
            if (t.get("language") or "und").lower() == "und":
                _sup = "text" if codec_l in _TEXT_SUB_CODECS else "image" if codec_l in _IMAGE_SUB_CODECS else "unsupported"
                detect_notes[("sub", t["stream_index"])] = _sub_note or "could not identify subtitle language"
                print(f"[LANG-DETECT] sub s{t.get('stream_index')} codec={codec_l} ({_sup}): stayed und", flush=True)

    # External sidecar subs: read the file text (charset-aware), detect with
    # the same gated detector as embedded text subs, then rename the file to
    # embed the ISO-639-2 code so the language persists (the scanner and media
    # servers both read it from the filename). The DB language is updated even
    # if the rename fails (fail-open, same as embedded file writes).
    for es in stored_external_subs:
        if (es.get("language") or "und").lower() != "und":
            continue
        es_path = es.get("external_path") or ""
        if not es_path or not os.path.isfile(es_path):
            print(f"[LANG-DETECT] external sub missing on disk ({es_path}): stayed und", flush=True)
            continue
        codec_l = (es.get("codec") or "").lower()
        try:
            if codec_l in _IMAGE_SUB_CODECS:
                # v0.9.11: external VobSub (.idx/.sub) is image data, not text —
                # OCR it directly with subtile-ocr (it reads an on-disk .idx).
                from backend.image_sub_ocr import detect_external_vobsub_language
                _ocr_lang, _c = await detect_external_vobsub_language(es_path)
                new_lang = _ocr_lang or "und"
            else:
                from backend.scanner import _clean_srt_bytes
                with open(es_path, "rb") as _fh:
                    _raw = _fh.read(200_000)
                _text = _clean_srt_bytes(_raw)
                new_lang = maybe_detect_subtitle_track_language("und", codec_l, _text)
        except Exception:
            new_lang = "und"
        if new_lang != "und":
            es["language"] = new_lang
            new_path = _rename_external_sub_with_lang(es_path, new_lang)
            if new_path:
                es["external_path"] = new_path
                external_renamed = True
            changed = True
        else:
            print(f"[LANG-DETECT] external sub codec={codec_l} "
                  f"({os.path.basename(es_path)}): stayed und", flush=True)

    if not changed and not detect_notes:
        return {"status": "ok", "changed": False}

    # v0.9.26: write the detected tags into the FILE first, and only keep an
    # embedded result if it actually landed. mkv → mkvpropedit in place; other
    # → ffmpeg -c copy remux. A container that can't store per-track language
    # (AVI) reports success from ffmpeg but drops the tag, so apply_...()
    # verifies and returns False. When the write didn't persist we revert the
    # embedded tracks to und so the DB — and the Unknown-language flag — mirror
    # what the file (and Plex) actually contain, instead of silently dropping
    # the title out of the filter while it stays und on disk.
    from backend.language_detection import apply_track_languages_to_file, _UNTAGGABLE_CONTAINERS
    file_written = False
    try:
        file_written = await apply_track_languages_to_file(
            req.file_path, audio_write, sub_write,
        )
    except Exception as exc:
        print(f"[LANG-DETECT] file write failed (kept und): {exc}", flush=True)

    # v0.9.35: when the container can't hold per-track language (AVI etc.),
    # remember the detected language on the track (keyed by stream index) so a
    # later mkv conversion can apply it — but leave `language` = und (file
    # truth) so the title stays in the Unknown-language filter until converted.
    # v0.9.51: remember it whenever the write didn't stick — untaggable AVI OR
    # a taggable container the in-place write failed on (e.g. an .m4v the ipod
    # muxer rejects). Previously only untaggable stored pending, so a detected
    # .m4v silently reverted to und with no hint. Now it shows the
    # "detected → convert to MKV" hint and is offered in the remux dialog.
    audio_detected: dict[int, str] = {}
    sub_detected: dict[int, str] = {}
    if not file_written:
        for i, code in enumerate(audio_write):
            if code:
                si = raw_audio[i].get("stream_index")
                if si is not None:
                    audio_detected[si] = code
                raw_audio[i]["language"] = "und"
                audio_write[i] = None
        for j, code in enumerate(sub_write):
            if code:
                si = raw_subs[j].get("stream_index")
                if si is not None:
                    sub_detected[si] = code
                raw_subs[j]["language"] = "und"
                sub_write[j] = None

    # Nothing persisted or remembered — leave the row untouched so the title
    # stays in the Unknown-language filter and can be re-attempted.
    if not (any(audio_write) or any(sub_write) or external_renamed
            or audio_detected or sub_detected or detect_notes):
        return {"status": "ok", "changed": False, "file_written": False}

    # Re-classify + persist in the STORED schema (mirror the v0.6.5 backfill).
    native_lang = detect_native_language(raw_audio)
    audio_tracks = classify_audio_tracks(raw_audio, native_lang, duration)
    subtitle_tracks = classify_subtitle_tracks(raw_subs, native_lang)
    # v0.9.35: re-attach detected-but-unwritten languages (untaggable source).
    for t in audio_tracks:
        if t.stream_index in audio_detected:
            t.detected_language = audio_detected[t.stream_index]
    for t in subtitle_tracks:
        if t.stream_index in sub_detected:
            t.detected_language = sub_detected[t.stream_index]
    # v0.9.7: re-attach external sidecar subs (with any newly-detected
    # language / renamed path) — they aren't in `raw_subs`, so without this
    # they'd be dropped from subtitle_tracks_json.
    from backend.models import SubtitleTrack
    for es in stored_external_subs:
        try:
            subtitle_tracks.append(SubtitleTrack(**es))
        except Exception:
            pass
    # v0.9.44: attach the "why it stayed und" note to each still-und track.
    for t in audio_tracks:
        if ("audio", t.stream_index) in detect_notes:
            t.detect_note = detect_notes[("audio", t.stream_index)]
    for t in subtitle_tracks:
        if ("sub", t.stream_index) in detect_notes:
            t.detect_note = detect_notes[("sub", t.stream_index)]
    audio_json = json.dumps([t.model_dump() for t in audio_tracks])
    subtitle_json = json.dumps([t.model_dump() for t in subtitle_tracks])
    has_removable = 1 if any(not t.keep for t in audio_tracks) else 0
    has_removable_subs = 1 if any(not t.keep for t in subtitle_tracks) else 0
    db = await connect_db()
    try:
        # v0.9.68: record that detection ran via `tracks_detected`, and DON'T
        # overwrite language_source with 'detected' — detection determines
        # per-track languages, not the show's native language, so it must not
        # masquerade as a native-language source (that both hid the real
        # heuristic provenance and excluded the title from the heuristic→API
        # refresh). Refresh the native language + mark it 'heuristic' only when
        # the current source isn't authoritative (api / manual / tmdb-manual) —
        # never downgrade a TMDB- or user-set native to a heuristic guess.
        await db.execute(
            "UPDATE scan_results SET audio_tracks_json = ?, subtitle_tracks_json = ?, "
            "tracks_detected = 1, "
            "has_removable_tracks_flag = ?, has_removable_subs_flag = ?, "
            "has_und_tracks_flag = ?, "
            "native_language = CASE WHEN language_source IN ('api','manual','tmdb-manual') "
            "                       THEN native_language ELSE ? END, "
            "language_source = CASE WHEN language_source IN ('api','manual','tmdb-manual') "
            "                       THEN language_source ELSE 'heuristic' END "
            "WHERE file_path = ?",
            (audio_json, subtitle_json, has_removable, has_removable_subs,
             _und_flag(audio_tracks, subtitle_tracks), native_lang, req.file_path),
        )
        await db.commit()
        await recompute_is_dubbed_flag(db, req.file_path)
        await db.commit()
    finally:
        await db.close()

    # v0.9.1: notify Plex so it re-reads the now-corrected track languages,
    # only when the file was actually rewritten. v0.9.2: skippable via
    # notify_plex=False so batch runs can coalesce refreshes by folder
    # (see detect-languages-batch).
    # v0.9.7: an external-sub rename also changes what Plex reads (subtitle
    # filename), so notify on that too, not just embedded file writes.
    plex_notified = False
    if (file_written or external_renamed) and notify_plex:
        plex_notified = await _maybe_notify_plex_lang_change(req.file_path)

    return {
        "status": "ok",
        # v0.9.44: a notes-only run (everything stayed und, we just recorded
        # why) persists but isn't a real language change — keep the batch
        # "updated" counter honest.
        "changed": bool(any(audio_write) or any(sub_write) or external_renamed
                        or audio_detected or sub_detected),
        "file_written": file_written,
        "external_renamed": external_renamed,
        # v0.9.35: a language was detected but only remembered (untaggable
        # container) — it applies when the file is converted to mkv.
        "pending_detected": bool(audio_detected or sub_detected),
        "plex_notified": plex_notified,
        "native_language": native_lang,
        "audio_tracks": [t.model_dump() for t in audio_tracks],
        "subtitle_tracks": [t.model_dump() for t in subtitle_tracks],
    }


class SetTrackLanguageRequest(BaseModel):
    file_path: str
    track_type: str  # "audio" | "subtitle"
    stream_index: int
    language: str     # ISO 639-2/B code


@router.post("/set-track-language")
async def set_track_language(req: SetTrackLanguageRequest):
    """v0.9.43: manually set a track's language (for tracks detection can't
    resolve). Taggable containers (mkv/mp4) are written in place; untaggable
    ones (AVI etc.) store it as detected_language pending a remux-to-mkv, the
    same path auto-detection uses. External sidecar subs are renamed."""
    from backend.scanner import (
        probe_file, classify_audio_tracks, classify_subtitle_tracks,
        detect_native_language,
    )
    from backend.language_detection import apply_track_languages_to_file, _UNTAGGABLE_CONTAINERS
    from backend.models import SubtitleTrack

    lang = (req.language or "").strip().lower()
    if not lang:
        raise HTTPException(400, "A language must be provided")
    # "und" is allowed on purpose: it resets a track (e.g. one the old model
    # mis-detected) back to undetermined so language detection will re-run on
    # it. Detection re-probes the file, so the und must be written to the file,
    # not just the DB — the write path below handles that.

    probe = await probe_file(req.file_path, detect_und_subs=False)
    if probe is None:
        raise HTTPException(404, "Could not probe file")
    duration = probe.get("duration", 0.0) or 0.0
    raw_audio = probe.get("audio_tracks", []) or []
    raw_subs = probe.get("subtitle_tracks", []) or []

    # Load stored external subs (not in probe output) so we can set their
    # language and preserve them when re-persisting.
    stored_external_subs: list[dict] = []
    _db0 = await connect_db()
    try:
        async with _db0.execute(
            "SELECT subtitle_tracks_json FROM scan_results WHERE file_path = ?",
            (req.file_path,),
        ) as cur:
            _r = await cur.fetchone()
        if _r and _r["subtitle_tracks_json"]:
            try:
                stored_external_subs = [s for s in json.loads(_r["subtitle_tracks_json"]) if s.get("external")]
            except (ValueError, TypeError):
                pass
    finally:
        await _db0.close()

    audio_write: list = [None] * len(raw_audio)
    sub_write: list = [None] * len(raw_subs)
    external_renamed = False
    matched = False

    if req.track_type == "audio":
        for i, t in enumerate(raw_audio):
            if t.get("stream_index") == req.stream_index:
                t["language"] = lang; audio_write[i] = lang; matched = True; break
    else:
        for j, t in enumerate(raw_subs):
            if t.get("stream_index") == req.stream_index:
                t["language"] = lang; sub_write[j] = lang; matched = True; break
        if not matched:
            for es in stored_external_subs:
                if es.get("stream_index") == req.stream_index:
                    es["language"] = lang
                    new_path = _rename_external_sub_with_lang(es.get("external_path") or "", lang)
                    if new_path:
                        es["external_path"] = new_path; external_renamed = True
                    matched = True; break

    if not matched:
        raise HTTPException(404, "Track not found")

    # Write to the file if possible; anything that doesn't stick is remembered
    # as pending below (applied via Remux/Convert-to-MKV).
    file_written = False
    if any(audio_write) or any(sub_write):
        try:
            file_written = await apply_track_languages_to_file(req.file_path, audio_write, sub_write)
        except Exception as exc:
            print(f"[SET-LANG] file write failed: {exc}", flush=True)

    audio_detected: dict[int, str] = {}
    sub_detected: dict[int, str] = {}
    if (any(audio_write) or any(sub_write)) and not file_written:
        # v0.9.47: a MANUAL choice must never be silently lost. If the in-place
        # write didn't stick (untaggable AVI, an mp4 the muxer won't tag, a
        # mkvpropedit hiccup) remember it as pending — regardless of container —
        # so it applies via the Remux/Convert-to-MKV flow instead of reverting
        # to und with a misleading "Language set".
        for i, code in enumerate(audio_write):
            if code:
                si = raw_audio[i].get("stream_index")
                if si is not None:
                    audio_detected[si] = code
                raw_audio[i]["language"] = "und"; audio_write[i] = None
        for j, code in enumerate(sub_write):
            if code:
                si = raw_subs[j].get("stream_index")
                if si is not None:
                    sub_detected[si] = code
                raw_subs[j]["language"] = "und"; sub_write[j] = None

    native_lang = detect_native_language(raw_audio)
    audio_tracks = classify_audio_tracks(raw_audio, native_lang, duration)
    subtitle_tracks = classify_subtitle_tracks(raw_subs, native_lang)
    for es in stored_external_subs:
        try:
            subtitle_tracks.append(SubtitleTrack(**es))
        except Exception:
            pass
    for t in audio_tracks:
        if t.stream_index in audio_detected:
            t.detected_language = audio_detected[t.stream_index]
    for t in subtitle_tracks:
        if t.stream_index in sub_detected:
            t.detected_language = sub_detected[t.stream_index]

    audio_json = json.dumps([t.model_dump() for t in audio_tracks])
    subtitle_json = json.dumps([t.model_dump() for t in subtitle_tracks])
    has_removable = 1 if any(not t.keep for t in audio_tracks) else 0
    has_removable_subs = 1 if any(not t.keep for t in subtitle_tracks) else 0
    db = await connect_db()
    try:
        await db.execute(
            "UPDATE scan_results SET audio_tracks_json = ?, subtitle_tracks_json = ?, "
            "native_language = ?, language_source = 'manual', "
            "has_removable_tracks_flag = ?, has_removable_subs_flag = ?, "
            "has_und_tracks_flag = ? WHERE file_path = ?",
            (audio_json, subtitle_json, native_lang, has_removable, has_removable_subs,
             _und_flag(audio_tracks, subtitle_tracks), req.file_path),
        )
        await db.commit()
        await recompute_is_dubbed_flag(db, req.file_path)
        await db.commit()
    finally:
        await db.close()

    if file_written or external_renamed:
        await _maybe_notify_plex_lang_change(req.file_path)

    return {
        "status": "ok",
        "file_written": file_written,
        "pending_detected": bool(audio_detected or sub_detected),
        "audio_tracks": [t.model_dump() for t in audio_tracks],
        "subtitle_tracks": [t.model_dump() for t in subtitle_tracks],
    }


class DetectLanguagesBatchRequest(BaseModel):
    file_paths: list[str]


async def _expand_paths_for_detection(paths: list[str]) -> list[str]:
    """Expand folder selections to the und-track files inside them.

    v0.9.6: the bulk "Detect languages" action passes the raw scanner
    selection, which is mostly folder paths (poster cards select a folder,
    trailing "/"). Fan each folder out to its files that still carry und
    tracks (has_und_tracks_flag=1) so we don't re-probe files that don't
    need detection. Explicit file paths pass through unchanged so an
    intentional single-file selection is always honored.
    """
    folders = [p for p in paths if p.endswith("/")]
    files = [p for p in paths if not p.endswith("/")]
    resolved: list[str] = list(files)
    seen: set[str] = set(files)
    if folders:
        db = await connect_db()
        try:
            for folder in folders:
                async with db.execute(
                    "SELECT file_path FROM scan_results "
                    "WHERE file_path LIKE ? AND removed_from_list = 0 "
                    "AND COALESCE(has_und_tracks_flag, 0) = 1 "
                    "ORDER BY file_path",
                    (folder + "%",),
                ) as cur:
                    async for row in cur:
                        fp = row["file_path"]
                        if fp not in seen:
                            seen.add(fp)
                            resolved.append(fp)
        finally:
            await db.close()
    # v0.9.34: preserve the CALLER's folder order (the UI sends folders in the
    # order they're displayed — poster grid / file tree sort), so detection
    # processes in the order the user sees. Within a folder, files are
    # ORDER BY file_path (above) for stability. v0.9.33 force-sorted the whole
    # list alphabetically, which ignored the view's chosen sort/direction.
    return resolved


@router.post("/detect-languages-batch")
async def detect_languages_batch(req: DetectLanguagesBatchRequest):
    """Run detect-languages over files sequentially (single model instance —
    no parallel inference).

    Accepts folder paths (trailing "/") as well as file paths; folders are
    expanded server-side to their und-track files (see
    _expand_paths_for_detection) so the bulk action works on a poster-grid
    selection without the frontend pre-loading each folder's children.

    v0.9.2: Plex refreshes are coalesced. The per-file notify is suppressed
    during the loop; afterward one refresh fires per unique parent folder, so
    a season of episodes triggers a single folder refresh rather than one per
    episode (trigger_plex_scan refreshes the file's parent folder).
    """
    global _detect_task, _detect_progress
    if _detect_task is not None and not _detect_task.done():
        return {"status": "already_running", "progress": dict(_detect_progress)}
    _detect_progress = {
        "active": True, "total": 0, "done": 0, "current": "",
        "changed": 0, "failed": 0, "cancelled": False,
        # v0.9.37: files whose language was detected but only remembered
        # (untaggable container, e.g. AVI) — they need a remux-to-mkv to apply.
        "pending": 0, "pending_paths": [],
    }
    _detect_task = asyncio.create_task(_run_detect_batch(list(req.file_paths)))
    return {"status": "started"}


async def _run_detect_batch(paths_in: list[str]) -> None:
    """Background bulk detect. Updates `_detect_progress` per file so the UI can
    poll N/total, checks the cancel flag between files, and coalesces Plex
    refreshes to one per affected library section at the end (v0.9.26)."""
    global _detect_progress
    try:
        file_paths = await _expand_paths_for_detection(paths_in)
        _detect_progress["total"] = len(file_paths)
        written_folders: dict[str, str] = {}  # folder -> representative file_path
        for fp in file_paths:
            if _detect_progress["cancelled"]:
                break
            _detect_progress["current"] = os.path.basename(fp)
            try:
                r = await detect_languages(
                    DetectLanguagesRequest(file_path=fp), notify_plex=False)
                if r.get("changed"):
                    _detect_progress["changed"] += 1
                if r.get("file_written") or r.get("external_renamed"):
                    written_folders.setdefault(os.path.dirname(fp), fp)
                if r.get("pending_detected"):
                    _detect_progress["pending"] += 1
                    _detect_progress["pending_paths"].append(fp)
            except Exception as exc:
                _detect_progress["failed"] += 1
                print(f"[LANG-DETECT] batch error on {fp}: {exc}", flush=True)
            _detect_progress["done"] += 1
        # v0.9.26: refresh each affected Plex SECTION once (deduped) rather
        # than a scoped scan per folder — a full section refresh reliably
        # re-reads changed files' stream metadata, so files whose tags we just
        # wrote stop showing und in Plex without a manual library scan.
        if written_folders and not _detect_progress["cancelled"]:
            try:
                from backend.scanner import _is_cleanup_enabled
                if _is_cleanup_enabled("plex_notify_on_lang_change", default=True):
                    from backend.plex import refresh_plex_sections_for_files
                    await refresh_plex_sections_for_files(list(written_folders.values()))
            except Exception as exc:
                print(f"[LANG-DETECT] Plex refresh failed (non-fatal): {exc}", flush=True)
    except Exception as exc:
        print(f"[LANG-DETECT] batch aborted: {exc}", flush=True)
    finally:
        _detect_progress["active"] = False
        _detect_progress["current"] = ""


@router.get("/detect-batch-status")
async def detect_batch_status():
    """Current bulk-detect progress. Polled by the UI so the N/total indicator
    survives navigating away and back."""
    return dict(_detect_progress)


@router.post("/detect-batch-cancel")
async def detect_batch_cancel():
    """Ask the running bulk detect to stop after the current file."""
    _detect_progress["cancelled"] = True
    return {"status": "cancelling"}


@router.post("/detect-batch-ack-pending")
async def detect_batch_ack_pending():
    """v0.9.42: clear the finished batch's 'pending remux' list once the UI has
    shown its "convert to apply" dialog, so it isn't offered again on the next
    navigation. The batch result persists after completion precisely so a user
    who navigated away and back still sees the dialog once."""
    _detect_progress["pending"] = 0
    _detect_progress["pending_paths"] = []
    return {"status": "ok"}


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
                SUM(COALESCE(has_und_tracks_flag, 0)) as unknown_language,
                SUM(COALESCE(is_dubbed_flag, 0)) as dubbed,
                SUM(CASE WHEN language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual') THEN 1 ELSE 0 END) as not_api_matched,
                SUM(CASE WHEN (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual')) AND COALESCE(tmdb_unresolved,0) = 1 THEN 1 ELSE 0 END) as not_api_matched_no_tmdb,
                SUM(CASE WHEN disc_type IS NOT NULL THEN 1 ELSE 0 END) as disc_iso,
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

        # Media-dir label index for type classification — loaded once and
        # reused per-row in the loop below. v0.3.76+.
        dir_label_index: list[tuple[str, str]] = []
        try:
            async with db.execute("SELECT path, label FROM media_dirs WHERE enabled = 1") as cur:
                _dir_rows = [(r["path"], r["label"] or "") for r in await cur.fetchall()]
            dir_label_index = _build_dir_label_index(_dir_rows)
        except Exception:
            pass

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

                # Type detection — uses both filename brackets AND the
                # containing media-dir's label. Pre-v0.3.76 only the
                # bracket check ran, so users without `[tvdb-N]` /
                # `[ttN]` folder naming saw all files classified as
                # "other" even when they'd labelled their dirs in
                # Settings → Directories. v0.3.76+.
                dt = _classify_type_for_path(fp, dir_label_index)
                if dt == "tv":
                    type_tv_count += 1
                elif dt == "movie":
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
                "unknown_language": row["unknown_language"] or 0,
                "dubbed": row["dubbed"] or 0,
                "not_api_matched": row["not_api_matched"] or 0,
                "not_api_matched_no_tmdb": row["not_api_matched_no_tmdb"] or 0,
                "disc_iso": row["disc_iso"] or 0,
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
                "unknown_language": row["unknown_language"] or 0,
                "ignored_count": ignored_count,
                "estimated_savings_bytes": estimated_savings,
                "total_size": row["total_size"] or 0,
            },
        }
    finally:
        await db.close()


def _build_dir_label_index(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sort (path, label) pairs into a prefix-match-friendly index.

    Each entry's path gets a trailing slash so prefix matches don't false-
    positive on `/media/MovieDocs` when only `/media/Movie` is configured.
    Labels are lowercased for case-insensitive comparison. Sorted by path
    length descending so a nested dir wins over its parent (mirrors
    `media_dir_label_for` in backend/media_paths.py). v0.3.76+.
    """
    out: list[tuple[str, str]] = []
    for path, label in rows:
        if not path:
            continue
        norm = path.rstrip("/") + "/"
        out.append((norm, (label or "").strip().lower()))
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


# Pre-compiled in-path ID detectors. v0.3.85 broadened past the
# original `[tvdb-` / `[tt` / `[tmdb-` substring checks to also match
# curly-brace forms (Plex), `id` suffix forms (Jellyfin), and bare
# forms (file-level tagging without surrounding brackets). Mirrors
# `_extract_ids` in backend/routes/posters.py — both serve the same
# purpose of recognising user-tagged folders/files. Kept independent
# (rather than importing) so the hot-path classifier stays in this
# module.
_RE_TVDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tvdb(?:id)?[-=:]?\d+[\]\})]'         # bracketed/braced
    r'|(?<![a-z0-9])tvdb(?:id)?[-=]\d+(?![a-z0-9]))'  # bare with separator
)
_RE_TMDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tmdb(?:id)?[-=:]?\d+[\]\})]'
    r'|(?<![a-z0-9])tmdb(?:id)?[-=]\d+(?![a-z0-9]))'
)
_RE_IMDB_IN_PATH = re.compile(
    r'(?:[\[\{(]tt\d+[\]\})]'                          # bracketed/braced
    r'|(?<![a-z0-9])tt\d{7,}(?![a-z0-9]))'             # bare, ≥7 digits
)


def _classify_type_for_path(fp: str, dir_label_index: list[tuple[str, str]] | None) -> str:
    """Classify a file as 'movie', 'tv', or 'other'.

    Resolution priority:
      1. Bracket / brace / bare ID anywhere in the path:
           `tvdb…` → tv;  `tmdb…` or `tt…` → movie.
         Recognised forms (Sonarr/Radarr/Plex/Jellyfin/manual tagging):
           [tvdb-N], [tvdbid-N], {tvdb-N}, tvdb-N, tvdbid-N
           [tmdb-N], [tmdbid-N], {tmdb-N}, tmdb-N, tmdbid-N
           [ttN], {ttN}, ttNNNNNNN (≥7 digits, surrounded by separators)
         The full path is searched, so file-level tagging works
         (`/media/Movies/Foo.tt1234567.mkv`) just as well as folder-
         level. v0.3.85+.
      2. Containing media directory's user-set label — "Movies" → movie,
         "TV Shows" → tv, "Other" / unset → other.
      3. Default to 'other'.
    """
    fp_lower = fp.lower()
    if _RE_TVDB_IN_PATH.search(fp_lower):
        return "tv"
    if _RE_TMDB_IN_PATH.search(fp_lower) or _RE_IMDB_IN_PATH.search(fp_lower):
        return "movie"
    if dir_label_index:
        for prefix, label in dir_label_index:
            if fp.startswith(prefix):
                if label in ("movies", "movie"):
                    return "movie"
                if label in ("tv shows", "tv show", "tv"):
                    return "tv"
                return "other"
    return "other"


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

    # Media-dir label index for type-filter classification. Loaded once
    # per request and reused by every row's enrichment so we don't run a
    # DB query per file. v0.3.76+.
    dir_label_index: list[tuple[str, str]] = []
    try:
        async with db.execute("SELECT path, label FROM media_dirs WHERE enabled = 1") as cur:
            dir_rows = [(r["path"], r["label"] or "") for r in await cur.fetchall()]
        dir_label_index = _build_dir_label_index(dir_rows)
    except Exception:
        pass

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
        "dir_label_index": dir_label_index,
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
    disc_type = row.get("disc_type")

    is_ignored = _check_ignored(fp, ctx)
    bitrate = (sz * 8 / dur) if dur > 0 else 0
    low_bitrate = bool(row.get("needs_conversion") and dur > 0 and bitrate < ctx["LOW_BITRATE_THRESHOLD"])

    detected_at = row.get("new_detected_at")

    return {
        "id": row["id"],
        "file_path": fp,
        # v0.6.0: disc-aware file_name. For disc folders (file_path points
        # at the VIDEO_TS.IFO / BDMV/index.bdmv marker), use the disc-root
        # folder name (parent.parent). Regular files use the basename.
        "file_name": _disc_aware_file_name(fp, disc_type),
        "file_size": sz,
        "video_codec": row.get("video_codec"),
        "needs_conversion": bool(row.get("needs_conversion")),
        "native_language": row.get("native_language"),
        "has_removable_tracks": bool(row.get("has_removable_tracks")),
        # v0.9.3: the unknown_language / audio_cleanup matchers read
        # f["has_und_tracks"]; without it here they never matched, so the
        # Unknown-language filter returned "No files found" on click.
        "has_und_tracks": bool(row.get("has_und_tracks")),
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
        # Type filter (movie/tv/other) — combines filename-bracket detection
        # with the containing media-dir's user-set label. v0.3.76+.
        "dir_type": _classify_type_for_path(fp, ctx.get("dir_label_index")),
        # v0.6.0: disc marker ('dvd' / 'bdmv' / None). Frontend uses this
        # to render disc badges and skip per-track UI that doesn't apply.
        "disc_type": disc_type,
        # v0.6.7: CQ-calibrated video-conversion savings (excludes audio
        # track removal). Frontend reads this instead of computing
        # file_size * 0.3 locally.
        "video_conv_savings_bytes": row.get("video_conv_savings_bytes", 0) or 0,
    }


def _disc_aware_file_name(fp: str, disc_type: str | None) -> str:
    """Display-name for a scan row.

    Three cases:

    1. Folder disc — `file_path` points at the marker inside VIDEO_TS/
       or BDMV/ (~KB file). Basename ('VIDEO_TS.IFO' / 'index.bdmv') is
       useless as a label. Use the disc-root folder name two levels up.

    2. ISO disc (v0.7.10+) — `file_path` IS the `.iso` file. Return the
       .iso basename (parts[-1]), e.g. `rz0u.iso`, so the file list
       shows which actual disc image a row points at instead of the
       movie folder name (which is often already visible elsewhere
       and ambiguous when a folder holds multiple ISOs).
       v0.7.3-7.9 returned the parent folder (parts[-2], the movie
       folder); v0.7.0-7.1 returned parts[-3] (the media_dir, wrong).

    3. Regular file — basename.
    """
    if not fp:
        return ""
    if disc_type:
        parts = fp.rstrip("/").split("/")
        if fp.lower().endswith(".iso") and len(parts) >= 1:
            # /media/Misc/Movies2/Elephant (2003) [tt0363589]/rz0u.iso
            # → "rz0u.iso"
            return parts[-1]
        # Only treat this as a folder-disc when the basename is an actual
        # disc marker. A stale disc_type left on a converted single-file row
        # (e.g. a BDMV converted to .mkv, disc_type not cleared) must NOT
        # take this branch — parts[-3] would be the category dir ("Movies2")
        # instead of the title. See queue.py post-conversion update + the
        # stale-disc_type backfill.
        if parts[-1].lower() in ("video_ts.ifo", "index.bdmv") and len(parts) >= 3:
            # /movies/Some Movie/VIDEO_TS/VIDEO_TS.IFO → "Some Movie"
            return parts[-3]
    return fp.rsplit("/", 1)[-1]


def _enrich_row(row: dict, ctx: dict) -> dict:
    """Enrich a scan_results row with computed fields (ignored, queued, watch status, etc.)."""
    fp = row["file_path"]
    sz = row["file_size"] or 0
    dur = row["duration"] or 0
    disc_type = row.get("disc_type")

    is_ignored = _check_ignored(fp, ctx)
    bitrate = (sz * 8 / dur) if dur > 0 else 0
    low_bitrate = bool(row.get("needs_conversion") and dur > 0 and bitrate < ctx["LOW_BITRATE_THRESHOLD"])

    detected_at = row.get("new_detected_at")

    return {
        "id": row["id"],
        "file_path": fp,
        # v0.6.0: disc-aware file_name (see _disc_aware_file_name). Without
        # this, the frontend recomputes from file_path.split("/").pop(),
        # which returns the marker basename ('VIDEO_TS.IFO') for discs.
        "file_name": _disc_aware_file_name(fp, disc_type),
        "file_size": sz,
        "video_codec": row.get("video_codec"),
        "needs_conversion": bool(row.get("needs_conversion")),
        "native_language": row.get("native_language"),
        "has_removable_tracks": bool(row.get("has_removable_tracks")),
        # v0.9.3: the unknown_language / audio_cleanup matchers read
        # f["has_und_tracks"]; without it here they never matched, so the
        # Unknown-language filter returned "No files found" on click.
        "has_und_tracks": bool(row.get("has_und_tracks")),
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
        "is_dubbed_flag": row.get("is_dubbed_flag", 0),
        # Health-check status. Without these fields, the "corrupt" filter
        # in _matches_single_filter (which looks at health_status == 'corrupt')
        # silently missed every file flagged corrupt by a health check rather
        # than by a probe failure. _enrich_row_minimal had these fields from
        # the start; _enrich_row simply forgot them. Mirrored here so the
        # two enrichers return compatible dicts.
        "health_status": row.get("health_status"),
        "health_check_type": row.get("health_check_type"),
        "health_checked_at": row.get("health_checked_at"),
        # Type filter (movie/tv/other) — combines filename-bracket detection
        # with the containing media-dir's user-set label. v0.3.76+.
        "dir_type": _classify_type_for_path(fp, ctx.get("dir_label_index")),
        # v0.6.0: disc marker ('dvd' / 'bdmv' / None). Frontend uses this
        # to render disc badges and skip per-track UI that doesn't apply.
        "disc_type": disc_type,
        # v0.6.7: CQ-calibrated video-conversion savings (excludes audio
        # track removal). Frontend reads this instead of computing
        # file_size * 0.3 locally.
        "video_conv_savings_bytes": row.get("video_conv_savings_bytes", 0) or 0,
    }


# Standard columns used by tree/files/results endpoints
_SCAN_SELECT_COLS = """id, file_path, file_size, video_codec, needs_conversion,
    native_language, language_source, new_detected_at, converted, file_mtime, duration,
    audio_tracks_json, subtitle_tracks_json,
    COALESCE(probe_status, 'ok') as probe_status,
    COALESCE(video_height, 0) as video_height,
    COALESCE(has_removable_tracks_flag, 0) as has_removable_tracks,
    COALESCE(has_und_tracks_flag, 0) as has_und_tracks,
    COALESCE(has_removable_subs_flag, 0) as has_removable_subs,
    COALESCE(has_lossless_audio_flag, 0) as has_lossless_audio,
    vmaf_score,
    health_status, health_check_type, health_checked_at,
    COALESCE(dup_count, 0) as duplicate_count,
    dup_group as duplicate_group,
    disc_type,
    COALESCE(is_dubbed_flag, 0) as is_dubbed_flag,
    COALESCE(video_conv_savings_bytes, 0) as video_conv_savings_bytes"""

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
        # v0.9.31: ignored titles included — ignore means "don't convert", not
        # "don't tidy tracks". Only the conversion filters hide ignored.
        return bool(f["has_removable_tracks"] or f.get("has_und_tracks"))
    if filter_name == "unknown_language":
        # v0.9.26: ignored titles ARE included here — an ignore rule means
        # "don't convert", not "don't tell me the audio is untagged".
        return bool(f.get("has_und_tracks"))
    if filter_name == "dubbed":
        return bool(enriched.get("is_dubbed_flag"))
    if filter_name == "not_api_matched":
        return (enriched.get("language_source") or "") not in ("api", "manual", "tmdb-manual")
    if filter_name == "disc_iso":
        return bool(enriched.get("disc_type"))
    if filter_name == "sub_cleanup":
        # v0.9.31: ignored titles included (see audio_cleanup).
        return bool(f["has_removable_subs"])
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
    # Type filters — combine filename-bracket detection (Sonarr/Radarr-
    # style) with the containing media-dir's user-set label. The combined
    # classification is precomputed in _enrich_row as `dir_type` so we
    # don't repeat the prefix match per filter check. v0.3.76+.
    dt = (f.get("dir_type") or "other").lower()
    if filter_name == "type_movie":
        return dt == "movie"
    if filter_name == "type_tv":
        return dt == "tv"
    if filter_name == "type_other":
        return dt == "other"
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
        sql = "AND (COALESCE(has_removable_tracks_flag, 0) = 1 OR COALESCE(has_und_tracks_flag, 0) = 1)"
        needs_python = {f}  # ignored NOT excluded (cleanup, not conversion) — v0.9.31
    elif f == "unknown_language":
        sql = "AND COALESCE(has_und_tracks_flag, 0) = 1"
        needs_python = {f}  # ignored NOT excluded — v0.9.26
    elif f == "dubbed":
        sql = "AND COALESCE(is_dubbed_flag, 0) = 1"
    elif f == "not_api_matched":
        sql = "AND (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))"
    elif f == "disc_iso":
        sql = "AND disc_type IS NOT NULL"
    elif f == "sub_cleanup":
        sql = "AND COALESCE(has_removable_subs_flag, 0) = 1"
        needs_python = {f}  # ignored NOT excluded (cleanup, not conversion) — v0.9.31
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

    # Type filters now fall through to Python because the classification
    # combines filename brackets AND the containing media-dir's label
    # (loaded into ctx.dir_label_index). Pre-v0.3.76 these were SQL LIKE
    # patterns — `LIKE '%[tt%'` etc. — but that ignored the user's dir
    # labels, so users without bracketed folder names saw every file
    # classified as `other`. The Python path uses the precomputed
    # `dir_type` field on each enriched row, so this is per-row constant
    # time. v0.3.76+.

    else:
        # Filters that need Python enrichment (is_new, ignored, queued, plex_*,
        # type_movie/tv/other since v0.3.76)
        needs_python = {f}

    return sql, params, needs_python


# Filters that require the expensive enrichment context (ignored/queued/plex tables)
_ENRICHMENT_FILTERS = {
    "new", "ignored", "queued", "plex_watched", "plex_unwatched", "plex_watchlist",
    "needs_conversion", "audio_cleanup", "sub_cleanup", "low_bitrate", "high_bitrate",
    # Type filters need the dir_label_index from ctx to classify files by
    # their containing media-dir's label (Movies/TV Shows/Other). v0.3.76+.
    "type_movie", "type_tv", "type_other",
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
                  COALESCE(has_und_tracks_flag, 0) as has_und_tracks,
                  COALESCE(has_removable_subs_flag, 0) as has_removable_subs,
                  COALESCE(has_lossless_audio_flag, 0) as has_lossless_audio,
                  new_detected_at"""
        where_extra = (" " + " ".join(sql_extras)) if sql_extras else ""

        query = f"SELECT {cols} FROM scan_results WHERE {_SCAN_WHERE}{where_extra}"
        async with db.execute(query, sql_params) as cur:
            rows = await cur.fetchall()

        # Files directly at a media root (no title folder) are grouped under
        # their full file path, so each stray file becomes its own "folder"
        # entry. Without this, they'd collapse under `/media` as a single row
        # whose prefix match then pulls in every sibling title.
        async with db.execute("SELECT path FROM media_dirs") as cur:
            media_roots = {r["path"].rstrip("/") for r in await cur.fetchall()}

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
                        # Mirror the SQL fragment + _matches_single_filter:
                        # audio_cleanup also covers und tracks, so an und-only
                        # file (no removable tracks) must still pass here.
                        # v0.9.31: ignored titles ARE included (cleanup, not conversion).
                        if not (r.get("has_removable_tracks") or r.get("has_und_tracks")):
                            skip = True; break
                    elif pf == "unknown_language":
                        # v0.9.26: ignored titles ARE included (see _matches_single_filter).
                        if not r.get("has_und_tracks"):
                            skip = True; break
                    elif pf == "sub_cleanup":
                        # v0.9.31: ignored titles ARE included (see audio_cleanup).
                        if not r.get("has_removable_subs"):
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
                    elif pf in ("type_movie", "type_tv", "type_other"):
                        # The tree endpoint maintains its own hand-rolled
                        # per-filter switch for performance — bypassing
                        # _matches_filter. v0.3.76 added type_* to
                        # _matches_filter and removed them from the SQL
                        # push-down, but forgot to wire them into THIS
                        # loop. Result: type_tv applied to a tree fetch
                        # silently passed every row through (no elif
                        # matched, skip=False stayed). The count and the
                        # /scan/results-driven badges were correct, but
                        # the tree (poster grid + file tree) ignored the
                        # filter entirely. v0.3.79 wires them in.
                        dt = _classify_type_for_path(fp, ctx["dir_label_index"]) if ctx else "other"
                        want = pf.split("_", 1)[1]  # 'movie', 'tv', 'other'
                        if dt != want:
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

            # Stray file directly at a media root also gets emitted as its
            # own pseudo-folder entry so the poster view can render one card
            # per loose file instead of collapsing them under the media root.
            # FileTree filters these out via `is_file` and keeps using the
            # parent folder entry above.
            if parent in media_roots:
                folders[fp] = {
                    "path": fp,
                    "file_count": 1,
                    "total_size": sz,
                    "newest_mtime": mt,
                    "is_file": True,
                }

        return {"folders": list(folders.values())}
    finally:
        await db.close()


@router.get("/files-by-title")
async def get_files_by_title(prefix: str, filter: str = "all"):
    """Return all enriched files under a title prefix (all seasons). Single DB call.

    When the prefix is itself a full file path (stray file at a media root),
    return just that file — prevents the LIKE from matching sibling titles
    that happen to share the media-root prefix.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)
        title_prefix = prefix.rstrip("/") + "/"
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS} FROM scan_results
                WHERE {_SCAN_WHERE}
                  AND (file_path = ? OR file_path LIKE ?)
                ORDER BY file_path ASC""",
            (prefix, title_prefix + "%"),
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
    """Return enriched files for a single folder. Typically 5-50 files per call.

    Also handles the `folder` being a full file path (stray file at a media
    root grouped as its own entry) — returns just that file.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)

        # Direct children of the folder OR an exact file match for stray-file
        # pseudo-folders that are keyed by the file path itself.
        folder_prefix = folder.rstrip("/") + "/"
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS} FROM scan_results
                WHERE {_SCAN_WHERE}
                  AND (
                    file_path = ?
                    OR (file_path LIKE ? AND file_path NOT LIKE ?)
                  )
                ORDER BY file_path ASC""",
            (folder, folder_prefix + "%", folder_prefix + "%/%"),
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


def _reclassify_keep_flags(audio_json_str, sub_json_str, native, duration):
    """Recompute audio/subtitle keep+locked flags for a corrected native
    language, mapping them back onto the STORED track dicts by stream_index so
    every other field (detected_language, detect_note, external_path, order)
    is preserved. v0.9.69: the metadata refresh used to change only
    native_language, leaving the keep/remove decisions computed against the old
    (wrong) native — e.g. a TV show heuristically matched as Portuguese kept
    its Portuguese track and marked the real Korean (native) one for removal
    even after the refresh corrected the native to Korean.

    Returns (audio_json, sub_json, has_removable_audio, has_removable_subs,
    has_und) or None if the JSON couldn't be parsed."""
    import json as _j
    from backend.scanner import classify_audio_tracks, classify_subtitle_tracks
    try:
        raw_audio = _j.loads(audio_json_str) if audio_json_str else []
        raw_subs = _j.loads(sub_json_str) if sub_json_str else []
    except (ValueError, TypeError):
        return None
    dur = duration or 0
    a_flags = {t.stream_index: (t.keep, t.locked)
               for t in classify_audio_tracks(list(raw_audio), native, dur)}
    s_flags = {t.stream_index: (t.keep, t.locked)
               for t in classify_subtitle_tracks(list(raw_subs), native)}
    for t in raw_audio:
        f = a_flags.get(t.get("stream_index"))
        if f:
            t["keep"], t["locked"] = f
    for t in raw_subs:
        f = s_flags.get(t.get("stream_index"))
        if f:
            t["keep"], t["locked"] = f
    has_rem_a = 1 if any(not t.get("keep", True) for t in raw_audio) else 0
    has_rem_s = 1 if any(not t.get("keep", True) for t in raw_subs) else 0
    und = 1 if any((t.get("language") or "und").lower() == "und"
                   for t in list(raw_audio) + list(raw_subs)) else 0
    return _j.dumps(raw_audio), _j.dumps(raw_subs), has_rem_a, has_rem_s, und


def _reclass_item(row, native):
    """Re-classify a scan_results row's tracks against `native`; return a
    _write_lang_batch item (dict) ONLY if the keep/remove classification
    actually changed, else None — so correctly-classified rows aren't
    needlessly rewritten. v0.9.70."""
    reclass = _reclassify_keep_flags(
        row["audio_tracks_json"], row["subtitle_tracks_json"], native, row["duration"])
    if not reclass:
        return None
    a_json, s_json, rem_a, rem_s, und = reclass
    if a_json == (row["audio_tracks_json"] or "[]") and \
            s_json == (row["subtitle_tracks_json"] or "[]"):
        return None  # unchanged — no write needed
    from backend.scanner import _is_dubbed
    audio_langs = [(t.get("language") or "und") for t in json.loads(a_json or "[]")]
    return {"rid": row["id"], "a_json": a_json, "s_json": s_json,
            "rem_a": rem_a, "rem_s": rem_s, "und": und,
            "dubbed": _is_dubbed(audio_langs, native, "api")}


async def _write_lang_batch(pending: list, retries: int = 4) -> bool:
    """Write a batch of language-refresh updates, retrying on a transient DB
    lock. Each item is a dict with 'rid' plus any of:
      - 'native'          → set native_language + language_source='api'
      - 'a_json'/'s_json'/'rem_a'/'rem_s'/'und' → rewrite the re-classified
        track JSON + keep/remove flags
    so a corrected native both flips the label AND fixes which tracks are kept
    (v0.9.69), and an already-'api' title whose classification drifted can be
    healed by rewriting tracks alone without touching the native (v0.9.70).

    v0.9.63: resilient — a persistent "database is locked" defers only THIS
    batch (rows retried next refresh) instead of aborting the whole run."""
    if not pending:
        return True
    for attempt in range(retries):
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("PRAGMA busy_timeout=60000")
            for item in pending:
                sets: list[str] = []
                params: list = []
                if item.get("native") is not None:
                    sets += ["native_language = ?", "language_source = 'api'"]
                    params.append(item["native"])
                if item.get("a_json") is not None:
                    sets += ["audio_tracks_json = ?", "subtitle_tracks_json = ?",
                             "has_removable_tracks_flag = ?", "has_removable_subs_flag = ?",
                             "has_und_tracks_flag = ?"]
                    params += [item["a_json"], item["s_json"], item["rem_a"],
                               item["rem_s"], item["und"]]
                if item.get("dubbed") is not None:
                    sets.append("is_dubbed_flag = ?")
                    params.append(item["dubbed"])
                if item.get("tmdb_unresolved") is not None:
                    sets.append("tmdb_unresolved = ?")
                    params.append(item["tmdb_unresolved"])
                if not sets:
                    continue
                params.append(item["rid"])
                await db.execute(
                    f"UPDATE scan_results SET {', '.join(sets)} WHERE id = ?", params)
            await db.commit()
            return True
        except Exception as exc:
            if "locked" in str(exc).lower() and attempt < retries - 1:
                print(f"[METADATA] batch write locked (attempt {attempt+1}/{retries}), retrying…", flush=True)
                await asyncio.sleep(2 * (attempt + 1))
                continue
            print(f"[METADATA] batch write failed, {len(pending)} update(s) deferred to next refresh: {exc}", flush=True)
            return False
        finally:
            await db.close()
    return False


async def backfill_reclassify_authoritative_native() -> int:
    """One-time heal: rows with an authoritative native (api/manual/tmdb-manual)
    whose audio/subtitle keep-flags were computed against a stale HEURISTIC
    native get their flags re-derived against the STORED native.

    Fixes legacy titles where a non-native track — e.g. a Chinese dub, when the
    first audio track is Chinese so the heuristic guessed native=chi — stayed
    marked-keep even though the API set native to English. The current scan and
    the heuristic→api resolve path both classify against the resolved native, so
    this only repairs rows an older scanner left drifted; the DEFAULT metadata
    refresh skips already-'api' rows. Only rows that actually change are written.
    Chunked by id to bound memory; guarded by a settings sentinel. v0.9.109.
    """
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'reclass_authoritative_native_done'"
        ) as cur:
            if await cur.fetchone():
                return 0
    finally:
        await db.close()

    healed = 0
    last_id = 0
    while True:
        db = await connect_db()
        try:
            async with db.execute(
                "SELECT id, audio_tracks_json, subtitle_tracks_json, native_language, duration "
                "FROM scan_results "
                "WHERE language_source IN ('api','manual','tmdb-manual') "
                "AND removed_from_list = 0 AND id > ? ORDER BY id LIMIT 2000",
                (last_id,),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()
        if not rows:
            break
        last_id = rows[-1]["id"]
        pending = [it for it in (_reclass_item(r, r["native_language"]) for r in rows) if it]
        if pending:
            await _write_lang_batch(pending)
            healed += len(pending)

    db = await connect_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES "
            "('reclass_authoritative_native_done', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        await db.commit()
    finally:
        await db.close()
    if healed:
        print(f"[METADATA] Reclassified {healed} api/manual row(s) against their "
              f"authoritative native (fixed stale keep-flags)", flush=True)
    return healed


async def _run_metadata_refresh(deep: bool = False) -> None:
    """Background task: refresh API metadata for files with heuristic language detection."""
    from backend.scanner import _is_dubbed
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

            # 'heuristic' titles get a fresh API lookup (→ 'api' when resolved);
            # 'api' titles get their tracks re-classified against the stored
            # (correct) native and rewritten only if the classification drifted
            # — this heals titles whose native was corrected by an earlier
            # refresh before track re-classification existed (v0.9.70). 'manual'
            # / 'tmdb-manual' stay excluded (authoritative — don't override).
            if deep:
                where = "WHERE language_source IN ('heuristic','api') AND removed_from_list = 0"
            else:
                # v0.9.91: retry ALL still-heuristic items every run (no
                # permanent tmdb_unresolved skip). The metadata_cache already
                # throttles real API load — cached results are instant and
                # failed lookups only re-hit TMDB after 24h — and no-id items
                # return instantly without an API call. The old permanent skip
                # also stranded items that a later lookup improvement (e.g.
                # v0.9.89's TMDB-id support) could now resolve. Speed still
                # comes from keeping the api-row re-classification deep-only.
                where = ("WHERE language_source = 'heuristic' "
                         "AND removed_from_list = 0")
            async with db.execute(
                "SELECT id, file_path, native_language, language_source, "
                "audio_tracks_json, subtitle_tracks_json, duration FROM scan_results "
                f"{where} ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()

        from backend.metadata import lookup_original_language
        from backend.media_paths import is_other_typed_dir

        total = len(rows)
        updated = 0
        skipped = 0
        pending_updates = []

        for idx, row in enumerate(rows):
            if _metadata_cancel.is_set():
                print(f"[METADATA] Refresh cancelled after {updated} updates", flush=True)
                break

            file_path = row["file_path"]

            # Skip files in "Other"-typed media dirs — TMDB matches there are
            # spurious. v0.3.33+.
            try:
                if await is_other_typed_dir(file_path):
                    pending_updates.append({"rid": row["id"], "tmdb_unresolved": 1})
                    skipped += 1
                    continue
            except Exception:
                pass

            is_heuristic = (row["language_source"] or "") == "heuristic"

            if is_heuristic:
                try:
                    api_lang = await asyncio.wait_for(
                        lookup_original_language(file_path),
                        timeout=10,
                    )
                except (asyncio.TimeoutError, Exception):
                    api_lang = None
                if api_lang:
                    # Resolved → flip to api + re-classify against the new native.
                    item = _reclass_item(row, api_lang) or {"rid": row["id"]}
                    item["native"] = api_lang
                    _aj = item.get("a_json") or row["audio_tracks_json"] or "[]"
                    item["dubbed"] = _is_dubbed(
                        [(t.get("language") or "und") for t in json.loads(_aj)],
                        api_lang, "api")
                    pending_updates.append(item)
                    updated += 1
                else:
                    # Unresolved: heal any drifted flags vs the current native
                    # and mark the row so future (non-deep) runs skip it.
                    item = _reclass_item(row, row["native_language"]) or {"rid": row["id"]}
                    item["tmdb_unresolved"] = 1
                    pending_updates.append(item)
                    skipped += 1
            else:
                # Already 'api' — no lookup; heal drifted track classification
                # against the stored (correct) native, writing only if changed.
                item = _reclass_item(row, row["native_language"])
                if item:
                    pending_updates.append(item)
                    updated += 1
                else:
                    skipped += 1

            # Batch-write updates every 25 files (resilient to transient locks
            # so contention doesn't abort the whole refresh — v0.9.63).
            if len(pending_updates) >= 25:
                await _write_lang_batch(pending_updates)
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

        # Flush remaining updates (resilient — see _write_lang_batch)
        if pending_updates:
            await _write_lang_batch(pending_updates)
            pending_updates.clear()

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
async def refresh_metadata(deep: bool = False):
    global _metadata_task
    if _metadata_task and not _metadata_task.done():
        raise HTTPException(status_code=409, detail="Metadata refresh already in progress")
    _metadata_task = asyncio.create_task(_run_metadata_refresh(deep=deep))
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
                return {"audio_tracks": [], "subtitle_tracks": [], "has_lossless_audio": False}
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

            # Compute lossless flag here so the queue page doesn't have
            # to duplicate the detection logic and drift over time. Uses
            # prefix matching on the DTS profile to catch variants beyond
            # the literal "DTS-HD MA" / "DTS-HD HRA" — e.g. ffprobe
            # sometimes reports "DTS-HD Master Audio" or
            # "DTS-HD MA + DTS:X". v0.3.106+.
            _LOSSLESS_CODECS = {
                "truehd", "pcm_s16le", "pcm_s24le", "pcm_s32le",
                "pcm_bluray", "flac", "mlp", "pcm_dvd",
            }

            def _track_is_lossless(t: dict) -> bool:
                codec = (t.get("codec") or "").lower()
                if codec in _LOSSLESS_CODECS:
                    return True
                if codec == "dts":
                    profile = (t.get("profile") or "").lower()
                    # DTS-HD MA, DTS-HD MA+, DTS-HD Master Audio, DTS-HD HRA, etc.
                    if profile.startswith("dts-hd m") or profile.startswith("dts-hd h"):
                        return True
                return False

            # Stamp each track with `is_lossless` so the queue UI can
            # check whether the *kept* tracks (after the job's removal
            # list is applied) actually need the lossless→EAC3
            # transcode. Without this, a file with a lossless secondary
            # that's being removed still got the "Lossless → EAC3"
            # badge in the Now-Converting card. v0.3.124+.
            for t in audio:
                t["is_lossless"] = _track_is_lossless(t)
            has_lossless = any(t.get("is_lossless") for t in audio)

            return {
                "audio_tracks": audio,
                "subtitle_tracks": subs,
                "has_lossless_audio": has_lossless,
            }
    finally:
        await db.close()


@router.post("/rescan-folder")
async def rescan_folder(request: ScanRequest):
    """Rescan a specific folder (e.g. a single movie or TV show directory)."""
    global _scan_task
    if scan_is_actively_running():  # v0.7.32: reaps a hung scan
        raise HTTPException(status_code=409, detail="Scan already in progress")
    _scan_task = asyncio.create_task(_run_scan(request.paths, is_folder_rescan=True))
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
    from pathlib import Path as _P
    file_path = req.file_path

    # Safety: only allow deleting files under configured media directories.
    # The old check was `file_path.startswith(media_dir + "/")` which a
    # literal `/media/../etc/hostname` passed trivially. Now we resolve
    # both sides (following symlinks) and use commonpath for a true
    # ancestor relationship.
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            dirs = [r["path"] for r in await cur.fetchall()]
    finally:
        await db.close()

    try:
        resolved_target = _P(file_path).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, f"Invalid file path: {exc}")
    resolved_target_str = str(resolved_target)

    def _is_inside(child: str, parent: str) -> bool:
        try:
            common = os.path.commonpath([child, str(_P(parent).resolve(strict=False))])
        except ValueError:
            return False  # different drives / mount points
        return common == str(_P(parent).resolve(strict=False))

    if not any(_is_inside(resolved_target_str, d) for d in dirs):
        raise HTTPException(403, "File is not under a configured media directory")

    # Use the resolved path downstream so an attacker can't smuggle a path
    # with traversal components past the DB lookups either.
    file_path = resolved_target_str

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


