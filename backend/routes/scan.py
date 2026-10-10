import asyncio
import json
import os
import re
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Request
from backend.api_errors import ApiError
from pydantic import BaseModel

from backend.database import DB_PATH, connect_db, is_temp_path, prefix_clause
from backend.models import ScanRequest
from backend.scan_filters import (
    HIGH_BITRATE, LISTED, LOW_BITRATE, PY_COLUMNS, count_all, facet_counts, parse_filter,
    row_converted, row_ignored, row_low_bitrate, row_type, row_watch_status,
    build_dir_label_index as _build_dir_label_index,
)
from backend.scanner import (
    keep_manual_choices, keep_manual_choices_json, mark_manual_choices, removable_audio_flag, scan_directory,
)
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


def _maybe_preserve_authoritative_tracks(
    scanned_source, existing_source,
    fresh_audio_json, fresh_sub_json, stored_audio_json, stored_sub_json,
):
    """Decide which track JSON to persist for a re-scanned row.

    v0.9.112: when this scan's fresh TMDB lookup FAILED (so its classification
    is 'heuristic') but the STORED row already has an authoritative native
    (api/manual/tmdb-manual), the upsert preserves that native — but a
    heuristic re-classification of the tracks drifts against it (e.g. re-keeps a
    Chinese dub on an English show because chi is the first audio track) AND
    wipes manual per-track edits. So keep the stored track JSON when the stream
    layout is unchanged. Returns the (audio_json, sub_json) to write.
    """
    _AUTH = ("api", "manual", "tmdb-manual")
    if scanned_source in _AUTH or existing_source not in _AUTH:
        return fresh_audio_json, fresh_sub_json

    def _layout(js):
        try:
            return sorted((t.get("stream_index"), (t.get("language") or "und").lower())
                          for t in json.loads(js or "[]"))
        except (ValueError, TypeError):
            return None

    if (_layout(fresh_audio_json) == _layout(stored_audio_json)
            and _layout(fresh_sub_json) == _layout(stored_sub_json)):
        return stored_audio_json, (stored_sub_json if stored_sub_json else None)
    return fresh_audio_json, fresh_sub_json


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

def _bool_or_none(value) -> "int | None":
    return None if value is None else (1 if value else 0)


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
            if is_temp_path(scanned.file_path):
                continue  # F18: never a library file (the walkers skip them too)
            audio_json = json.dumps([t.model_dump() for t in scanned.audio_tracks])
            sub_json = json.dumps([t.model_dump() for t in scanned.subtitle_tracks]) if scanned.subtitle_tracks else None

            try:
                _ex = db.execute(
                    "SELECT language_source, audio_tracks_json, subtitle_tracks_json, native_language "
                    "FROM scan_results WHERE file_path = ?", (scanned.file_path,)
                ).fetchone()
            except Exception:
                _ex = None
            # v0.9.112: don't let a re-scan whose fresh TMDB lookup failed
            # (source='heuristic') overwrite an authoritative row's tracks with a
            # heuristic re-classification — it drifts against the preserved
            # native and wipes manual edits. Preserve the stored tracks when the
            # stream layout is unchanged.
            _preserved = False
            if _ex and getattr(scanned, "language_source", "heuristic") == "heuristic":
                audio_json, sub_json = _maybe_preserve_authoritative_tracks(
                    getattr(scanned, "language_source", "heuristic"),
                    _ex[0], audio_json, sub_json, _ex[1], _ex[2])
                _preserved = audio_json == _ex[1]
            # SC-05 (v0.10.0): the upsert keeps a stored TMDB/manual native over
            # this scan's own lookup, so sort the fresh tracks against it too —
            # otherwise a manual match's original-language audio was marked for
            # removal whenever TMDB disagreed (the reason it was fixed).
            if (_ex and not _preserved and _ex[0] in AUTHORITATIVE_NATIVE_SOURCES and _ex[3]
                    and _ex[3].lower() != (scanned.native_language or "").lower()):
                _reclass = _reclassify_keep_flags(audio_json, sub_json, _ex[3], scanned.duration)
                if _reclass:
                    audio_json = _reclass[0]
                    sub_json = _reclass[1] if sub_json is not None else None

            # Keep/remove choices made by hand survive a rescan (v0.10.0).
            if _ex and not _preserved:
                audio_json = keep_manual_choices_json(audio_json, _ex[1], audio=True)
                sub_json = keep_manual_choices_json(sub_json, _ex[2])

            # Pre-compute flags at scan time (avoids 226K JSON parses per page
            # load) from the FINAL json — which may be the preserved stored one.
            def _has_removable(js):
                try:
                    return 1 if any(not t.get("keep", True) for t in json.loads(js or "[]")) else 0
                except (ValueError, TypeError):
                    return 0
            # Audio: also the original-language audio to move first, against
            # the native the upsert keeps (a stored TMDB / manual one wins).
            _native = _ex[3] if (_ex and _ex[0] in AUTHORITATIVE_NATIVE_SOURCES) else scanned.native_language
            try:
                has_removable = removable_audio_flag(json.loads(audio_json or "[]"), _native)
            except (ValueError, TypeError):
                has_removable = 0
            has_removable_subs = _has_removable(sub_json)
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
                    audio_tracks_json, subtitle_tracks_json, native_language, language_source, scan_timestamp, removed_from_list, is_new, file_mtime, new_detected_at, duration, probe_status, probe_error, video_height,
                    has_removable_tracks_flag, has_removable_subs_flag, has_lossless_audio_flag, has_external_subs_flag, disc_type, video_conv_savings_bytes, has_und_tracks_flag, is_dubbed_flag, hdr_format, video_width, probe_json,
                    video_fps, video_bit_depth, video_interlaced, video_vfr)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
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
                       probe_error=excluded.probe_error,
                       video_height=excluded.video_height,
                       has_removable_tracks_flag=excluded.has_removable_tracks_flag,
                       has_removable_subs_flag=excluded.has_removable_subs_flag,
                       has_lossless_audio_flag=excluded.has_lossless_audio_flag,
                       has_external_subs_flag=excluded.has_external_subs_flag,
                       disc_type=excluded.disc_type,
                       video_conv_savings_bytes=excluded.video_conv_savings_bytes,
                       has_und_tracks_flag=excluded.has_und_tracks_flag,
                       -- is_dubbed_flag: scan native is always heuristic -> 0; recomputed by refresh/set-language
                       is_dubbed_flag=0,
                       hdr_format=excluded.hdr_format,
                       video_width=excluded.video_width,
                       -- SC-13: NULL from the watcher drops a stale probe.
                       probe_json=excluded.probe_json,
                       video_fps=excluded.video_fps,
                       video_bit_depth=excluded.video_bit_depth,
                       video_interlaced=excluded.video_interlaced,
                       video_vfr=excluded.video_vfr
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
                    getattr(scanned, 'probe_error', None),  # v0.9.153
                    getattr(scanned, 'video_height', 0),
                    has_removable,
                    has_removable_subs,
                    has_lossless,
                    1 if getattr(scanned, 'has_external_subs', False) else 0,
                    getattr(scanned, 'disc_type', None),  # v0.6.0
                    getattr(scanned, 'video_conv_savings_bytes', 0),  # v0.6.7
                    has_und,  # v0.8.0 language detection
                    getattr(scanned, 'hdr_format', None),  # v0.10.0
                    getattr(scanned, 'video_width', 0),  # v0.10.0 (SC-22)
                    getattr(scanned, 'probe_cache', None),  # v0.10.0 (SC-13)
                    # v0.10.0: unknown (0 / None) stored as NULL
                    getattr(scanned, 'video_fps', 0) or None,
                    getattr(scanned, 'video_bit_depth', 0) or None,
                    _bool_or_none(getattr(scanned, 'video_interlaced', None)),
                    _bool_or_none(getattr(scanned, 'video_vfr', None)),
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
# In the data folder, not world-writable /tmp under a fixed name, where any
# local user could cancel scans or fake progress for the hung-scan reaper,
# and two instances on one host shared them (F21, v0.10.0).
_scan_progress_file = os.path.join(os.path.dirname(DB_PATH), "scan_progress.json")
_scan_cancel_file = os.path.join(os.path.dirname(DB_PATH), "scan_cancel")

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
    # The scan subprocess has finished and the task is in its post-scan
    # health check (each check has its own timeout): a quiet progress file
    # there is not a hang. SC-19 (v0.10.0): the check was reaped as a "hung
    # scan" after 15 minutes and the UI showed the scan as cancelled.
    if _scan_proc is not None and not _scan_proc.is_alive():
        return True

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


