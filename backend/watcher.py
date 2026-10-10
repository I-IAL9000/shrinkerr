"""Background file watcher — periodically checks media dirs for new, changed, or deleted files."""

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiosqlite

from backend.config import settings
from backend.database import DB_PATH
from backend.scanner import _classify_disc, _disc_marker_path, already_converted, clamp_future_mtime, folder_candidates, removable_audio_flag, SUBTITLE_EXTENSIONS, walk_media_dir

# v0.9.100: a probe failure is retried after this many seconds instead of
# blocklisting the file until the process restarts. A transient timeout / lock
# / mid-copy then self-heals on a later cycle. (Previously permanent: disc
# `concat:` probes over CIFS occasionally blew the 30s ffprobe limit and the
# disc stayed invisible until an app restart — which is how the whole library
# accumulated 0 DVDs.)
_PROBE_RETRY_COOLDOWN_S = 1800


def _file_too_recent(mtime: float, now: float, skip_age_minutes: int) -> bool:
    """True if a file was modified within the last `skip_age_minutes` and is
    thus likely still being written/copied — so the watcher should defer it.

    v0.9.101: a NEGATIVE age (future mtime) is a bogus timestamp, common on
    DVD/ISO rips (a VIDEO_TS.IFO dated years ahead). It must NOT count as
    "recent" — otherwise the disc is skipped as too-new on every cycle forever
    and never gets probed or registered."""
    if skip_age_minutes <= 0:
        return False
    age_min = (now - mtime) / 60
    return 0 <= age_min < skip_age_minutes


def _safe_int(s, default):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# v0.7.26: the per-subfolder belt now fires on an ABSOLUTE row-loss
# threshold, not a percentage. Rationale: the belt is meant to catch
# catastrophic disasters (mount loss, drive unmount) — not normal user
# actions like deleting a show, even one with hundreds of episodes.
# The pre-v0.7.26 ">50% of subfolder + ≥5 known rows" trigger fired on
# any fully-deleted folder regardless of total file count; users with
# scene-style per-folder layouts hit it on every legit delete.
#
# Switching to "≥ MIN_BELT_STALE_TRIGGER rows lost in one cycle for a
# single subfolder" means a 100-episode show deletion (100 rows) cleans
# up normally while a 20K-file mount loss is still protected.
#
# Tunable via SHRINKERR_BELT_MIN_SIZE env var (name kept for back-compat
# with v0.7.25 — semantics changed). Lower it for small libraries where
# 1000 is too high; raise it if even bulk-move false-positives bother you.
def _belt_stale_trigger() -> int:
    """Read the belt's stale-row trigger from env at call time.
    Returns the default (1000) on parse error or unset."""
    try:
        return max(1, int(os.environ.get("SHRINKERR_BELT_MIN_SIZE", "1000")))
    except ValueError:
        return 1000


async def _unreadable_iso_entry(file_path: str):
    """Scanner row for a disc image no reader could open (v0.9.153)."""
    import time
    from backend.disc_metadata import diagnose_unreadable_iso
    from backend.models import ScannedFile
    p = Path(file_path)
    try:
        st = await asyncio.to_thread(p.stat)
        size, mtime = st.st_size, clamp_future_mtime(st.st_mtime, time.time())
    except OSError:
        size, mtime = 0, None
    reason = await asyncio.to_thread(diagnose_unreadable_iso, p)
    print(f"[WATCHER] Unreadable disc image: {file_path} — {reason}", flush=True)
    return ScannedFile(
        file_path=file_path, file_name=p.name, folder_name=p.parent.name,
        file_size=size, file_size_gb=round(size / (1024 ** 3), 3),
        video_codec="unknown", needs_conversion=False, audio_tracks=[],
        native_language="und", has_removable_tracks=False,
        estimated_savings_bytes=0, estimated_savings_gb=0,
        file_mtime=mtime, duration=0,
        probe_status="unreadable", probe_error=reason,
    )


