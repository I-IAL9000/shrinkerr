"""Background file watcher — periodically checks media dirs for new, changed, or deleted files."""

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiosqlite

from backend.config import settings
from backend.database import DB_PATH
from backend.scanner import _classify_disc, _disc_marker_path


def _safe_int(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


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

        Skips paths with a pending/running job: during conversion the original
        h264 file disappears from disk (rename) BEFORE the worker's post-
        conversion `UPDATE scan_results SET file_path=<h265>, converted=1`
        commits. Pre-v0.3.132 we'd race the worker — the watcher saw the
        h264 path missing from disk, DELETEd its scan_results row, and the
        worker's UPDATE then matched 0 rows. Net effect: the new h265 path
        ended up freshly INSERTed by the watcher with `is_new=1, converted=0`,
        which surfaced as "newly converted files counting as new files" on
        the Scanner page. Mirrors scan.py's full-rescan orphan cleanup,
        which already filters out active jobs. v0.3.132+.
        """
        if not stale_paths:
            return 0
        # v0.5.24: chunked the IN clause. A bulk filesystem change (mass
        # rename, mount swap, source-tree restructure) can yield 1000+
        # stale paths in one poll — older SQLite builds would error out
        # at 999 variables, and modern builds still benefit from smaller
        # per-statement plans.
        CHUNK = 900
        deleted = 0
        db = await aiosqlite.connect(self.db_path)
        try:
            for i in range(0, len(stale_paths), CHUNK):
                chunk = stale_paths[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                result = await db.execute(
                    f"""DELETE FROM scan_results
                        WHERE file_path IN ({placeholders})
                          AND file_path NOT IN (
                              SELECT file_path FROM jobs
                              WHERE status IN ('pending', 'running')
                          )""",
                    chunk,
                )
                deleted += result.rowcount or 0
            await db.commit()
            return deleted
        finally:
            await db.close()

    async def _scan_new_files(self, new_files: list[str], ignored_folders: list[str] | None = None) -> int:
        """Probe and add new files to scan_results."""
        if not new_files:
            return 0

        from backend.scanner import probe_file, detect_native_language, is_x264, is_x265, is_av1, codec_matches_source
        from backend.scanner import classify_audio_tracks, classify_subtitle_tracks, estimate_savings
        from backend.encoding_estimates import video_conv_savings_bytes
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

        # v0.5.22: load source_codecs once per poll so codec matching
        # matches what the scanner + webhook do. Pre-v0.5.22 the watcher
        # hardcoded `is_x264(video_codec)` — only H.264 was recognised as
        # "needs conversion", so MPEG-2 / MPEG-4 / VC-1 / WMV files
        # auto-discovered via filesystem watching never got a `convert`
        # job even though they were in the user's source_codecs list.
        # HEVC was unaffected (not in default source_codecs either way).
        source_codecs = ["h264", "mpeg2", "mpeg4", "vc1"]
        # v0.6.7: load global NVENC CQ once per cycle to match the scanner
        # / queue-estimate's CQ-calibrated savings curve. Pre-v0.6.7 this
        # path used a flat 0.30 default that disagreed with the modal.
        global_cq = 25
        try:
            import json as _json
            db3 = await aiosqlite.connect(self.db_path)
            try:
                async with db3.execute(
                    "SELECT value FROM settings WHERE key = 'source_codecs'"
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        source_codecs = _json.loads(row[0])
                async with db3.execute(
                    "SELECT value FROM settings WHERE key = 'nvenc_cq'"
                ) as cur:
                    cqrow = await cur.fetchone()
                    if cqrow and cqrow[0]:
                        try:
                            global_cq = int(cqrow[0])
                        except (TypeError, ValueError):
                            pass
            finally:
                await db3.close()
        except Exception:
            pass

        # v0.6.0: disc-folder discovery. When the watcher discovers a path
        # inside VIDEO_TS/ or BDMV/, or the disc-root folder itself, map
        # it to the disc-marker file the scanner expects. Deduplicate so
        # multiple inner-VOB discoveries collapse to a single marker.
        # The marker is what flows through the rest of the pipeline; the
        # internal VOB / M2TS files never appear as standalone scan items.
        # v0.6.1: the polling walk is now disc-aware (see _walk_dirs in
        # check_once), so the common case is already handled upstream;
        # this pre-pass remains as belt-and-suspenders for any non-walk-
        # sourced disc path that might land in new_files via future paths.
        from backend.scanner import _classify_disc, _disc_marker_path

        new_files_disc_adjusted: list[str] = []
        seen_discs: set[str] = set()
        for fp in new_files:
            p = Path(fp)
            # v0.7.0: .iso files are disc images. Unlike folder discs
            # (which we map to inner marker files), the ISO file itself
            # IS the scan item. Pass through unchanged; probe_file
            # handles ISO classification + routing.
            if p.suffix.lower() == ".iso":
                # explicit no-op — keep the .iso path as-is, let probe_file route
                new_files_disc_adjusted.append(fp)
                continue
            # Case A: path is inside VIDEO_TS or BDMV → map to disc-root's marker
            if any(part in ("VIDEO_TS", "BDMV") for part in p.parts):
                # Walk up to the disc-root (the folder CONTAINING VIDEO_TS/BDMV)
                disc_root = p
                while disc_root.parent != disc_root:
                    if disc_root.name in ("VIDEO_TS", "BDMV"):
                        disc_root = disc_root.parent
                        break
                    disc_root = disc_root.parent
                disc_type = _classify_disc(disc_root)
                if disc_type:
                    marker = str(_disc_marker_path(disc_root, disc_type))
                    if marker not in seen_discs:
                        new_files_disc_adjusted.append(marker)
                        seen_discs.add(marker)
                    continue  # drop the inner VOB/M2TS path only if mapped
            # Case B: path is the disc-root folder itself
            if p.is_dir():
                disc_type = _classify_disc(p)
                if disc_type:
                    marker = str(_disc_marker_path(p, disc_type))
                    if marker not in seen_discs:
                        new_files_disc_adjusted.append(marker)
                        seen_discs.add(marker)
                    continue
            # Default: regular file, pass through
            new_files_disc_adjusted.append(fp)

        new_files = new_files_disc_adjusted

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
                # v0.6.2: disc probes can fail silently. Surface them.
                if "/VIDEO_TS/VIDEO_TS.IFO" in file_path or "/BDMV/index.bdmv" in file_path.lower():
                    print(f"[WATCHER] !!! Disc probe FAILED: {file_path}", flush=True)
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

            # v0.5.22: was `is_x264(video_codec)` — only matched h264 and
            # silently classified MPEG-2 / MPEG-4 / VC-1 as "no
            # conversion needed" regardless of source_codecs.
            needs_conversion = codec_matches_source(video_codec, source_codecs)
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

            savings_bytes = estimate_savings(file_size, needs_conversion, tracks_to_remove, duration, cq=global_cq)
            video_conv_bytes = video_conv_savings_bytes(file_size, global_cq) if needs_conversion else 0

            p = Path(file_path)
            # For disc items the file_path is the marker (.../<Disc Root>/VIDEO_TS/VIDEO_TS.IFO
            # or .../<Disc Root>/BDMV/index.bdmv). The user-facing name should be the
            # disc-root folder (p.parent.parent.name), not "VIDEO_TS.IFO". v0.6.0+.
            # v0.7.2: helper handles ISO inputs correctly (parent vs parent.parent).
            from backend.scanner import _disc_display_name
            disc_type_val = probe.get("disc_type")
            display_name = _disc_display_name(p, disc_type_val)

            # Get file modification time from disk. For discs, the marker file
            # (VIDEO_TS.IFO / index.bdmv) keeps the original DVD/BDMV authoring
            # timestamp — often decades old — which makes "Newest" sort treat
            # freshly-added discs as ancient. Use the disc-root folder's mtime
            # instead, which reflects when the user actually copied the disc
            # into their library. v0.6.3+.
            try:
                if disc_type_val:
                    file_mtime = p.parent.parent.stat().st_mtime
                else:
                    file_mtime = os.path.getmtime(file_path)
            except OSError:
                file_mtime = None

            scanned = ScannedFile(
                file_path=file_path,
                file_name=display_name,
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
                video_conv_savings_bytes=video_conv_bytes,
                file_mtime=file_mtime,
                duration=duration,
                disc_type=disc_type_val,  # v0.6.0
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

            # v0.7.2: clear any stale health_status='corrupt' on disc rows
            # that just got re-discovered. A previous health-check during a
            # v0.6.x mid-conversion (VIDEO_TS deleted) leaves a corrupt
            # flag on the disc row; the fresh probe success means that
            # flag is wrong. No-op for non-disc rows (helper filters on
            # disc_type IS NOT NULL).
            from backend.scanner import _clear_stale_disc_health_status
            for scanned in results:
                if getattr(scanned, "disc_type", None):
                    await _clear_stale_disc_health_status(
                        self.db_path, scanned.file_path
                    )

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
        """Auto-enqueue new files that need work, if the setting is enabled.

        v0.5.0+: now goes through the rules engine (resolve_rules_for_batch)
        like every other queue-entry path. Priority precedence:
            rule.queue_priority  OR  settings.auto_queue_priority  OR  0
        Skip / ignore rule actions short-circuit enqueue.
        """
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

        # Load all settings (used as fallbacks when a rule doesn't override)
        db = await aiosqlite.connect(self.db_path)
        try:
            settings = {}
            async with db.execute("SELECT key, value FROM settings") as cur:
                rows = await cur.fetchall()
                for r in rows:
                    settings[r[0]] = r[1]
        finally:
            await db.close()

        default_encoder = settings.get("default_encoder", "nvenc")
        default_nvenc_preset = settings.get("nvenc_preset", "p6")
        default_nvenc_cq = _safe_int(settings.get("nvenc_cq", "20"), 20)
        default_libx265_preset = settings.get("libx265_preset", "medium")
        default_libx265_crf = _safe_int(settings.get("libx265_crf", "20"), 20)
        default_target_res = settings.get("target_resolution", "")
        default_audio_codec = settings.get("audio_codec", "copy")
        default_audio_bitrate = _safe_int(settings.get("audio_bitrate", "128"), 128)
        default_priority = max(0, min(2, _safe_int(settings.get("auto_queue_priority", "0") or 0, 0)))

        # Resolve rules for the whole batch at once (one DB hit instead of per-file).
        # extra_context=None because auto-queue has no nzbget category to surface.
        from backend.rule_resolver import resolve_rules_for_batch
        file_paths = [s.file_path for s in results]
        rule_results = await resolve_rules_for_batch(file_paths, extra_context=None)

        from backend.queue import JobQueue
        queue = JobQueue(self.db_path)

        queued = 0
        skipped_by_rule = 0
        for scanned in results:
            tracks_to_remove = [t.stream_index for t in scanned.audio_tracks
                                if not t.keep and not t.locked]
            has_removable = len(tracks_to_remove) > 0

            if not scanned.needs_conversion and not has_removable:
                continue

            # Skip / ignore actions from rules short-circuit enqueue.
            # Mirrors routes/jobs.py:946-948.
            rule = rule_results.get(scanned.file_path)
            if rule and rule.get("action") in ("skip", "ignore"):
                skipped_by_rule += 1
                continue

            # Determine job type
            if scanned.needs_conversion and has_removable:
                job_type = "combined"
            elif scanned.needs_conversion:
                job_type = "convert"
            else:
                job_type = "audio"

            # Resolve every kwarg via OR-cascade: rule wins, else global default.
            # NOTE: this is different from routes/jobs.py:973's `max(...)`
            # because auto-queue has no per-file user choice that should act
            # as a floor — the rule fully wins (including lowering).
            r = rule or {}
            encoder = r.get("encoder") or default_encoder
            nvenc_preset = r.get("nvenc_preset") or default_nvenc_preset
            nvenc_cq = r.get("nvenc_cq") if r.get("nvenc_cq") is not None else default_nvenc_cq
            libx265_preset = r.get("libx265_preset") or default_libx265_preset
            libx265_crf = r.get("libx265_crf") if r.get("libx265_crf") is not None else default_libx265_crf
            target_resolution = r.get("target_resolution") or default_target_res
            audio_codec = r.get("audio_codec") or default_audio_codec
            audio_bitrate = r.get("audio_bitrate") if r.get("audio_bitrate") is not None else default_audio_bitrate
            rule_pri = r.get("queue_priority")
            priority = rule_pri if rule_pri is not None else default_priority

            await queue.add_job(
                file_path=scanned.file_path,
                job_type=job_type,
                encoder=encoder,
                audio_tracks_to_remove=tracks_to_remove,
                original_size=scanned.file_size,
                nvenc_preset=nvenc_preset,
                nvenc_cq=nvenc_cq,
                libx265_preset=libx265_preset,
                libx265_crf=libx265_crf,
                target_resolution=target_resolution,
                audio_codec=audio_codec,
                audio_bitrate=audio_bitrate,
                priority=priority,
            )
            queued += 1

        if queued or skipped_by_rule:
            msg = f"[WATCHER] Auto-queued {queued} new files"
            if skipped_by_rule:
                msg += f" ({skipped_by_rule} skipped by rule)"
            print(msg, flush=True)
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

    async def _backfill_disc_languages_v065(self) -> None:
        """One-shot v0.6.5 migration: re-probe existing disc rows whose
        audio tracks are tagged 'und' so they pick up the new IFO/mpls
        language metadata. Tracked via settings flag
        'disc_lang_backfilled_v065'. Skips paths whose source has been
        deleted (stale rows are cleaned up by the normal stale-removal
        path)."""
        flag_key = "disc_lang_backfilled_v065"
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (flag_key,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["value"] == "true":
                return  # already done

            async with db.execute(
                "SELECT file_path FROM scan_results "
                "WHERE disc_type IS NOT NULL "
                "AND audio_tracks_json LIKE '%\"language\":\"und\"%'"
            ) as cur:
                candidates = [r["file_path"] for r in await cur.fetchall()]
        finally:
            await db.close()

        if not candidates:
            # Nothing to backfill; still set the flag so we don't re-query.
            await self._set_setting(flag_key, "true")
            return

        print(
            f"[WATCHER] v0.6.5 backfill: re-probing {len(candidates)} disc rows for language metadata",
            flush=True,
        )

        from pathlib import Path as _Path
        from backend.scanner import (
            probe_file as _probe_file,
            classify_audio_tracks as _classify_audio_tracks,
            classify_subtitle_tracks as _classify_subtitle_tracks,
            detect_native_language as _detect_native_language,
        )
        import json as _json

        updated = 0
        for fp in candidates:
            if not _Path(fp).exists():
                continue  # stale; let normal stale-removal handle it
            probe = await _probe_file(fp)
            if probe is None:
                continue

            raw_audio = probe.get("audio_tracks", [])
            raw_subs = probe.get("subtitle_tracks", [])

            # Re-derive native_lang from the just-probed (now-language-tagged)
            # audio tracks. Pre-v0.6.5 these were 'und' so native_language was
            # whatever the first track happened to be; after re-probe the
            # IFO/mpls metadata may upgrade it.
            native_lang = _detect_native_language(raw_audio)

            # Run the same classification pipeline as the canonical scan write
            # path in backend/routes/scan.py:_write_batch_sync_inner. Pre-fix
            # the raw probe dicts were written directly — leaner JSON missing
            # the keep/score/locked fields that downstream consumers (e.g.
            # backend/routes/jobs.py) read off of these rows.
            audio_tracks = _classify_audio_tracks(raw_audio, native_lang)
            subtitle_tracks = _classify_subtitle_tracks(raw_subs, native_lang)

            audio_json = _json.dumps([t.model_dump() for t in audio_tracks])
            subtitle_json = _json.dumps([t.model_dump() for t in subtitle_tracks])

            # Mirror the subset of derived flags from _write_batch_sync_inner
            # that depend on track classification. has_lossless_audio_flag is
            # derived from codec/profile (invariant across re-probe) so we
            # leave it alone; same for has_external_subs_flag, disc_type,
            # video_codec, etc. native_language can change because 'und' may
            # now resolve to a real ISO code.
            has_removable = 1 if any(not t.keep for t in audio_tracks) else 0
            has_removable_subs = 1 if any(not t.keep for t in subtitle_tracks) else 0

            db2 = await aiosqlite.connect(self.db_path)
            try:
                await db2.execute(
                    "UPDATE scan_results SET "
                    "audio_tracks_json = ?, subtitle_tracks_json = ?, "
                    "native_language = ?, "
                    "has_removable_tracks_flag = ?, has_removable_subs_flag = ? "
                    "WHERE file_path = ?",
                    (
                        audio_json,
                        subtitle_json,
                        native_lang,
                        has_removable,
                        has_removable_subs,
                        fp,
                    ),
                )
                await db2.commit()
            finally:
                await db2.close()
            updated += 1

        print(f"[WATCHER] v0.6.5 backfill: updated {updated} disc rows", flush=True)
        await self._set_setting(flag_key, "true")

    async def _backfill_estimated_savings_v067(self) -> None:
        """One-shot v0.6.7 migration: recompute estimated_savings_bytes
        + video_conv_savings_bytes for existing scan_results rows using
        the new CQ-calibrated curve. Idempotent via settings flag
        'savings_recomputed_v067'.

        Pre-v0.6.7 the scanner used a flat 30% reduction default for the
        video-conversion portion; existing rows still carry those stale
        numbers until they're re-scanned. We rewrite them in-place using
        the same `total_estimated_savings_bytes` helper that scan-time
        writes go through.
        """
        flag_key = "savings_recomputed_v067"
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (flag_key,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["value"] == "true":
                return  # already done

            # Global CQ — same source as scanner / watcher / estimate modal.
            async with db.execute(
                "SELECT value FROM settings WHERE key = 'nvenc_cq'"
            ) as cur:
                cqrow = await cur.fetchone()
                try:
                    global_cq = int(cqrow["value"]) if cqrow else 25
                except (TypeError, ValueError):
                    global_cq = 25

            # Only re-touch rows where the value would actually change —
            # i.e. rows that need_conversion. Skip rows already at zero
            # savings (no conversion needed); their numbers are correct.
            async with db.execute(
                "SELECT file_path, file_size, needs_conversion, audio_tracks_json, duration "
                "FROM scan_results "
                "WHERE removed_from_list = 0 AND needs_conversion = 1"
            ) as cur:
                candidates = [dict(r) for r in await cur.fetchall()]
        finally:
            await db.close()

        if not candidates:
            await self._set_setting(flag_key, "true")
            return

        print(
            f"[WATCHER] v0.6.7 backfill: recomputing savings for {len(candidates)} rows (cq={global_cq})",
            flush=True,
        )

        from backend.encoding_estimates import video_conv_savings_bytes

        # NOTE: scan_results doesn't store total `estimated_savings_bytes`
        # as a column — the frontend recomputes the audio-removal portion
        # from `audio_tracks` (with keep=False + size_estimate_bytes) at
        # render time. So all we need to backfill is the new video-only
        # CQ-derived column. The audio portion was already correct.
        updated = 0
        db2 = await aiosqlite.connect(self.db_path)
        try:
            for r in candidates:
                file_size = r["file_size"] or 0
                new_video = video_conv_savings_bytes(file_size, global_cq)
                await db2.execute(
                    "UPDATE scan_results SET video_conv_savings_bytes = ? "
                    "WHERE file_path = ?",
                    (new_video, r["file_path"]),
                )
                updated += 1
            await db2.commit()
        finally:
            await db2.close()

        print(f"[WATCHER] v0.6.7 backfill: updated {updated} rows", flush=True)
        await self._set_setting(flag_key, "true")

    async def _backfill_iso_languages_v076(self) -> None:
        """One-shot v0.7.6 migration: re-probe existing BD ISO scan_results
        rows whose audio_tracks are all-und so they pick up the v0.7.4
        libbluray ctypes language metadata. Tracked via settings flag
        'iso_lang_backfilled_v076'. Skips paths whose source has been
        deleted (stale rows are cleaned up by the normal stale-removal
        path).

        Scope-locked to BD ISOs (`disc_type='bdmv'` AND `.iso` suffix)
        with `audio_tracks_json` that is NULL, empty, or every entry
        carries `language='und'`. Partial-coverage rows (e.g. `[eng,
        und]`) are out of scope — they reflect either real und tracks
        or accepted prior state.

        v0.7.6 supersedes the broken v0.7.5 sweep. The v0.7.5 selector
        had a JSON LIKE clause `'%"language":"und"%'` (no spaces) that
        never matched real stored JSON — `json.dumps()` defaults to
        `': '` separators, so production rows always store
        `"language": "und"` with a space. v0.7.6 drops the JSON LIKE
        clause entirely and does all language-shape filtering in Python
        where the parse is correct regardless of separator style. New
        flag name re-runs the sweep on installs that already had the
        broken v0.7.5 sweep silently set its flag.
        """
        flag_key = "iso_lang_backfilled_v076"
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (flag_key,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["value"] == "true":
                return  # already done

            # Stage 1 — SQL pull. Cheap path-and-type prefilter only;
            # no JSON LIKE clause (v0.7.5 had `'%"language":"und"%'`
            # which never matched real stored JSON because json.dumps
            # default separators include a space after the colon).
            # Stage-2 Python filter below parses the JSON and applies
            # the all-und/empty rule correctly regardless of format.
            async with db.execute(
                "SELECT file_path, audio_tracks_json FROM scan_results "
                "WHERE disc_type = 'bdmv' "
                "AND lower(file_path) LIKE '%.iso'"
            ) as cur:
                sql_candidates = await cur.fetchall()
        finally:
            await db.close()

        if not sql_candidates:
            await self._set_setting(flag_key, "true")
            return

        # Stage 2 — Python filter. SQL `LIKE '%"language":"und"%'` accepts
        # rows where ANY track is und (incl. [eng, und]). Per the v0.7.5
        # scope decision, only fully-und (or empty) rows are stale.
        import json as _json
        candidates: list[str] = []
        for r in sql_candidates:
            raw_json = r["audio_tracks_json"]
            try:
                tracks = _json.loads(raw_json) if raw_json else []
            except (ValueError, TypeError):
                tracks = []  # corrupt JSON — treat as empty, definitely stale
            if not tracks or all(
                t.get("language") == "und" for t in tracks
            ):
                candidates.append(r["file_path"])

        if not candidates:
            await self._set_setting(flag_key, "true")
            return

        print(
            f"[WATCHER] v0.7.6 backfill: re-probing {len(candidates)} BD ISO rows for language metadata",
            flush=True,
        )

        from pathlib import Path as _Path
        from backend.scanner import (
            probe_file as _probe_file,
            classify_audio_tracks as _classify_audio_tracks,
            classify_subtitle_tracks as _classify_subtitle_tracks,
            detect_native_language as _detect_native_language,
        )

        updated = 0
        for fp in candidates:
            if not _Path(fp).exists():
                continue  # stale; let normal stale-removal handle it
            probe = await _probe_file(fp)
            if probe is None:
                continue

            raw_audio = probe.get("audio_tracks", [])
            raw_subs = probe.get("subtitle_tracks", [])

            native_lang = _detect_native_language(raw_audio)
            audio_tracks = _classify_audio_tracks(raw_audio, native_lang)
            subtitle_tracks = _classify_subtitle_tracks(raw_subs, native_lang)

            audio_json = _json.dumps([t.model_dump() for t in audio_tracks])
            subtitle_json = _json.dumps([t.model_dump() for t in subtitle_tracks])

            has_removable = 1 if any(not t.keep for t in audio_tracks) else 0
            has_removable_subs = 1 if any(not t.keep for t in subtitle_tracks) else 0

            db2 = await aiosqlite.connect(self.db_path)
            try:
                await db2.execute(
                    "UPDATE scan_results SET "
                    "audio_tracks_json = ?, subtitle_tracks_json = ?, "
                    "native_language = ?, "
                    "has_removable_tracks_flag = ?, has_removable_subs_flag = ? "
                    "WHERE file_path = ?",
                    (
                        audio_json,
                        subtitle_json,
                        native_lang,
                        has_removable,
                        has_removable_subs,
                        fp,
                    ),
                )
                await db2.commit()
            finally:
                await db2.close()
            updated += 1

        print(f"[WATCHER] v0.7.6 backfill: updated {updated} rows", flush=True)
        await self._set_setting(flag_key, "true")

    async def _set_setting(self, key: str, value: str) -> None:
        """Helper: upsert a row in the settings table."""
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
            await db.commit()
        finally:
            await db.close()

    async def check_once(self) -> dict:
        """Run a single check cycle. Only monitors directories that have been scanned."""
        # v0.6.5: one-shot re-probe of existing disc rows so they pick up
        # IFO/mpls language metadata. Idempotent via settings flag.
        await self._backfill_disc_languages_v065()
        # v0.6.7: one-shot recompute of video_conv_savings_bytes for
        # existing rows using the CQ-calibrated curve. Idempotent.
        await self._backfill_estimated_savings_v067()
        # v0.7.6: one-shot re-probe of existing BD ISO rows whose
        # audio_tracks are all-und (pre-v0.7.4 libbluray-ctypes path).
        # Supersedes the broken v0.7.5 sweep (selector bug). Idempotent.
        await self._backfill_iso_languages_v076()
        scanned_dirs = await self._get_scanned_dirs()
        if not scanned_dirs:
            return {"checked": 0, "new": 0, "removed": 0}

        extensions = {ext.lower() for ext in settings.video_extensions}
        extensions.add(".iso")  # v0.7.0: include ISO files for disc-image
                                # classification (separate from user-configured
                                # video extensions)
        known_paths = await self._get_known_files()

        def _walk_dirs():
            result: set[str] = set()
            for dir_path in scanned_dirs:
                dir_p = Path(dir_path)
                if not dir_p.exists():
                    continue
                for root, dirs, files in os.walk(dir_path):
                    root_path = Path(root)

                    # v0.6.1: disc-folder detection — mirror scanner walk so
                    # newly-dropped VIDEO_TS/ and BDMV/ folders auto-discover.
                    # Without this the extension filter below drops .IFO /
                    # .VOB / .m2ts before the disc-marker pre-pass can see
                    # them, and disc folders never get registered.
                    disc_type = _classify_disc(root_path)
                    if disc_type:
                        marker = _disc_marker_path(root_path, disc_type)
                        if marker.is_file():
                            result.add(str(marker))
                        dirs[:] = [d for d in dirs if d not in ("VIDEO_TS", "BDMV")]
                        continue

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