# Extras that sit beside a movie and aren't another copy of it: Plex's local
# extras suffixes, samples, and the parts of a multi-part film (v0.10.0).
_EXTRA_RE = re.compile(
    r"(?:-(?:trailer|sample|featurette|behindthescenes|deleted|interview|scene|short|other)\b"
    r"|\b(?:sample|trailer|featurette)\b"
    r"|\b(?:cd|disc|disk|part|pt)[ ._-]?\d\b)",
    re.IGNORECASE,
)
_EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def duplicate_groups(paths) -> dict[str, list[str]]:
    """Files that are other copies of the same title, grouped: the same
    episode (season and episode) twice in a folder, or two versions of a
    movie in its folder. The season was ignored, so S01E01 and S02E01 in one
    show folder were "duplicates", as were a movie's trailer, sample and
    CD1/CD2 parts (SC-16, v0.10.0)."""
    from collections import defaultdict
    by_folder: dict[str, list[str]] = defaultdict(list)
    for fp in paths:
        by_folder[fp.rsplit("/", 1)[0] if "/" in fp else ""].append(fp)
    groups: dict[str, list[str]] = {}
    for folder, files in by_folder.items():
        if len(files) < 2:
            continue
        folder_name = folder.rsplit("/", 1)[-1].lower()
        episodic = (folder_name.startswith(("season", "specials"))
                    or any(_EPISODE_RE.search(f.rsplit("/", 1)[-1]) for f in files))
        if episodic:
            by_episode: dict[str, list[str]] = defaultdict(list)
            for fp in files:
                m = _EPISODE_RE.search(fp.rsplit("/", 1)[-1])
                if m:
                    by_episode[f"S{int(m[1])}E{int(m[2])}"].append(fp)
            for key, eps in by_episode.items():
                if len(eps) > 1:
                    groups[f"ep:{folder}/{key}"] = eps
        else:
            versions = [fp for fp in files if not _EXTRA_RE.search(fp.rsplit("/", 1)[-1])]
            if len(versions) > 1:
                groups[f"folder:{folder}"] = versions
    return groups