class FileWatcher:
    def __init__(self, db_path: str, interval_minutes: int = 5):
        self.db_path = db_path
        self.interval = interval_minutes * 60
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.new_files_count = 0  # Tracks unseen new files since last scanner page visit
        # path → monotonic time of last ffprobe failure. Retried after
        # _PROBE_RETRY_COOLDOWN_S (was a permanent set that hid a title until
        # the process restarted). See _active_probe_failures().
        self._probe_failures: dict[str, float] = {}
        self._last_disk_alert: float = 0  # Cooldown for disk space alerts
        # Last (ignored, probe_failures, to_process) tuple we logged for the
        # "Pre-filtered" line. Used to deduplicate identical states cycle to
        # cycle so a stable backlog doesn't spam the log every 5 minutes.
        self._last_pre_filtered_log: Optional[tuple[int, int, int]] = None

    def _active_probe_failures(self) -> set[str]:
        """Paths whose last probe failure is still within the retry cooldown.
        Older entries fall out so a transient timeout / lock / mid-copy is
        retried on a later cycle instead of being blocklisted until restart."""
        import time
        now = time.monotonic()
        return {p for p, t in self._probe_failures.items()
                if now - t < _PROBE_RETRY_COOLDOWN_S}

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

    async def _carry_over_moves(self, stale_paths: set[str], new_paths: set[str]) -> dict[str, str]:
        """Rows to move rather than delete and re-add: a file renamed or
        moved (by Sonarr/Radarr, by hand) is a path gone and a path new in
        the same cycle. Paired when exactly one of each has the same size and
        extension; the row keeps its manual match, track edits, health
        result, converted flag and ignore instead of coming back "New"
        (SC-20, v0.10.0). Returns {old: new}."""
        if not stale_paths or not new_paths:
            return {}
        db = await aiosqlite.connect(self.db_path)
        try:
            stale_sizes: dict[str, int] = {}
            stale = sorted(stale_paths)
            for i in range(0, len(stale), 900):
                chunk = stale[i:i + 900]
                async with db.execute(
                    f"SELECT file_path, file_size FROM scan_results WHERE file_path IN ({','.join('?' * len(chunk))}) "
                    "AND file_path NOT IN (SELECT file_path FROM jobs WHERE status IN ('pending', 'running'))",
                    chunk,
                ) as cur:
                    stale_sizes.update({r[0]: r[1] for r in await cur.fetchall() if r[1]})
        finally:
            await db.close()

        def _sizes() -> dict[str, int]:
            out = {}
            for path in new_paths:
                try:
                    out[path] = os.path.getsize(path)
                except OSError:
                    pass
            return out

        new_sizes = await asyncio.to_thread(_sizes)
        # Only real media sizes: tiny files (disc markers, samples) collide.
        def _key(path: str, size: int):
            return (size, Path(path).suffix.lower()) if size >= 10 * 1024 * 1024 else None

        from collections import defaultdict as _dd
        gone, appeared = _dd(list), _dd(list)
        for path, size in stale_sizes.items():
            if _key(path, size):
                gone[_key(path, size)].append(path)
        for path, size in new_sizes.items():
            if _key(path, size):
                appeared[_key(path, size)].append(path)
        moves = {gone[k][0]: appeared[k][0] for k in gone if len(gone[k]) == 1 and len(appeared.get(k, [])) == 1}
        if not moves:
            return {}

        db = await aiosqlite.connect(self.db_path)
        try:
            for old, new in moves.items():
                await db.execute("UPDATE OR IGNORE scan_results SET file_path = ? WHERE file_path = ?", (new, old))
                await db.execute("UPDATE OR IGNORE ignored_files SET file_path = ? WHERE file_path = ?", (new, old))
                await db.execute("UPDATE jobs SET file_path = ? WHERE file_path = ? AND status = 'pending'", (new, old))
            await db.commit()
        finally:
            await db.close()
        for old, new in moves.items():
            print(f"[WATCHER] Moved: {old} -> {new}", flush=True)
        return moves

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

        from backend.scanner import probe_file, is_x264, is_x265, is_av1

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

        source_codecs, global_cq = await scan_settings(self.db_path)

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

        def _map_discs(new_files: list[str]) -> list[str]:
            # Blocking (is_dir / disc classification on the NAS): run in a
            # thread (SC-26, v0.10.0).
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

            return new_files_disc_adjusted

        new_files = await asyncio.to_thread(_map_discs, new_files)

        results = []
        new_file_paths = []
        skipped_ignored = 0
        skipped_probe = 0
        skipped_av1 = 0
        skipped_age = 0
        _active_failures = self._active_probe_failures()
        for file_path in new_files:
            if file_path in ignored_paths:
                skipped_ignored += 1
                continue

            if file_path in _active_failures:
                skipped_probe += 1
                continue

            # Skip recently modified files (still being written/copied)
            if skip_age_minutes > 0:
                try:
                    if _file_too_recent(await asyncio.to_thread(os.path.getmtime, file_path),
                                        _time.time(), skip_age_minutes):
                        skipped_age += 1
                        continue
                except OSError:
                    pass

            probe = await probe_file(file_path)
            if probe is None and file_path.lower().endswith(".iso"):
                # v0.9.153: an unreadable disc image used to be skipped here
                # and never appeared anywhere. Record it as "unreadable" with
                # the reader's error so it shows up in the Scanner.
                results.append(await _unreadable_iso_entry(file_path))
                new_file_paths.append(file_path)
                continue
            if probe is None:
                # v0.6.2: disc probes can fail silently. Surface them.
                if "/VIDEO_TS/VIDEO_TS.IFO" in file_path or "/BDMV/index.bdmv" in file_path.lower():
                    print(f"[WATCHER] !!! Disc probe FAILED: {file_path}", flush=True)
                self._probe_failures[file_path] = _time.monotonic()
                skipped_probe += 1
                continue

            if is_av1(probe["video_codec"]):
                skipped_av1 += 1
                continue
            results.append(await scanned_from_probe(file_path, probe, source_codecs, global_cq))
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

        v0.10.0: the jobs Add to Queue would create (routes/jobs.py
        queue_new_files): rules, the conversion filters, audio AND subtitle
        removals, moving the original-language audio first, an "ignore" rule
        still doing the cleanup — it decided on its own before, and froze the
        global settings into each job. Files with nothing to do are left out.
        Priority: the highest of settings.auto_queue_priority and the rule's.
        settings.auto_queue_view limits it to the files a saved view lists.
        """
        db = await aiosqlite.connect(self.db_path)
        try:
            async with db.execute(
                "SELECT key, value FROM settings WHERE key IN ('auto_queue_new', 'auto_queue_priority', 'auto_queue_view')"
            ) as cur:
                settings = {r[0]: r[1] for r in await cur.fetchall()}
        finally:
            await db.close()
        if (settings.get("auto_queue_new") or "").lower() != "true":
            return 0
        priority = max(0, min(2, _safe_int(settings.get("auto_queue_priority", "0") or 0, 0)))
        paths = [s.file_path for s in results]
        if settings.get("auto_queue_view"):  # only files in that saved view (v0.10.0)
            from backend.routes.views import paths_in_view
            listed = await paths_in_view(settings["auto_queue_view"], paths)
            paths = [p for p in paths if p in listed]

        from backend.queue import JobQueue
        from backend.routes.jobs import queue_new_files
        queued, by_rule = await queue_new_files(paths, priority, JobQueue(self.db_path))
        if queued or by_rule:
            msg = f"[WATCHER] Auto-queued {queued} new files"
            if by_rule:
                msg += f" ({by_rule} skipped or kept from converting by a rule)"
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
            from backend.database import prefix_clause
            for d in media_dirs:
                under_sql, under_params = prefix_clause([d.rstrip("/") + "/"])
                async with db.execute(
                    f"SELECT 1 FROM scan_results WHERE {under_sql} LIMIT 1", under_params,
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
            if not await asyncio.to_thread(_Path(fp).exists):
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
            has_removable = removable_audio_flag(audio_tracks, native_lang)
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
            if not await asyncio.to_thread(_Path(fp).exists):
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

            has_removable = removable_audio_flag(audio_tracks, native_lang)
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

    async def _backfill_disc_languages_v078(self) -> None:
        """One-shot v0.7.8 migration: re-probe existing FOLDER disc
        scan_results rows whose audio_tracks are all-und or empty so
        they pick up the v0.6.5 IFO/mpls language metadata. The original
        v0.6.5 sweep used a SQL `LIKE '%"language":"und"%'` clause that
        never matched real stored JSON (json.dumps default separators
        include a space — same bug v0.7.5 had for BD ISOs, fixed in
        v0.7.6). On real installs the broken selector pulled zero rows
        and the flag was set to true, so v0.6.5 effectively never ran.

        v0.7.8 mirrors v0.7.6's structure for the folder-disc case: SQL
        prefilter on disc_type + path-suffix only, Python all-und/empty
        filter, then probe + classify + UPDATE per row. The UPDATE also
        clears stale corrupt markers via `_clear_stale_disc_health_status`
        (extended in v0.7.8 to clear `probe_status` + `health_errors_json`
        alongside `health_status`).

        Tracked via settings flag 'disc_lang_backfilled_v078'. Scope-locked
        to FOLDER discs (`disc_type IS NOT NULL AND lower(file_path) NOT
        LIKE '%.iso'`) — BD ISOs are already handled by v0.7.6's
        `_backfill_iso_languages_v076`.
        """
        flag_key = "disc_lang_backfilled_v078"
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (flag_key,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["value"] == "true":
                return  # already done

            # Stage 1 — SQL pull. Path-and-type prefilter only; the JSON
            # shape check goes through Python (see v0.7.6 commit for why).
            async with db.execute(
                "SELECT file_path, audio_tracks_json FROM scan_results "
                "WHERE disc_type IS NOT NULL "
                "AND lower(file_path) NOT LIKE '%.iso'"
            ) as cur:
                sql_candidates = await cur.fetchall()
        finally:
            await db.close()

        if not sql_candidates:
            await self._set_setting(flag_key, "true")
            return

        # Stage 2 — Python filter. Same all-und/empty semantics as v0.7.6.
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
            f"[WATCHER] v0.7.8 backfill: re-probing {len(candidates)} folder disc rows for language metadata",
            flush=True,
        )

        from pathlib import Path as _Path
        from backend.scanner import (
            probe_file as _probe_file,
            classify_audio_tracks as _classify_audio_tracks,
            classify_subtitle_tracks as _classify_subtitle_tracks,
            detect_native_language as _detect_native_language,
            _clear_stale_disc_health_status as _clear_stale,
        )

        updated = 0
        for fp in candidates:
            if not await asyncio.to_thread(_Path(fp).exists):
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

            has_removable = removable_audio_flag(audio_tracks, native_lang)
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
            # The probe succeeded — clear any stale corrupt markers
            # (health_status / probe_status / health_errors_json) on
            # this row. v0.7.8's extension of the helper handles the
            # full corrupt-flag set in one statement.
            await _clear_stale(self.db_path, fp)
            updated += 1

        print(f"[WATCHER] v0.7.8 backfill: updated {updated} rows", flush=True)
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

    async def _reconcile_external_subs(self, sub_folder_files: dict, known_paths: set) -> int:
        """Update already-known videos whose sidecar subtitles changed on disk.

        Adding an external sub next to an existing video used to require a manual
        folder rescan (the sub isn't a new video, and the video is already
        known). Scoped to folders that actually contain sidecar subs, and writes
        only when a video's external-sub set actually changed. Restart-safe:
        reconciles disk against each row's stored subs, so subs added while the
        app was down are picked up on the next cycle too. v0.9.107.
        """
        import json as _json
        if not sub_folder_files:
            return 0
        from backend.scanner import detect_external_subtitles, merge_external_subs

        # Known videos that live in a sub-containing folder.
        sub_folders = set(sub_folder_files.keys())
        by_folder: dict[str, list[str]] = {}
        for vp in known_paths:
            folder = os.path.dirname(vp)
            if folder in sub_folders:
                by_folder.setdefault(folder, []).append(vp)
        if not by_folder:
            return 0

        all_videos = [v for vs in by_folder.values() for v in vs]

        # Phase 1 — READ stored subs (WAL readers don't block writers); close
        # the connection before computing.
        stored: dict[str, tuple] = {}
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            for i in range(0, len(all_videos), 500):
                chunk = all_videos[i:i + 500]
                ph = ",".join("?" for _ in chunk)
                async with db.execute(
                    f"SELECT file_path, native_language, subtitle_tracks_json "
                    f"FROM scan_results WHERE file_path IN ({ph})",
                    chunk,
                ) as cur:
                    for r in await cur.fetchall():
                        stored[r["file_path"]] = (r["native_language"], r["subtitle_tracks_json"])
        finally:
            await db.close()

        # Phase 2 — COMPUTE changes in memory, holding NO DB connection.
        # v0.9.113: the previous version held a write transaction open across
        # this whole pass (UPDATE in the loop, commit only at the end) which
        # blocked running conversions' progress writes → "database is locked".
        # v0.10.0 (SC-26): pure CPU — about 1.8 s per cycle for 30k subbed
        # videos — so it runs in a thread instead of on the event loop.
        def _compute() -> list[tuple]:
            changes: list[tuple] = []  # (file_path, subtitle_tracks_json, has_ext, has_rem)
            for folder, videos in by_folder.items():
                sibs = sub_folder_files.get(folder) or []
                for vp in videos:
                    if vp not in stored:
                        continue
                    native, subs_json = stored[vp]
                    try:
                        cur_subs = _json.loads(subs_json or "[]")
                    except (ValueError, TypeError):
                        continue
                    try:
                        cur_ext = detect_external_subtitles(vp, siblings=sibs)
                    except Exception:
                        continue
                    changed, new_subs, has_ext, has_rem = merge_external_subs(
                        cur_subs, native or "und", cur_ext)
                    if changed:
                        changes.append((vp, _json.dumps(new_subs),
                                        1 if has_ext else 0, 1 if has_rem else 0))

            return changes

        changes = await asyncio.to_thread(_compute)

        if not changes:
            return 0

        # Phase 3 — WRITE the (few) changes in a SHORT transaction with a
        # busy_timeout, then commit promptly, so the write lock isn't held long
        # enough to fail a concurrent conversion.
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA busy_timeout=60000")
            for vp, sj, he, hr in changes:
                await db.execute(
                    "UPDATE scan_results SET subtitle_tracks_json = ?, "
                    "has_external_subs_flag = ?, has_removable_subs_flag = ? "
                    "WHERE file_path = ?", (sj, he, hr, vp))
            await db.commit()
        finally:
            await db.close()

        # Activity-feed events AFTER the write commits (own connections).
        try:
            from backend.file_events import log_event, EVENT_RESCANNED
            for vp, *_ in changes:
                await log_event(vp, EVENT_RESCANNED, "External subtitles updated (auto-detected)",
                                summary_key="externalSubsUpdated")
        except Exception:
            pass
        print(f"[WATCHER] External subs updated for {len(changes)} title(s)", flush=True)
        return len(changes)

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
        # v0.7.8: same fix applied to folder-disc rows — v0.6.5's
        # original sweep had the same JSON-LIKE selector bug and
        # silently matched zero rows on real installs. Also clears
        # stale corrupt markers (probe_status / health_errors_json)
        # alongside the language metadata. Idempotent.
        await self._backfill_disc_languages_v078()
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
            # v0.9.107: folders holding sidecar subtitle files, mapped to their
            # full file list (Paths). Lets _reconcile_external_subs pick up subs
            # added next to an already-known video with no extra directory read.
            sub_folder_files: dict[str, list] = {}
            for dir_path in scanned_dirs:
                dir_p = Path(dir_path)
                if not dir_p.exists():
                    continue
                for root, dirs, files in walk_media_dir(dir_path, unreadable_dirs):
                    root_path = Path(root)
                    # The Scanner's own rules (SC-11): disc markers, and
                    # video files next to a disc too.
                    result.update(str(p) for p in folder_candidates(root_path, dirs, files, extensions))
                    visible = [n for n in files if not n.startswith(".")
                               and ".converting." not in n and ".remuxing." not in n]
                    if any(Path(n).suffix.lower() in SUBTITLE_EXTENSIONS for n in visible):
                        sub_folder_files[root] = [root_path / n for n in visible]
            # Skip what a full scan skips — sources whose converted version
            # is already there — or the watcher re-adds and re-queues them.
            result -= already_converted(result, log=False)
            return result, sub_folder_files

        unreadable_dirs: list[str] = []
        disk_files, sub_folder_files = await asyncio.get_event_loop().run_in_executor(None, _walk_dirs)
        if unreadable_dirs:
            print(f"[WATCHER] Couldn't list {len(unreadable_dirs)} folder(s) this cycle; "
                  f"keeping their rows: {sorted(unreadable_dirs)[:10]}", flush=True)

        new_files_all = disk_files - known_paths

        # v0.7.7: scope stale-row removal to media_dirs that actually exist
        # on disk this cycle. Previously `stale_path_set = known_paths -
        # disk_files` was computed across ALL configured media_dirs as one
        # set — so if a volume mount was missing (e.g. user temporarily
        # docker-composed with a subset of volumes for RC testing), every
        # row under the unmounted dirs was flagged stale and hard-DELETEd
        # by `_remove_stale_entries`. Once gone, recovery required a full
        # rescan. The scanner's full-rescan orphan cleanup is already
        # scoped to `completed_paths`; this brings the watcher into line.
        walked_dirs = await asyncio.to_thread(lambda: [d for d in scanned_dirs if Path(d).exists()])
        missing_dirs = [d for d in scanned_dirs if d not in walked_dirs]
        if missing_dirs:
            print(
                f"[WATCHER] {len(missing_dirs)} configured media_dir(s) missing "
                f"from disk this cycle (volume not mounted?); preserving rows "
                f"under them: {sorted(missing_dirs)}",
                flush=True,
            )

        def _is_under_walked(p: str) -> bool:
            return any(
                p == d or p.startswith(d.rstrip("/") + "/") for d in walked_dirs
            )

        def _is_under_unreadable(p: str) -> bool:
            return any(p.startswith(d.rstrip("/") + "/") for d in unreadable_dirs)

        raw_stale = known_paths - disk_files
        stale_path_set = {p for p in raw_stale if _is_under_walked(p) and not _is_under_unreadable(p)}
        # Paired with new paths below before the safety belts: a moved file
        # still exists, so moving its row is safe whatever else vanished.
        possibly_moved = set(stale_path_set)

        # Sanity belt #1 — global: if a single cycle would flag more than
        # half of the walked-dir rows as stale, something is wrong
        # (network mount hiccup, unreadable dir, filesystem permission
        # issue, …). Abort stale-removal for this cycle and log loudly.
        walked_known = {p for p in known_paths if _is_under_walked(p)}
        if walked_known and len(stale_path_set) > len(walked_known) // 2:
            print(
                f"[WATCHER] aborting stale-row removal: would delete "
                f"{len(stale_path_set)}/{len(walked_known)} rows under walked "
                f"dirs (>50%). Likely a filesystem hiccup; check mounts.",
                flush=True,
            )
            stale_path_set = set()

        # Sanity belt #2 (v0.7.22) — per-subfolder: if any IMMEDIATE
        # subdirectory of a walked media_dir would lose >50% of its
        # known rows in one cycle, preserve them. Catches the
        # partial-mount race the global belt misses: when a Synology
        # mount comes up but a nested mount under it (e.g. .../TV1) is
        # still pending, the watcher walks the parent, finds the
        # *other* subfolders, and would flag every TV1 row stale —
        # which is only ~10-30 % of total library files, so the global
        # belt doesn't trip. v0.7.7 had this gap; v0.7.22 closes it.
        def _first_subfolder(p: str) -> Optional[str]:
            """Return `<media_dir>/<top-subdir>` of `p`, or None if `p` is
            at a media_dir's root level. Used to group rows for the
            per-subfolder ratio check."""
            for d in walked_dirs:
                d_norm = d.rstrip("/")
                if p == d_norm or not p.startswith(d_norm + "/"):
                    continue
                rest = p[len(d_norm) + 1:]
                first_seg = rest.split("/", 1)[0]
                if first_seg:
                    return f"{d_norm}/{first_seg}"
            return None

        if stale_path_set:
            from collections import defaultdict as _dd
            known_by_sub: dict[str, int] = _dd(int)
            stale_by_sub: dict[str, int] = _dd(int)
            for p in walked_known:
                sub = _first_subfolder(p)
                if sub:
                    known_by_sub[sub] += 1
            for p in stale_path_set:
                sub = _first_subfolder(p)
                if sub:
                    stale_by_sub[sub] += 1

            preserved_subs: set[str] = set()
            belt_trigger = _belt_stale_trigger()
            for sub, stale_n in stale_by_sub.items():
                known_n = known_by_sub.get(sub, 0)
                # v0.7.26: belt fires only on absolute row-loss volume.
                # User actions (even big multi-show deletes) typically
                # affect <1000 rows; mount-loss disasters affect 1000+
                # in a single subfolder. Normal deletes flow through to
                # cleanup unchanged.
                if stale_n >= belt_trigger:
                    preserved_subs.add(sub)
                    print(
                        f"[WATCHER] subfolder {sub!r} would lose "
                        f"{stale_n}/{known_n} rows this cycle "
                        f"(>= {belt_trigger} disaster-trigger); "
                        f"preserving (likely partial mount / unmounted "
                        f"subvolume). Trigger a manual rescan once the "
                        f"mount is back if anything's stale-for-real.",
                        flush=True,
                    )
            if preserved_subs:
                stale_path_set = {
                    p for p in stale_path_set
                    if _first_subfolder(p) not in preserved_subs
                }

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

        _active_failures = self._active_probe_failures()
        exclude = ignored_paths | _active_failures
        new_files = [f for f in new_files_all if f not in exclude]
        skipped_ignored_total = len([f for f in new_files_all if f in ignored_paths])
        skipped_probe_total = len([f for f in new_files_all if f in _active_failures])
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

        moved = await self._carry_over_moves(possibly_moved, set(new_files_all))
        if moved:
            stale_path_set = stale_path_set - set(moved)
            new_files = [f for f in new_files if f not in set(moved.values())]
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

        # v0.9.107: auto-detect sidecar subs added next to already-known videos
        # (no manual folder rescan needed). Scoped to folders that gained subs —
        # and (SC-17, v0.10.0) to walked folders whose rows still list sidecars
        # that are gone: they never came back, so the tracks stayed forever.
        try:
            db_ext = await aiosqlite.connect(self.db_path)
            try:
                async with db_ext.execute(
                    "SELECT file_path FROM scan_results WHERE has_external_subs_flag = 1"
                ) as cur:
                    for (vp,) in await cur.fetchall():
                        if vp in disk_files:
                            sub_folder_files.setdefault(os.path.dirname(vp), [])
            finally:
                await db_ext.close()
            sub_updated = await self._reconcile_external_subs(sub_folder_files, known_paths)
            if sub_updated:
                try:
                    from backend.websocket import ws_manager
                    await ws_manager.send_scan_results_changed(added=0, removed=0)
                except Exception:
                    pass
        except Exception as exc:
            print(f"[WATCHER] External-sub reconcile skipped: {exc}", flush=True)

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
                    usage = await asyncio.to_thread(shutil.disk_usage, d)
                    # Avoid duplicate alerts for same mount point
                    mount_key = f"{usage.total}"
                    if mount_key in checked:
                        continue
                    checked.add(mount_key)
                    if usage.free < threshold_bytes:
                        from backend.notifications import notify_disk_low
                        free_gb = usage.free / (1024 ** 3)
                        await notify_disk_low(d, free_gb, threshold_gb, usage.total / (1024**4))
                        self._last_disk_alert = time.monotonic()
                        break  # One alert is enough
                except OSError:
                    pass
        except Exception as exc:
            print(f"[WATCHER] Disk space check failed: {exc}", flush=True)

    async def _run_loop(self) -> None:
        await asyncio.sleep(30)
        while self._running:
            # Skip cycle if a scan is running — avoid competing for ffprobe/DB I/O.
            # v0.7.32: scan_is_actively_running() self-heals a hung scan
            # (subprocess blocked on a dead mount) by reaping it when its
            # progress file goes stale, instead of skipping forever.
            from backend.routes.scan import scan_is_actively_running
            if scan_is_actively_running():
                print("[WATCHER] Scan in progress, skipping cycle", flush=True)
            else:
                try:
                    await self.check_once()
                except Exception as exc:
                    print(f"[WATCHER] Error during check: {exc}", flush=True)
            await asyncio.sleep(self.interval)


async def scan_settings(db_path: str) -> tuple[list, int]:
    """(source codecs, the default encoder's quality on NVENC's CQ scale) for
    scanned_from_probe()."""
    import aiosqlite as _aiosqlite
    # v0.5.22: load source_codecs once per poll so codec matching
    # matches what the scanner + webhook do. Pre-v0.5.22 the watcher
    # hardcoded `is_x264(video_codec)` — only H.264 was recognised as
    # "needs conversion", so MPEG-2 / MPEG-4 / VC-1 / WMV files
    # auto-discovered via filesystem watching never got a `convert`
    # job even though they were in the user's source_codecs list.
    # HEVC was unaffected (not in default source_codecs either way).
    from backend.scanner import DEFAULT_SOURCE_CODECS
    source_codecs = list(DEFAULT_SOURCE_CODECS)
    # v0.6.7: load global NVENC CQ once per cycle to match the scanner
    # / queue-estimate's CQ-calibrated savings curve. Pre-v0.6.7 this
    # path used a flat 0.30 default that disagreed with the modal.
    global_cq = 25
    try:
        import json as _json
        db3 = await _aiosqlite.connect(db_path)
        try:
            async with db3.execute(
                "SELECT value FROM settings WHERE key = 'source_codecs'"
            ) as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    source_codecs = _json.loads(row[0])
            # v0.10.0: the default encoder's quality, not always NVENC's.
            from backend.encoding_estimates import load_effective_cq
            global_cq = await load_effective_cq(db3)
        finally:
            await db3.close()
    except Exception:
        pass
    return source_codecs, global_cq


async def scanned_from_probe(file_path: str, probe: dict, source_codecs: list, global_cq: int):
    """A new file's Scanner row from its probe, as the watcher stores it: the
    original language (TMDB / TVDB, else from the tracks), the tracks sorted
    by the keep settings, external subtitles, and the size estimates. Also
    used by add-by-path and the queue webhook (v0.10.0), which queue from the
    stored row like Add to Queue."""
    from backend.scanner import detect_native_language, codec_matches_source, video_facts
    from backend.scanner import classify_audio_tracks, classify_subtitle_tracks, estimate_savings
    from backend.encoding_estimates import video_conv_savings_bytes
    from backend.models import ScannedFile
    import time as _time

    video_codec = probe["video_codec"]
    raw_tracks = probe["audio_tracks"]
    duration = probe["duration"]
    file_size = probe["file_size"]

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
    # v0.9.122: a disc image always needs conversion regardless of codec
    # (mirror the scanner) so a newly-discovered HEVC disc auto-queues as
    # a convert, not a no-op audio cleanup.
    if probe.get("disc_type"):
        needs_conversion = True
    from backend.scanner import is_dolby_vision
    if is_dolby_vision(probe.get("hdr_format")):
        needs_conversion = False  # v0.10.0: never re-encoded
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
    link_count = None  # v0.10.0: hardlinks (a disc folder: not counted)
    try:
        if disc_type_val:
            file_mtime = (await asyncio.to_thread(p.parent.parent.stat)).st_mtime
        else:
            st = await asyncio.to_thread(os.stat, file_path)
            file_mtime, link_count = st.st_mtime, st.st_nlink
    except OSError:
        file_mtime = None
    # v0.9.102: clamp a bogus future mtime (ripped media dated 2036)
    # so it doesn't pin the title to the top of the "Newest" sort.
    file_mtime = clamp_future_mtime(file_mtime, _time.time())

    return ScannedFile(
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
        # The watcher never set the height (SC-14): its rows read as
        # SD to the 4K filter and rules until a full scan.
        video_height=probe.get("video_height", 0),
        video_width=probe.get("video_width", 0),  # v0.10.0
        hdr_format=probe.get("hdr_format"),  # v0.10.0
        **video_facts(probe),  # v0.10.0
        link_count=link_count,
    )
