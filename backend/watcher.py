"""Background file watcher — periodically checks media dirs for new, changed, or deleted files."""

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiosqlite

from backend.config import settings
from backend.database import DB_PATH


class FileWatcher:
    def __init__(self, db_path: str, interval_minutes: int = 5):
        self.db_path = db_path
        self.interval = interval_minutes * 60
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.new_files_count = 0  # Tracks unseen new files since last scanner page visit
        self._probe_failures: set[str] = set()  # Files that failed ffprobe — skip on future cycles
        self._last_disk_alert: float = 0  # Cooldown for disk space alerts
        # Last (ignored, probe_failures, to_process) tuple we logged for the
        # "Pre-filtered" line. Used to deduplicate identical states cycle to
        # cycle so a stable backlog doesn't spam the log every 5 minutes.
        self._last_pre_filtered_log: Optional[tuple[int, int, int]] = None

    def start(self) -> None:
        if self._running and self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        print(f"[WATCHER] Started, checking every {self.interval // 60} minutes", flush=True)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def clear_new_count(self) -> None:
        """Clear the new files counter (called when user visits scanner page)."""
        self.new_files_count = 0

    async def _get_known_files(self) -> set[str]:
        """Get all file paths from scan_results."""
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT file_path FROM scan_results") as cur:
                rows = await cur.fetchall()
                return {r["file_path"] for r in rows}
        finally:
            await db.close()

    async def _remove_stale_entries(self, stale_paths: list[str]) -> int:
        """Remove scan_results entries for files that no longer exist on disk.

        Deletes by file_path (not ID) so that rows whose file_path was updated
        by the queue worker (e.g. x264→x265 rename) are not accidentally removed.
        """
        if not stale_paths:
            return 0
        db = await aiosqlite.connect(self.db_path)
        try:
            placeholders = ",".join("?" * len(stale_paths))
            result = await db.execute(
                f"DELETE FROM scan_results WHERE file_path IN ({placeholders})",
                stale_paths,
            )
            await db.commit()
            return result.rowcount
        finally:
            await db.close()

    async def _scan_new_files(self, new_files: list[str], ignored_folders: list[str] | None = None) -> int:
        """Probe and add new files to scan_results."""
        if not new_files:
            return 0

        from backend.scanner import probe_file, detect_native_language, is_x264, is_x265, is_av1
        from backend.scanner import classify_audio_tracks, classify_subtitle_tracks, estimate_savings
        from backend.models import ScannedFile

        # Check for ignored files
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT file_path FROM ignored_files") as cur:
                rows = await cur.fetchall()
                ignored_paths = {r["file_path"] for r in rows}
        finally:
            await db.close()

        # Load file age setting
        skip_age_minutes = 0
        try:
            db2 = await aiosqlite.connect(self.db_path)
            db2.row_factory = aiosqlite.Row
            try:
                async with db2.execute(
                    "SELECT key, value FROM settings WHERE key IN ('skip_files_newer_enabled', 'skip_files_newer_than_minutes')"
                ) as cur:
                    age_settings = {r["key"]: r["value"] for r in await cur.fetchall()}
                if age_settings.get("skip_files_newer_enabled", "false").lower() == "true":
                    skip_age_minutes = int(age_settings.get("skip_files_newer_than_minutes", "10"))
            finally:
                await db2.close()
        except Exception:
            pass

        import time as _time

        results = []
        new_file_paths = []
        skipped_ignored = 0
        skipped_probe = 0
        skipped_av1 = 0
        skipped_age = 0
        for file_path in new_files:
            if file_path in ignored_paths:
                skipped_ignored += 1
                continue

            if file_path in self._probe_failures:
                skipped_probe += 1
                continue

            # Skip recently modified files
            if skip_age_minutes > 0:
                try:
                    mtime = os.path.getmtime(file_path)
                    age_min = (_time.time() - mtime) / 60
                    if age_min < skip_age_minutes:
                        skipped_age += 1
                        continue
                except OSError:
                    pass

            probe = await probe_file(file_path)
            if probe is None:
                self._probe_failures.add(file_path)
                skipped_probe += 1
                continue

            video_codec = probe["video_codec"]
            raw_tracks = probe["audio_tracks"]
            duration = probe["duration"]
            file_size = probe["file_size"]

            if is_av1(video_codec):
                skipped_av1 += 1
                continue

            native_lang = detect_native_language(raw_tracks)
            language_source = "heuristic"

            # Try TMDB/TVDB lookup for accurate native language. Skip when
            # the file is inside an "Other"-typed media dir — those hold
            # non-cataloguable content and would just produce spurious matches.
            try:
                from backend.media_paths import is_other_typed_dir
                if not await is_other_typed_dir(str(file_path)):
                    from backend.metadata import lookup_original_language
                    api_lang = await asyncio.wait_for(
                        lookup_original_language(str(file_path)),
                        timeout=10,
                    )
                    if api_lang:
                        native_lang = api_lang
                        language_source = "api"
            except Exception:
                pass

            needs_conversion = is_x264(video_codec)
            audio_tracks = classify_audio_tracks(raw_tracks, native_lang)
            raw_subs = probe.get("subtitle_tracks", [])
            subtitle_tracks = classify_subtitle_tracks(raw_subs, native_lang)

            # Detect external subtitle files (.srt/.ass/.ssa/.sub/.vtt) alongside the video
            try:
                from backend.scanner import detect_external_subtitles
                ext_subs_raw = detect_external_subtitles(file_path)
                has_external_subs = len(ext_subs_raw) > 0
                if ext_subs_raw:
                    for i, es in enumerate(ext_subs_raw):
                        es["stream_index"] = -(i + 1)
                    ext_classified = classify_subtitle_tracks(ext_subs_raw, native_lang)
                    for cls_track, raw in zip(ext_classified, ext_subs_raw):
                        cls_track = cls_track.model_copy(update={
                            "external": True,
                            "external_path": raw["external_path"],
                        })
                        subtitle_tracks.append(cls_track)
            except Exception as exc:
                print(f"[WATCHER] External sub detection failed: {exc}", flush=True)
                has_external_subs = False

            tracks_to_remove = [t for t in audio_tracks if not t.keep]
            has_removable = len(tracks_to_remove) > 0
            has_removable_subs = any(not t.keep for t in subtitle_tracks)

            # Include x265 files so converted content shows with "x265 ✓" badge

            savings_bytes = estimate_savings(file_size, needs_conversion, tracks_to_remove, duration)

            # Get file modification time from disk
            try:
                file_mtime = os.path.getmtime(file_path)
            except OSError:
                file_mtime = None

            p = Path(file_path)
            scanned = ScannedFile(
                file_path=file_path,
                file_name=p.name,
                folder_name=p.parent.name,
                file_size=file_size,
                file_size_gb=round(file_size / (1024 ** 3), 3),
                video_codec=video_codec,
                needs_conversion=needs_conversion,
                audio_tracks=audio_tracks,
                subtitle_tracks=subtitle_tracks,
                native_language=native_lang,
                language_source=language_source,
                has_removable_tracks=has_removable,
                has_removable_subs=has_removable_subs,
                has_external_subs=has_external_subs,
                estimated_savings_bytes=savings_bytes,
                estimated_savings_gb=round(savings_bytes / (1024 ** 3), 3),
                file_mtime=file_mtime,
                duration=duration,
            )
            results.append(scanned)
            new_file_paths.append(file_path)

        if skipped_ignored or skipped_probe or skipped_av1:
            print(f"[WATCHER] Skipped: {skipped_ignored} ignored, {skipped_probe} probe failed, {skipped_av1} AV1", flush=True)

        if results:
            # Final defensive filter: re-query scan_results right before writing.
            # Conversion jobs can complete mid-probe-loop and update scan_results with
            # the new (renamed) file_path. If we don't filter here, those freshly-
            # converted files would hit the ON CONFLICT branch and incorrectly count
            # toward the new-files badge. (The SQL CASE in _write_batch_sync_inner
            # already prevents them from being flagged is_new, but the badge counter
            # still increments unless we filter here.)
            db_chk = await aiosqlite.connect(self.db_path)
            db_chk.row_factory = aiosqlite.Row
            try:
                result_paths = [s.file_path for s in results]
                placeholders = ",".join("?" * len(result_paths))
                async with db_chk.execute(
                    f"SELECT file_path FROM scan_results WHERE file_path IN ({placeholders})",
                    result_paths,
                ) as cur:
                    already_known = {r["file_path"] for r in await cur.fetchall()}
            finally:
                await db_chk.close()

            if already_known:
                before = len(results)
                results = [s for s in results if s.file_path not in already_known]
                skipped_race = before - len(results)
                if skipped_race > 0:
                    print(f"[WATCHER] Skipped {skipped_race} files that scan_results picked up mid-probe (post-conversion renames)", flush=True)

        if results:
            from backend.routes.scan import _write_batch
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            await _write_batch(self.db_path, results, now, mark_new=True)

            # Auto-ignore files in ignored folders
            if ignored_folders:
                from datetime import datetime as _dt, timezone as _tz
                auto_ignored = [
                    s.file_path for s in results
                    if any(s.file_path.startswith(folder) for folder in ignored_folders)
                ]
                if auto_ignored:
                    db2 = await aiosqlite.connect(self.db_path)
                    try:
                        _now = _dt.now(_tz.utc).isoformat()
                        for fp in auto_ignored:
                            await db2.execute(
                                "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
                                (fp, "folder_ignored", _now),
                            )
                        await db2.commit()
                    finally:
                        await db2.close()
                    print(f"[WATCHER] Auto-ignored {len(auto_ignored)} files in ignored folders", flush=True)
                    # Exclude auto-ignored from auto-queue
                    auto_ignored_set = set(auto_ignored)
                    results = [s for s in results if s.file_path not in auto_ignored_set]

            # Auto-queue if enabled
            await self._auto_queue_new_files(results)

        return len(results)

    async def _auto_queue_new_files(self, results: list) -> int:
        """Auto-enqueue new files that need work, if the setting is enabled."""
        # Check setting
        db = await aiosqlite.connect(self.db_path)
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'auto_queue_new'"
            ) as cur:
                row = await cur.fetchone()
                enabled = row and row[0].lower() == "true"
        finally:
            await db.close()

        if not enabled:
            return 0

        # Load default encoder settings
        db = await aiosqlite.connect(self.db_path)
        try:
            settings = {}
            async with db.execute("SELECT key, value FROM settings") as cur:
                rows = await cur.fetchall()
                for r in rows:
                    settings[r[0]] = r[1]
        finally:
            await db.close()

        encoder = settings.get("default_encoder", "nvenc")
        nvenc_preset = settings.get("nvenc_preset", "p6")
        nvenc_cq = int(settings.get("nvenc_cq", "20"))
        audio_codec = settings.get("audio_codec", "copy")
        audio_bitrate = int(settings.get("audio_bitrate", "128"))

        from backend.queue import JobQueue
        queue = JobQueue(self.db_path)

        queued = 0
        for scanned in results:
            tracks_to_remove = [t.stream_index for t in scanned.audio_tracks if not t.keep and not t.locked]
            has_removable = len(tracks_to_remove) > 0

            if not scanned.needs_conversion and not has_removable:
                continue

            if scanned.needs_conversion and has_removable:
                job_type = "combined"
            elif scanned.needs_conversion:
                job_type = "convert"
            else:
                job_type = "audio"

            await queue.add_job(
                file_path=scanned.file_path,
                job_type=job_type,
                encoder=encoder,
                audio_tracks_to_remove=tracks_to_remove,
                original_size=scanned.file_size,
                nvenc_preset=nvenc_preset,
                nvenc_cq=nvenc_cq,
                audio_codec=audio_codec,
                audio_bitrate=audio_bitrate,
            )
            queued += 1

        if queued:
            print(f"[WATCHER] Auto-queued {queued} new files", flush=True)
        return queued

    async def _refresh_metadata_for_files(self, file_paths: list[str]) -> int:
        """Do lazy metadata lookups for a batch of new files. Returns count updated."""
        if not file_paths:
            return 0

        try:
            from backend.metadata import lookup_original_language
            from backend.media_paths import is_other_typed_dir
        except ImportError:
            return 0

        updated = 0
        for file_path in file_paths[:10]:  # Max 10 per cycle
            # Skip "Other" dirs — TMDB matches against non-movie/non-tv
            # content produce spurious results.
            try:
                if await is_other_typed_dir(file_path):
                    continue
            except Exception:
                pass
            try:
                api_lang = await asyncio.wait_for(
                    lookup_original_language(file_path),
                    timeout=8,
                )
            except (asyncio.TimeoutError, Exception):
                api_lang = None

            if not api_lang:
                continue

            # Update the scan result with API language
            db = await aiosqlite.connect(self.db_path)
            try:
                await db.execute(
                    "UPDATE scan_results SET native_language = ? WHERE file_path = ?",
                    (api_lang, file_path),
                )
                await db.commit()
                updated += 1
            finally:
                await db.close()

            # Small delay between API calls to be nice
            await asyncio.sleep(1)

        if updated > 0:
            print(f"[WATCHER] Metadata: updated {updated}/{len(file_paths)} new files", flush=True)
        return updated

    async def _get_scanned_dirs(self) -> set[str]:
        """Get the set of top-level directories that have been scanned (have results in DB)."""
        db = await aiosqlite.connect(self.db_path)
        try:
            media_dirs = []
            # Watch only dirs the user has marked auto_scan=1 (default).
            # auto_scan=0 dirs (e.g. an NZBGet downloads folder added so
            # the post-processing webhook can queue from it) stay
            # webhook-eligible but invisible to the watcher. v0.3.49+.
            async with db.execute(
                "SELECT path FROM media_dirs WHERE enabled = 1 AND auto_scan = 1"
            ) as cur:
                rows = await cur.fetchall()
                media_dirs = [row[0] for row in rows]

            scanned = set()
            for d in media_dirs:
                async with db.execute(
                    "SELECT 1 FROM scan_results WHERE file_path LIKE ? LIMIT 1",
                    (d.rstrip("/") + "/%",),
                ) as cur:
                    if await cur.fetchone():
                        scanned.add(d)
            return scanned
        finally:
            await db.close()

    async def check_once(self) -> dict:
        """Run a single check cycle. Only monitors directories that have been scanned."""
        scanned_dirs = await self._get_scanned_dirs()
        if not scanned_dirs:
            return {"checked": 0, "new": 0, "removed": 0}

        extensions = {ext.lower() for ext in settings.video_extensions}
        known_paths = await self._get_known_files()

        def _walk_dirs():
            result: set[str] = set()
            for dir_path in scanned_dirs:
                dir_p = Path(dir_path)
                if not dir_p.exists():
                    continue
                for root, _dirs, files in os.walk(dir_path):
                    for name in files:
                        # Skip temp files from active conversions/remuxing
                        if ".converting." in name or ".remuxing." in name:
                            continue
                        # Skip hidden / dot files. The big offender on
                        # Mac-formatted volumes is AppleDouble companions
                        # (`._<name>.mkv`) — same extension as the video
                        # they shadow but contain HFS+ resource-fork data,
                        # not video. ffprobe rightly fails on them and the
                        # watcher used to log 200+ "probe failed" per cycle
                        # for these. Matches scanner.py's filter so the
                        # watcher and the initial scan agree on visibility.
                        if name.startswith("."):
                            continue
                        if Path(name).suffix.lower() in extensions:
                            result.add(str(Path(root) / name))
            return result

        disk_files = await asyncio.get_event_loop().run_in_executor(None, _walk_dirs)

        new_files_all = disk_files - known_paths
        stale_path_set = known_paths - disk_files

        # Pre-filter ignored files and recently converted files
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT file_path FROM ignored_files") as cur:
                rows = await cur.fetchall()
                ignored_paths = {r["file_path"] for r in rows}
            # Re-check scan_results for any paths updated mid-cycle (e.g. jobs that
            # just finished and renamed files). This catches the race where known_paths
            # was loaded before a job completed and updated the file_path.
            if new_files_all:
                async with db.execute("SELECT file_path FROM scan_results") as cur:
                    rows = await cur.fetchall()
                    current_known = {r["file_path"] for r in rows}
                # Remove any "new" files that are actually already tracked
                new_files_all = new_files_all - current_known
        finally:
            await db.close()

        exclude = ignored_paths | self._probe_failures
        new_files = [f for f in new_files_all if f not in exclude]
        skipped_ignored_total = len([f for f in new_files_all if f in ignored_paths])
        skipped_probe_total = len([f for f in new_files_all if f in self._probe_failures])
        # Deduplicate the log line: only emit when at least one of the three
        # numbers changed since last cycle. A stable backlog (e.g. 600
        # always-failing companion files plus zero new content) used to spam
        # this every 5 minutes with the exact same numbers — non-actionable
        # noise. v0.3.34+.
        current_state = (skipped_ignored_total, skipped_probe_total, len(new_files))
        if (skipped_ignored_total > 0 or skipped_probe_total > 0) and current_state != self._last_pre_filtered_log:
            print(f"[WATCHER] Pre-filtered: {skipped_ignored_total} ignored, {skipped_probe_total} previous probe failures, {len(new_files)} to process", flush=True)
            self._last_pre_filtered_log = current_state

        # Collect folder-level ignores (paths ending with /) for auto-tagging new files
        ignored_folders = [p for p in ignored_paths if p.endswith("/")]

        removed = await self._remove_stale_entries(list(stale_path_set))
        added = await self._scan_new_files(new_files[:200], ignored_folders)

        # Track new files for the badge
        if added > 0:
            self.new_files_count += added

        remaining_new = max(0, len(new_files) - 200)

        if removed > 0 or added > 0:
            print(f"[WATCHER] Removed {removed} stale, added {added} new"
                  + (f" ({remaining_new} more pending)" if remaining_new > 0 else ""),
                  flush=True)
            # Tell connected clients (the Scanner page) that the file
            # tree changed, so they can re-fetch live instead of
            # requiring the user to navigate away and back. v0.3.64+.
            try:
                from backend.websocket import ws_manager
                await ws_manager.send_scan_results_changed(added=added, removed=removed)
            except Exception as exc:
                print(f"[WATCHER] WS broadcast failed (non-fatal): {exc}", flush=True)

        # Lazy metadata lookup for newly added files
        if added > 0:
            new_paths = new_files[:added]  # The files we just added
            await self._refresh_metadata_for_files(new_paths)

        # Check disk space and notify if low
        await self._check_disk_space(scanned_dirs)

        return {"checked": len(disk_files), "new": added, "removed": removed, "pending": remaining_new}

    async def _check_disk_space(self, dirs: list[str]) -> None:
        """Check disk free space and send notification if below threshold."""
        import shutil, time
        # Cooldown: don't alert more than once per hour
        if time.monotonic() - self._last_disk_alert < 3600:
            return
        try:
            db = await aiosqlite.connect(self.db_path)
            db.row_factory = aiosqlite.Row
            try:
                async with db.execute(
                    "SELECT value FROM settings WHERE key = 'disk_space_threshold_gb'"
                ) as cur:
                    row = await cur.fetchone()
                    threshold_gb = int(row["value"]) if row else 50
            finally:
                await db.close()

            threshold_bytes = threshold_gb * (1024 ** 3)
            checked: set[str] = set()
            for d in dirs:
                try:
                    usage = shutil.disk_usage(d)
                    # Avoid duplicate alerts for same mount point
                    mount_key = f"{usage.total}"
                    if mount_key in checked:
                        continue
                    checked.add(mount_key)
                    if usage.free < threshold_bytes:
                        from backend.notifications import send_notification
                        free_gb = usage.free / (1024 ** 3)
                        await send_notification("disk_low", "Low Disk Space",
                            f"Free space is {free_gb:.1f} GB (threshold: {threshold_gb} GB)",
                            {"Path": d, "Free": f"{free_gb:.1f} GB", "Total": f"{usage.total / (1024**4):.1f} TB"})
                        self._last_disk_alert = time.monotonic()
                        break  # One alert is enough
                except OSError:
                    pass
        except Exception as exc:
            print(f"[WATCHER] Disk space check failed: {exc}", flush=True)

    async def _run_loop(self) -> None:
        await asyncio.sleep(30)
        while self._running:
            # Skip cycle if a scan is running — avoid competing for ffprobe/DB I/O
            from backend.routes.scan import _scan_task
            if _scan_task is not None and not _scan_task.done():
                print("[WATCHER] Scan in progress, skipping cycle", flush=True)
            else:
                try:
                    await self.check_once()
                except Exception as exc:
                    print(f"[WATCHER] Error during check: {exc}", flush=True)
            await asyncio.sleep(self.interval)