def _scan_worker_process(paths: list[str], db_path: str, progress_file: str, cancel_file: str,
                         reuse_probes: bool = False) -> None:
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
        unreadable_dirs: list[str] = []  # SC-03: never orphan-clean under these

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
                    unreadable=unreadable_dirs,
                    reuse_probes=reuse_probes,
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
                    preserved_paths: set[str] = set()
                    _unlisted = [d.rstrip("/") + "/" for d in unreadable_dirs]
                    _media_roots = {r[0].rstrip("/") for r in db.execute("SELECT path FROM media_dirs")}
                    for path in completed_paths:
                        path_norm = path.rstrip("/")
                        under_sql, under_params = prefix_clause([path_norm + "/"])

                        # The "would be deleted" set (same filter the
                        # DELETE below uses), and the full known set.
                        stale_rows = db.execute(
                            f"""SELECT file_path FROM scan_results
                               WHERE {under_sql}
                                 AND file_path NOT IN (SELECT file_path FROM _seen_paths)
                                 AND file_path NOT IN (
                                     SELECT file_path FROM jobs WHERE status IN ('pending', 'running')
                                 )""",
                            under_params,
                        ).fetchall()
                        known_rows = db.execute(
                            f"SELECT file_path FROM scan_results WHERE {under_sql}",
                            under_params,
                        ).fetchall()
                        # Rows under folders that couldn't be listed are kept
                        # anyway (SC-03); they say nothing about the rest.
                        stale_rows = [r for r in stale_rows if not any(r[0].startswith(u) for u in _unlisted)]
                        known_rows = [r for r in known_rows if not any(r[0].startswith(u) for u in _unlisted)]

                        # v0.10.0: whole-path belt, as the watcher's. An
                        # unmounted share leaves an empty mountpoint that
                        # walks clean, and every row under it looked stale;
                        # the per-subfolder belt below never fires on a
                        # movie library, where no folder holds 1000 files.
                        # Only for media folders: a missing subfolder can't
                        # be listed (kept above), and a folder rescan after
                        # deleting files should drop their rows.
                        if (path_norm in _media_roots and known_rows
                                and len(stale_rows) > len(known_rows) // 2):
                            preserved_paths.add(path_norm)
                            print(
                                f"[SCANNER] {path_norm!r} would lose {len(stale_rows)}/{len(known_rows)} "
                                f"rows this scan (>50%); preserving (likely not mounted). For legitimate "
                                f"bulk moves, clean stale rows from the UI.",
                                flush=True,
                            )
                            continue

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
                        if path_norm in preserved_paths:
                            continue
                        under_sql, under_params = prefix_clause([path_norm + "/"])
                        # Inject `AND NOT (<sub>/ range)` per preserved
                        # subfolder under this walked path.
                        preserved_here = [
                            s for s in preserved_subs
                            if s.startswith(path_norm + "/")
                        ] + [
                            d.rstrip("/") for d in unreadable_dirs
                            if d.rstrip("/") == path_norm or d.startswith(path_norm + "/")
                        ]
                        not_likes = ""
                        params: list = list(under_params)
                        for s in preserved_here:
                            keep_sql, keep_params = prefix_clause([s + "/"])
                            not_likes += f" AND NOT {keep_sql}"
                            params += keep_params
                        cur = db.execute(
                            f"""DELETE FROM scan_results
                               WHERE {under_sql}{not_likes}
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
                    _scope, _scope_params = prefix_clause([p.rstrip("/") + "/" for p in completed_paths])
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
                    _scope, _scope_params = prefix_clause([p.rstrip("/") + "/" for p in completed_paths])
                    # Reset dup counts within the walked paths
                    db.execute(
                        f"UPDATE scan_results SET dup_count = 0, dup_group = NULL "
                        f"WHERE +removed_from_list = 0 AND ({_scope})",
                        _scope_params,
                    )

                    # Find folders with multiple files (potential duplicates)
                    # Group by parent folder — if a movie folder has 2+ video files, they're duplicates
                    rows = db.execute(
                        f"""SELECT file_path FROM scan_results
                           WHERE +removed_from_list = 0
                             AND file_path NOT LIKE '%.converting.%'
                             AND file_path NOT LIKE '%.remuxing.%'
                             AND ({_scope})""",
                        _scope_params,
                    ).fetchall()

                    dup_count = 0
                    for group_id, files in duplicate_groups(fp for (fp,) in rows).items():
                        for fp in files:
                            db.execute(
                                "UPDATE scan_results SET dup_count = ?, dup_group = ? WHERE file_path = ?",
                                (len(files), group_id, fp)
                            )
                            dup_count += 1
                    # Commit the reset even with no duplicates left: a resolved
                    # duplicate kept its count forever (SC-16, v0.10.0).
                    db.commit()
                    if dup_count > 0:
                        print(f"[SCANNER] Detected {dup_count} duplicate files", flush=True)
                finally:
                    db.close()
            except Exception as exc:
                print(f"[SCANNER] Duplicate detection failed: {exc}", flush=True)

        write_progress("done" if not is_cancelled() else "cancelled", "", total_written, total_written)

    _asyncio.run(_do_scan())


def _path_scope_clause(paths: list[str]) -> tuple[str, list[str]]:
    """Build an SQL `((file_path >= ? AND file_path < ?) OR ...)` fragment + params that scopes a
    query to files under the given folder paths (recursively). Empty paths →
    ('0', []) which matches nothing.

    v0.9.106: used to scope the post-scan inline health-check to the folders the
    scan actually covered. Without it a targeted folder rescan swept the whole
    library's recently-detected backlog (rescanning one folder health-checked
    hundreds of unrelated files)."""
    if not paths:
        return "0", []
    return prefix_clause([p.rstrip("/") + "/" for p in paths])


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
        # A full scan reuses unchanged files' probes; a folder rescan is how
        # someone asks for a fresh look, so it probes everything (SC-13).
        args=(paths, DB_PATH, _scan_progress_file, _scan_cancel_file, not is_folder_rescan),
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
                await asyncio.to_thread(proc.join, 10)  # not on the loop (SC-19)
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
            # The Plex sync replaces the genre / library / watch rows Jellyfin
            # and Emby share with it; theirs were only restored by a manual
            # sync (v0.10.0). Both return at once when not configured.
            try:
                from backend.jellyfin import sync_jellyfin_metadata_cache
                await sync_jellyfin_metadata_cache()
            except Exception as exc:
                print(f"[SCANNER] Jellyfin metadata sync skipped: {exc}", flush=True)
            try:
                from backend.emby import sync_emby_metadata_cache
                await sync_emby_metadata_cache()
            except Exception as exc:
                print(f"[SCANNER] Emby metadata sync skipped: {exc}", flush=True)
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
                        "WHERE +removed_from_list = 0 AND +health_status IS NULL "
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
                from backend.file_events import log_event, EVENT_HEALTH_CHECK, health_check_code
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
                                    **health_check_code("corrupt", hc_mode),
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
        # In a thread (SC-19): joining here blocked the loop for up to 10 s.
        # Shielded so the join finishes even though this task is cancelled.
        try:
            await asyncio.shield(asyncio.to_thread(proc.join, 10))
        except asyncio.CancelledError:
            pass
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
        raise ApiError(status_code=409, detail="Scan already in progress", code="scan.alreadyRunning")
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
        _extract_embedded_sub_text,
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
        raise ApiError(404, "Could not probe file", code="scan.probeFailed")
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
    from backend.language_detection import KeyedNote  # v0.9.132 message codes
    _STREAM_NOTE = KeyedNote("detection not supported for m2ts/transport-stream — convert to MKV first", "streamUnsupported")

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
                    _err = str(_dexc)[:80]
                    _note = KeyedNote(f"audio detection error: {_err}", "audioError", {"error": _err})
            if lang:
                t["language"] = lang
                audio_write[i] = lang
                changed = True
            else:
                # v0.9.46: always record a reason so the UI never shows a bare
                # und with no explanation.
                detect_notes[("audio", t["stream_index"])] = _note or KeyedNote("could not identify audio language", "audioUnidentified")
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
                    _sub_note = (KeyedNote("no text in subtitle", "subNoText") if not (txt and txt.strip())
                                 else KeyedNote("subtitle text not confidently identified", "subNotConfident"))
            elif codec_l in _IMAGE_SUB_CODECS:
                try:
                    from backend.image_sub_ocr import detect_image_sub_language
                    # v0.9.1: stream coarse OCR stages to the UI (image-sub
                    # OCR takes minutes).
                    async def _ocr_progress(stage, stage_key=None, stage_params=None, _fp=req.file_path):
                        await ws_manager.send_detect_progress(_fp, stage, stage_key=stage_key, stage_params=stage_params)
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
                    _sub_note = KeyedNote("image subtitle OCR found no usable text", "ocrNoText")
            else:
                _sub_note = KeyedNote("unsupported subtitle format for detection", "subUnsupported")
            # Per-track outcome (title path returned earlier via `continue`).
            if (t.get("language") or "und").lower() == "und":
                _sup = "text" if codec_l in _TEXT_SUB_CODECS else "image" if codec_l in _IMAGE_SUB_CODECS else "unsupported"
                detect_notes[("sub", t["stream_index"])] = _sub_note or KeyedNote("could not identify subtitle language", "subUnidentified")
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
        if not es_path or not await asyncio.to_thread(os.path.isfile, es_path):
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
            new_path = await asyncio.to_thread(_rename_external_sub_with_lang, es_path, new_lang)
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
    native_lang, _ = await _classification_native(req.file_path, raw_audio)
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
    # v0.9.132: also carry the note's message code (KeyedNote) so the UI can
    # translate it; plain-str notes (none expected) just get no key.
    def _attach_note(t, note):
        t.detect_note = str(note)
        t.detect_note_key = getattr(note, "key", None)
        t.detect_note_params = getattr(note, "params", None) or None
    for t in audio_tracks:
        if ("audio", t.stream_index) in detect_notes:
            _attach_note(t, detect_notes[("audio", t.stream_index)])
    for t in subtitle_tracks:
        if ("sub", t.stream_index) in detect_notes:
            _attach_note(t, detect_notes[("sub", t.stream_index)])
    # Keep/remove choices made by hand stay (v0.10.0).
    stored_audio, stored_subs = await _stored_track_lists(req.file_path)
    audio_list = keep_manual_choices([t.model_dump() for t in audio_tracks], stored_audio, audio=True)
    sub_list = keep_manual_choices([t.model_dump() for t in subtitle_tracks], stored_subs)
    audio_json, subtitle_json = json.dumps(audio_list), json.dumps(sub_list)
    has_removable = removable_audio_flag(audio_list, native_lang)
    has_removable_subs = 1 if any(not t.get("keep", True) for t in sub_list) else 0
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


from backend.scanner import AUTHORITATIVE_NATIVE_SOURCES
_AUTHORITATIVE_NATIVE_SOURCES = AUTHORITATIVE_NATIVE_SOURCES


async def _classification_native(file_path: str, raw_audio: list) -> tuple[str, str]:
    """(native_language, language_source) to sort tracks against after a
    track's language changes. v0.9.155: both set-track-language and detection
    re-derived the native from the FIRST audio track — native German from
    TMDB plus a track relabeled Russian marked both German tracks for removal.
    A TMDB / manual native wins; otherwise the track-order guess, which stays
    labeled 'heuristic'."""
    from backend.scanner import detect_native_language
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT native_language, language_source FROM scan_results WHERE file_path = ?",
            (file_path,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row and row["native_language"] and (row["language_source"] or "") in _AUTHORITATIVE_NATIVE_SOURCES:
        return row["native_language"], row["language_source"]
    return detect_native_language(raw_audio), "heuristic"


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
    )
    from backend.language_detection import apply_track_languages_to_file, _UNTAGGABLE_CONTAINERS
    from backend.models import SubtitleTrack

    lang = (req.language or "").strip().lower()
    if not lang:
        raise ApiError(400, "A language must be provided", code="scan.languageRequired")
    # "und" is allowed on purpose: it resets a track (e.g. one the old model
    # mis-detected) back to undetermined so language detection will re-run on
    # it. Detection re-probes the file, so the und must be written to the file,
    # not just the DB — the write path below handles that.

    probe = await probe_file(req.file_path, detect_und_subs=False)
    if probe is None:
        raise ApiError(404, "Could not probe file", code="scan.probeFailed")
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
                    new_path = await asyncio.to_thread(_rename_external_sub_with_lang, es.get("external_path") or "", lang)
                    if new_path:
                        es["external_path"] = new_path; external_renamed = True
                    matched = True; break

    if not matched:
        raise ApiError(404, "Track not found", code="scan.trackNotFound")

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

    native_lang, native_source = await _classification_native(req.file_path, raw_audio)
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

    # Keep/remove choices made by hand stay (v0.10.0).
    stored_audio, stored_subs = await _stored_track_lists(req.file_path)
    audio_list = keep_manual_choices([t.model_dump() for t in audio_tracks], stored_audio, audio=True)
    sub_list = keep_manual_choices([t.model_dump() for t in subtitle_tracks], stored_subs)
    audio_json, subtitle_json = json.dumps(audio_list), json.dumps(sub_list)
    has_removable = removable_audio_flag(audio_list, native_lang)
    has_removable_subs = 1 if any(not t.get("keep", True) for t in sub_list) else 0
    db = await connect_db()
    try:
        await db.execute(
            "UPDATE scan_results SET audio_tracks_json = ?, subtitle_tracks_json = ?, "
            "native_language = ?, language_source = ?, "
            "has_removable_tracks_flag = ?, has_removable_subs_flag = ?, "
            "has_und_tracks_flag = ? WHERE file_path = ?",
            (audio_json, subtitle_json, native_lang, native_source, has_removable, has_removable_subs,
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
                under_sql, under_params = prefix_clause([folder])
                async with db.execute(
                    "SELECT file_path FROM scan_results "
                    f"WHERE {under_sql} AND +removed_from_list = 0 "
                    "AND COALESCE(has_und_tracks_flag, 0) = 1 "
                    "ORDER BY file_path",
                    under_params,
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
    """Every filter pill's count and the summary cards, from the same filter
    definitions as the lists (backend/scan_filters.py, F23 v0.10.0)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db)
        counts, needs_conversion_bytes = await count_all(db, ctx)
        async with db.execute(
            f"""SELECT SUM(file_size) AS total_size,
                SUM(CASE WHEN (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))
                         AND COALESCE(tmdb_unresolved, 0) = 1 THEN 1 ELSE 0 END) AS not_api_matched_no_tmdb
            FROM scan_results WHERE {LISTED}"""
        ) as cur:
            extra = dict(await cur.fetchone())
        counts["not_api_matched_no_tmdb"] = extra["not_api_matched_no_tmdb"] or 0

        # Savings estimate: the same curve and quality as the per-file and
        # Add to Queue estimates (v0.10.0: this card had its own curve, 25%
        # at CQ 20 where the estimate said 45%).
        from backend.encoding_estimates import cq_to_savings_pct, load_effective_cq
        est_pct = cq_to_savings_pct(await load_effective_cq(db))

        return {
            "counts": counts,
            "summary": {
                "files_to_convert": counts["needs_conversion"],
                "audio_cleanup": counts["audio_cleanup"],
                "unknown_language": counts["unknown_language"],
                "ignored_count": counts["ignored"],
                "estimated_savings_bytes": int(needs_conversion_bytes * est_pct),
                "total_size": extra["total_size"] or 0,
            },
        }
    finally:
        await db.close()


# F7 (v0.10.0): every completed conversion (45k+ jobs on a big install) was
# read into the "converted" sets on every folder expand, title view and Add
# to Queue — ~60 ms on the event loop each time. They're kept until a
# completed job changes: triggers on `jobs` (database.py) count every such
# change, whoever makes it. Keyed by the database file too (tests, and a
# restored backup replaces the file in place).
_converted_cache: dict = {"key": None, "paths": set(), "folders": set()}


async def _converted_key(db):
    try:
        async with db.execute("SELECT n FROM change_counters WHERE name = 'completed_jobs'") as cur:
            row = await cur.fetchone()
        async with db.execute("PRAGMA database_list") as cur:
            db_file = next((r[2] for r in await cur.fetchall() if r[1] == "main"), "")
    except Exception:
        return None  # e.g. a restored backup from before the counter
    if row is None or not db_file:
        return None
    return db_file, os.stat(db_file).st_ino, row[0]


async def _converted_sets(db) -> tuple[set[str], set[str]]:
    """Paths of completed conversions that saved space (output and original),
    and their parent folders (with trailing slash). Don't modify them."""
    key = await _converted_key(db)
    if key is not None and _converted_cache["key"] == key:
        return _converted_cache["paths"], _converted_cache["folders"]
    paths: set[str] = set()
    folders: set[str] = set()
    async with db.execute(
        "SELECT file_path, original_file_path FROM jobs WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0"
    ) as cur:
        for r in await cur.fetchall():
            for fp in (r[0], r[1]):
                if fp:
                    paths.add(fp)
                    folders.add(fp.rsplit("/", 1)[0] + "/" if "/" in fp else "")
    _converted_cache.update(key=key, paths=paths, folders=folders)
    return paths, folders


async def _title_metadata(db) -> tuple[dict, dict]:
    """The Poster grid's per-title metadata (folder -> year, rating, genres,
    network, status) and the media servers' genres (folder/ -> {genre}),
    for Advanced Search's title conditions (v0.10.0)."""
    meta: dict = {}
    genres: dict[str, set] = {}
    try:
        async with db.execute("SELECT folder_path, year, rating, genres, network, status FROM poster_cache") as cur:
            for r in await cur.fetchall():
                meta[r["folder_path"]] = (r["year"], r["rating"], r["genres"], r["network"], r["status"])
        async with db.execute(
            "SELECT folder_path, metadata_value FROM plex_metadata_cache WHERE metadata_type = 'genre'"
        ) as cur:
            for r in await cur.fetchall():
                folder = (r["folder_path"] or "").rstrip("/") + "/"
                genres.setdefault(folder, set()).add((r["metadata_value"] or "").strip().lower())
    except Exception as exc:
        print(f"[SCAN] Title metadata unavailable: {exc}", flush=True)
    return meta, genres


async def _build_enrichment_context(db, titles: bool = False) -> dict:
    """Build shared context for enriching scan results (used by results, tree, files endpoints).
    `titles`: also the title metadata (FilterExpr.needs_titles)."""
    import bisect
    from datetime import datetime, timedelta, timezone

    LOW_BITRATE_THRESHOLD = LOW_BITRATE
    HIGH_BITRATE_THRESHOLD = HIGH_BITRATE

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

    # Converted: both exact paths and parent folders from jobs with savings
    converted_paths, converted_folders = await _converted_sets(db)

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

    ctx = {
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
    if titles:
        ctx["title_meta"], ctx["server_genres"] = await _title_metadata(db)
    return ctx


def _enrich_row_minimal(row: dict, ctx: dict) -> dict:
    """Like _enrich_row but skips expensive json.loads on audio/subtitle track JSON.

    Use this when you only need the filter-relevant fields (no track lists), e.g.
    when resolving folder selections into file paths before queueing/estimating.
    """
    fp = row["file_path"]
    sz = row["file_size"] or 0
    dur = row["duration"] or 0
    disc_type = row.get("disc_type")

    is_ignored = row_ignored(row, ctx)
    low_bitrate = row_low_bitrate(row)

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
        "converted": row_converted(row, ctx),
        "low_bitrate": low_bitrate,
        "duration": dur,
        "file_mtime": row.get("file_mtime"),
        "probe_status": row.get("probe_status", "ok"),
        "probe_error": row.get("probe_error"),
        "video_height": row.get("video_height", 0),
        "video_width": row.get("video_width", 0),  # v0.10.0 (SC-22)
        "hdr_format": row.get("hdr_format"),  # v0.10.0
        "plex_watch_status": row_watch_status(row, ctx),
        "duplicate_count": row.get("duplicate_count", 0),
        "duplicate_group": row.get("duplicate_group"),
        "vmaf_score": row.get("vmaf_score"),
        "language_source": row.get("language_source", "heuristic"),
        "health_status": row.get("health_status"),
        "health_check_type": row.get("health_check_type"),
        "health_checked_at": row.get("health_checked_at"),
        # Type filter (movie/tv/other) — combines filename-bracket detection
        # with the containing media-dir's user-set label. v0.3.76+.
        "dir_type": row_type(row, ctx),
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

    is_ignored = row_ignored(row, ctx)
    low_bitrate = row_low_bitrate(row)

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
        "converted": row_converted(row, ctx),
        "low_bitrate": low_bitrate,
        "duration": dur,
        "file_mtime": row.get("file_mtime"),
        "probe_status": row.get("probe_status", "ok"),
        "probe_error": row.get("probe_error"),
        "video_height": row.get("video_height", 0),
        "video_width": row.get("video_width", 0),  # v0.10.0 (SC-22)
        "hdr_format": row.get("hdr_format"),  # v0.10.0
        "plex_watch_status": row_watch_status(row, ctx),
        "duplicate_count": row.get("duplicate_count", 0),
        "duplicate_group": row.get("duplicate_group"),
        "vmaf_score": row.get("vmaf_score"),
        "audio_tracks": json.loads(row.get("audio_tracks_json") or "[]"),
        "subtitle_tracks": json.loads(row.get("subtitle_tracks_json") or "[]"),
        "language_source": row.get("language_source", "heuristic"),
        "is_dubbed_flag": row.get("is_dubbed_flag", 0),
        # Health-check status (the badge; the "corrupt" filter reads the row).
        "health_status": row.get("health_status"),
        "health_check_type": row.get("health_check_type"),
        "health_checked_at": row.get("health_checked_at"),
        # Type filter (movie/tv/other) — combines filename-bracket detection
        # with the containing media-dir's user-set label. v0.3.76+.
        "dir_type": row_type(row, ctx),
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
    probe_error,
    COALESCE(video_height, 0) as video_height,
    COALESCE(video_width, 0) as video_width,
    hdr_format,
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

# F18 (v0.10.0): temp outputs and AppleDouble files were also filtered out
# here by three `NOT LIKE '%...%'` tests on every row of every list query
# (a quarter of a full listing's time); the scan writer now never stores
# them (database.is_temp_path) and older rows are removed once at startup.
_SCAN_WHERE = LISTED
# The same, for queries scoped to folders by a file_path range (F6). The
# unary "+" keeps SQLite off idx_scan_results_removed: without ANALYZE stats
# it prefers that equality to the range, and nearly every row matches it, so
# a folder expand read the whole table. Not for unscoped queries — it also
# rules out the partial indexes (WHERE removed_from_list = 0).
_SCAN_WHERE_IN_FOLDERS = "+" + _SCAN_WHERE


async def _get_converted_folders(db) -> set[str]:
    """Return the set of parent folder paths (with trailing slash) where Shrinkerr
    has successfully converted at least one file. Used to infer that other HEVC
    files in the same folder are 'already converted'."""
    return (await _converted_sets(db))[1]


@router.get("/filter-counts")
async def get_filter_counts(filter: str = "all"):
    """The pills' counts under the active filter: what clicking each would
    give (v0.10.0). With no filter, the library-wide counts."""
    expr = parse_filter(filter)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        if expr.is_all:
            counts, _ = await count_all(db, ctx)
            return {"counts": counts}
        return {"counts": await facet_counts(db, ctx, expr)}
    finally:
        await db.close()


@router.get("/tree")
async def get_scan_tree(filter: str = "all"):
    """Return folder hierarchy with aggregated counts/sizes.

    Filters SQL can settle run in the query; the enrichment context is only
    built when a filter needs it (ignored, queued, Plex, type...).
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        expr = parse_filter(filter)
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles) if expr.needs_ctx else None
        # The folder sums need path, size, mtime and the savings estimate;
        # the rest is what the Python filters read.
        async with db.execute(
            "SELECT file_mtime, COALESCE(video_conv_savings_bytes, 0) AS est_savings, "
            f"{PY_COLUMNS}{expr.select_sql} "
            f"FROM scan_results WHERE {_SCAN_WHERE}{expr.where_sql}",
            [*expr.select_params, *expr.where_params],
        ) as cur:
            rows = await cur.fetchall()

        # Files directly at a media root (no title folder) are grouped under
        # their full file path, so each stray file becomes its own "folder"
        # entry. Without this, they'd collapse under `/media` as a single row
        # whose prefix match then pulls in every sibling title.
        async with db.execute("SELECT path FROM media_dirs") as cur:
            media_roots = {r["path"].rstrip("/") for r in await cur.fetchall()}

        # Group by parent folder, applying the filters SQL couldn't
        folders: dict[str, dict] = {}
        for row in rows:
            r = dict(row)
            if expr.needs_ctx and not expr.post_filter(r, ctx):
                continue
            fp = r["file_path"]
            sz = r["file_size"] or 0
            est = r["est_savings"]

            parent = fp.rsplit("/", 1)[0] if "/" in fp else ""
            if parent not in folders:
                folders[parent] = {
                    "path": parent,
                    "file_count": 0,
                    "total_size": 0,
                    "newest_mtime": 0,
                    # What converting it would save (the "Savings" sort).
                    "est_savings": 0,
                }
            fd = folders[parent]
            fd["file_count"] += 1
            fd["total_size"] += sz
            fd["est_savings"] += est
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
                    "est_savings": est,
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
        expr = parse_filter(filter)
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        under_sql, under_params = prefix_clause([prefix.rstrip("/") + "/"])
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS}{expr.select_sql} FROM scan_results
                WHERE {_SCAN_WHERE_IN_FOLDERS}
                  AND (file_path = ? OR {under_sql}){expr.where_sql}
                ORDER BY file_path ASC""",
            (*expr.select_params, prefix, *under_params, *expr.where_params),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return [_enrich_row(r, ctx) for r in rows if expr.post_filter(r, ctx)]
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
        expr = parse_filter(filter)
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)

        # Direct children of the folder OR an exact file match for stray-file
        # pseudo-folders that are keyed by the file path itself.
        folder_prefix = folder.rstrip("/") + "/"
        under_sql, under_params = prefix_clause([folder_prefix])
        # Direct children: under the folder (an index range, F6) with no
        # further "/" after the folder's own.
        async with db.execute(
            f"""SELECT {_SCAN_SELECT_COLS}{expr.select_sql} FROM scan_results
                WHERE {_SCAN_WHERE_IN_FOLDERS}
                  AND (
                    file_path = ?
                    OR ({under_sql} AND instr(substr(file_path, ?), '/') = 0)
                  ){expr.where_sql}
                ORDER BY file_path ASC""",
            (*expr.select_params, folder, *under_params, len(folder_prefix) + 1, *expr.where_params),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return [_enrich_row(r, ctx) for r in rows if expr.post_filter(r, ctx)]
    finally:
        await db.close()


async def _stored_track_lists(file_path: str) -> tuple[list, list]:
    """The stored audio and subtitle track lists of a file ([] when none)."""
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT audio_tracks_json, subtitle_tracks_json FROM scan_results WHERE file_path = ?", (file_path,)
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    out = []
    for js in (row or (None, None)):
        try:
            out.append(json.loads(js or "[]"))
        except (ValueError, TypeError):
            out.append([])
    return out[0], out[1]


# ── Re-applying the language rules after a settings change (v0.10.0) ─────
# Changing the languages to keep (or the other track settings) used to reach
# only files scanned afterwards. Now the scanned files follow too — except
# tracks whose keep/remove the user chose by hand: those marked "manual", and
# (edits made before the mark existed) any track the old settings wouldn't
# have given its current keep/remove.

_reclass_lock = asyncio.Lock()
_background_tasks: set = set()


def _reapply_track_rules(row: dict, old_rules, new_rules) -> dict | None:
    """A _write_lang_batch item for a row whose tracks change under the new
    rules, or None."""
    from backend.scanner import classify_audio_tracks, classify_subtitle_tracks
    try:
        audio = json.loads(row["audio_tracks_json"] or "[]")
        subs = json.loads(row["subtitle_tracks_json"] or "[]")
    except (ValueError, TypeError):
        return None
    native, dur = row["native_language"] or "und", row["duration"] or 0

    def classify(tracks, kind, rules):
        copies = [dict(t) for t in tracks]
        if kind == "audio":
            return classify_audio_tracks(copies, native, dur, rules=rules)
        return classify_subtitle_tracks(copies, native, rules=rules)

    changed = False
    for tracks, kind in ((audio, "audio"), (subs, "subs")):
        if not tracks:
            continue
        old = {t.stream_index: t.keep for t in classify(tracks, kind, old_rules)}
        new = {t.stream_index: (t.keep, t.locked) for t in classify(tracks, kind, new_rules)}
        for t in tracks:
            si = t.get("stream_index")
            if t.get("manual") or si not in old or si not in new:
                continue
            if bool(t.get("keep", True)) != old[si]:
                continue  # not what the old rules said: chosen by hand
            if (bool(t.get("keep", True)), bool(t.get("locked", False))) != new[si]:
                t["keep"], t["locked"] = new[si]
                changed = True
    if not changed or (audio and not any(t.get("keep", True) for t in audio)):
        return None  # nothing to do — and never leave a file without audio
    und = 1 if any((t.get("language") or "und").lower() == "und" for t in audio + subs) else 0
    return {"rid": row["id"], "a_json": json.dumps(audio),
            "s_json": json.dumps(subs) if row["subtitle_tracks_json"] is not None else None,
            "rem_a": removable_audio_flag(audio, native),
            "rem_s": 1 if any(not t.get("keep", True) for t in subs) else 0, "und": und}


async def reapply_track_rules(old_rules) -> int:
    """Bring every scanned file's track keep/remove in line with the saved
    rules, which were `old_rules` before the change. Waits for a running scan
    (its worker uses the rules it started with) and for an earlier pass.
    Returns the number of files changed."""
    from backend.scanner import current_track_rules
    async with _reclass_lock:
        while scan_is_actively_running():
            await asyncio.sleep(5)
        new_rules = current_track_rules()
        if new_rules == old_rules:
            return 0
        changed, last_id = 0, 0
        while True:
            db = await connect_db()
            try:
                async with db.execute(
                    "SELECT id, audio_tracks_json, subtitle_tracks_json, native_language, duration "
                    f"FROM scan_results WHERE {_SCAN_WHERE} AND id > ? ORDER BY id LIMIT 1000", (last_id,),
                ) as cur:
                    rows = [dict(r) for r in await cur.fetchall()]
            finally:
                await db.close()
            if not rows:
                break
            last_id = rows[-1]["id"]
            # CPU-bound (JSON + classification): off the event loop.
            pending = await asyncio.to_thread(
                lambda: [it for it in (_reapply_track_rules(r, old_rules, new_rules) for r in rows) if it])
            if pending:
                await _write_lang_batch(pending)
                changed += len(pending)
        if changed:
            # Pending jobs follow their files (v0.10.0).
            from backend.routes.jobs import refresh_pending_jobs
            jobs = await refresh_pending_jobs()
            print(f"[SCAN] Track settings changed: updated {changed} file(s) and {jobs} pending job(s), "
                  "keeping choices made by hand", flush=True)
            await ws_manager.send_scan_results_changed(added=0, removed=0)
        return changed


def schedule_track_rules_update(old_rules) -> None:
    """reapply_track_rules() in the background (a settings save)."""
    task = asyncio.create_task(reapply_track_rules(old_rules))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def realign_audio_flags() -> int:
    """Recompute every scanned file's "has audio work" flag (the Audio cleanup
    filter) with scanner.removable_audio_flag, the queue's rule — after the
    reorder setting changes, and once for rows written under the old rules
    (v0.10.0). Pending jobs follow. Returns the files changed."""
    async with _reclass_lock:
        changed, last_id = 0, 0
        while True:
            db = await connect_db()
            try:
                async with db.execute(
                    "SELECT id, audio_tracks_json, native_language, has_removable_tracks_flag "
                    f"FROM scan_results WHERE {_SCAN_WHERE} AND id > ? ORDER BY id LIMIT 1000", (last_id,),
                ) as cur:
                    rows = [dict(r) for r in await cur.fetchall()]
            finally:
                await db.close()
            if not rows:
                break
            last_id = rows[-1]["id"]

            def stale() -> list[tuple[int, int]]:
                out = []
                for r in rows:
                    try:
                        flag = removable_audio_flag(json.loads(r["audio_tracks_json"] or "[]"), r["native_language"])
                    except (ValueError, TypeError):
                        continue
                    if flag != (r["has_removable_tracks_flag"] or 0):
                        out.append((flag, r["id"]))
                return out
            updates = await asyncio.to_thread(stale)
            if updates:
                db = await connect_db()
                try:
                    await db.executemany("UPDATE scan_results SET has_removable_tracks_flag = ? WHERE id = ?", updates)
                    await db.commit()
                finally:
                    await db.close()
                changed += len(updates)
        if changed:
            from backend.routes.jobs import refresh_pending_jobs
            jobs = await refresh_pending_jobs()
            print(f"[SCAN] Audio cleanup flag re-checked: {changed} file(s) and {jobs} pending job(s) updated",
                  flush=True)
            await ws_manager.send_scan_results_changed(added=0, removed=0)
        return changed


async def realign_audio_flags_once() -> int:
    """realign_audio_flags() once per install (startup, settings sentinel).
    _v2: the first pass (449af9e) read the stored track order, which lists
    the original language first, so it never found a reorder."""
    sentinel = "audio_flags_realigned_v2"
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (sentinel,)) as cur:
            if await cur.fetchone():
                return 0
    finally:
        await db.close()
    changed = await realign_audio_flags()
    db = await connect_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'", (sentinel,))
        await db.commit()
    finally:
        await db.close()
    return changed


def schedule_audio_flags_realign() -> None:
    """realign_audio_flags() in the background (the reorder setting changed)."""
    task = asyncio.create_task(realign_audio_flags())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _paths_matching(filter: str, folders: list[str] | None = None) -> list[str]:
    """Paths in the Scanner list matching `filter`: under any of `folders`
    (paths ending in "/"), or the whole list when None. For Add to Queue,
    estimates and health checks on selected folders or "select all"."""
    expr = parse_filter(filter)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles) if expr.needs_ctx else None
        if folders is None:
            scopes: list = [(_SCAN_WHERE, [])]
        else:
            # Chunked: 1000+ folders in one query exceeded SQLite's
            # expression-depth limit and the action did nothing (v0.5.23).
            scopes = []
            for i in range(0, len(folders), 800):
                under_sql, under_params = prefix_clause(folders[i:i + 800])  # index ranges (F6)
                scopes.append((f"{_SCAN_WHERE_IN_FOLDERS} AND ({under_sql})", under_params))
        paths: list[str] = []
        for where, params in scopes:
            async with db.execute(
                f"SELECT {PY_COLUMNS}{expr.select_sql} FROM scan_results WHERE {where}{expr.where_sql}",
                (*expr.select_params, *params, *expr.where_params),
            ) as cur:
                for row in await cur.fetchall():
                    r = dict(row)
                    if expr.post_filter(r, ctx):
                        paths.append(r["file_path"])
        return paths
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
        if f and not t.get("manual"):  # a choice made by hand stays (v0.10.0)
            t["keep"], t["locked"] = f
    if raw_audio and not any(t.get("keep", True) for t in raw_audio):
        # The choices removed every track the rules keep: a file keeps audio.
        first = next((t for t in raw_audio if a_flags.get(t.get("stream_index"), (False,))[0]), raw_audio[0])
        first["keep"] = True
    for t in raw_subs:
        f = s_flags.get(t.get("stream_index"))
        if f and not t.get("manual"):
            t["keep"], t["locked"] = f
    has_rem_a = removable_audio_flag(raw_audio, native)
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


def _keeps_from_normalized_codes(row) -> dict | None:
    """A _write_lang_batch item turning removals into keeps where a track only
    looked removable because its language code was spelled differently ("is"
    vs "ice", "de-DE" vs "ger") — or None when nothing changes. Never turns a
    keep into a removal (SC-08, v0.10.0)."""
    reclass = _reclassify_keep_flags(
        row["audio_tracks_json"], row["subtitle_tracks_json"], row["native_language"], row["duration"])
    if not reclass:
        return None
    try:
        old_a = json.loads(row["audio_tracks_json"] or "[]")
        old_s = json.loads(row["subtitle_tracks_json"] or "[]")
    except (ValueError, TypeError):
        return None
    changed = False
    for old, new in zip(old_a + old_s, json.loads(reclass[0]) + json.loads(reclass[1])):
        if not old.get("keep", True) and new.get("keep"):
            old["keep"] = True
            changed = True
    if not changed:
        return None
    return {
        "rid": row["id"], "a_json": json.dumps(old_a), "s_json": json.dumps(old_s),
        "rem_a": removable_audio_flag(old_a, row["native_language"]),
        "rem_s": 1 if any(not t.get("keep", True) for t in old_s) else 0,
        "und": 1 if any((t.get("language") or "und").lower() == "und" for t in old_a + old_s) else 0,
    }


async def backfill_normalized_language_keeps() -> int:
    """One-time pass after language codes became normalized (SC-08, v0.10.0):
    rows classified earlier can have a track marked for removal only because
    its code was spelled differently from the native / keep language — e.g.
    Bazarr's `.is.srt` on an Icelandic film. Turns those removals into keeps
    and never the reverse, so it can't add removals or undo a user's choice.
    Guarded by a settings sentinel; the JSON work runs off the event loop."""
    from backend.database import connect_db
    sentinel = "lang_normalized_keeps_done"
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (sentinel,)) as cur:
            if await cur.fetchone():
                return 0
        async with db.execute(
            "SELECT id, audio_tracks_json, subtitle_tracks_json, native_language, duration "
            "FROM scan_results WHERE removed_from_list = 0 "
            "AND (has_removable_tracks_flag = 1 OR has_removable_subs_flag = 1)"
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()

    pending = await asyncio.to_thread(
        lambda: [it for it in (_keeps_from_normalized_codes(r) for r in rows) if it])
    for i in range(0, len(pending), 500):
        await _write_lang_batch(pending[i:i + 500])

    db = await connect_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'", (sentinel,))
        await db.commit()
    finally:
        await db.close()
    if pending:
        print(f"[METADATA] Kept {len(pending)} title(s)' tracks that only looked removable "
              f"because of how their language code was spelled", flush=True)
    return len(pending)


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
            "SELECT value FROM settings WHERE key = 'reclass_authoritative_native_done_v2'"
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
            "('reclass_authoritative_native_done_v2', '1') "
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
        raise ApiError(status_code=409, detail="Metadata refresh already in progress", code="scan.metadataRefreshRunning")
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


async def _save_track_edit(result_id: int, column: str, flag_column: str, edited_json: str) -> None:
    """Persist a keep/remove edit, marking the tracks it changed as chosen by
    hand (rescans and settings changes leave those alone, v0.10.0) and
    keeping the row's "has removable" flag in step (it went stale)."""
    try:
        edited = json.loads(edited_json or "[]")
    except (ValueError, TypeError):
        raise ApiError(status_code=400, detail="That track list couldn't be read.", code="scan.invalidTracks")
    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute(
            f"SELECT {column}, file_path, native_language FROM scan_results WHERE id = ?", (result_id,),
        ) as cur:
            row = await cur.fetchone()
        try:
            stored = json.loads(row[0] or "[]") if row else []
        except (ValueError, TypeError):
            stored = []
        edited = mark_manual_choices(edited, stored)
        if column == "audio_tracks_json":
            removable = removable_audio_flag(edited, row[2] if row else None)
        else:
            removable = 1 if any(not t.get("keep", True) for t in edited) else 0
        await db.execute(
            f"UPDATE scan_results SET {column} = ?, {flag_column} = ? WHERE id = ?",
            (json.dumps(edited), removable, result_id),
        )
        await db.commit()
    finally:
        await db.close()
    # A pending job for the file follows the edit (v0.10.0).
    if row:
        from backend.routes.jobs import refresh_pending_jobs
        await refresh_pending_jobs([row[1]])


@router.put("/results/{result_id}/tracks")
async def update_audio_tracks(result_id: int, req: UpdateTracksRequest):
    """Persist audio track keep/remove changes to the DB."""
    await _save_track_edit(result_id, "audio_tracks_json", "has_removable_tracks_flag", req.audio_tracks_json)
    return {"status": "updated", "id": result_id}


class UpdateSubTracksRequest(BaseModel):
    subtitle_tracks_json: str


@router.put("/results/{result_id}/subtitle-tracks")
async def update_subtitle_tracks(result_id: int, req: UpdateSubTracksRequest):
    """Persist subtitle track keep/remove changes to the DB."""
    await _save_track_edit(result_id, "subtitle_tracks_json", "has_removable_subs_flag", req.subtitle_tracks_json)
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
        raise ApiError(status_code=409, detail="Scan already in progress", code="scan.alreadyRunning")
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
        raise ApiError(400, f"Invalid file path: {exc}", code="scan.invalidFilePath", params={"error": str(exc)})
    resolved_target_str = str(resolved_target)

    def _is_inside(child: str, parent: str) -> bool:
        try:
            common = os.path.commonpath([child, str(_P(parent).resolve(strict=False))])
        except ValueError:
            return False  # different drives / mount points
        return common == str(_P(parent).resolve(strict=False))

    if not any(_is_inside(resolved_target_str, d) for d in dirs):
        raise ApiError(403, "File is not under a configured media directory", code="scan.fileNotInMediaDir")

    # Use the resolved path downstream so an attacker can't smuggle a path
    # with traversal components past the DB lookups either.
    file_path = resolved_target_str

    # Check file exists (on the NAS: in a thread, SC-26)
    if not await asyncio.to_thread(os.path.isfile, file_path):
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
        await asyncio.to_thread(send2trash, file_path)  # can be a cross-device move
    except Exception as exc:
        raise ApiError(500, f"Failed to trash file: {exc}", code="scan.trashFailed", params={"error": str(exc)})

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


