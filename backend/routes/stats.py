import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Response

from backend.database import connect_db

router = APIRouter(prefix="/api/stats")


def _source_type(name: str) -> str:
    n = name.lower()
    if re.search(r"blu[\-\s]?ray|bdremux|bdrip", n):
        return "Blu-ray"
    if "web-dl" in n or "webdl" in n:
        return "WEB-DL"
    if "webrip" in n:
        return "WEBRip"
    if "hdtv" in n:
        return "HDTV"
    if "dvdrip" in n or "dvd" in n:
        return "DVD"
    if "remux" in n:
        return "Remux"
    return "Other"


def _resolution(name: str) -> str:
    n = name.lower()
    if any(x in n for x in ("2160p", "4k", "uhd")):
        return "4K"
    if "1080p" in n or "1080i" in n:
        return "1080p"
    if "720p" in n:
        return "720p"
    if any(x in n for x in ("480p", "576p", "sd")):
        return "SD"
    return "Unknown"


def _codec_label(codec: str) -> str:
    c = (codec or "unknown").lower()
    if "264" in c or "avc" in c:
        return "H.264"
    if "265" in c or "hevc" in c:
        return "H.265"
    if "av1" in c:
        return "AV1"
    return c.upper()


def _parse_json(val) -> list:
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except Exception:
        return []


@router.get("/summary")
async def get_stats_summary():
    db = await connect_db()
    try:
        # --- Completed jobs ---
        rows = await db.execute_fetchall(
            "SELECT file_path, job_type, status, space_saved, original_size, "
            "audio_tracks_to_remove, started_at, completed_at "
            "FROM jobs WHERE status = 'completed'"
        )
        completed = [dict(r) for r in rows]

        # --- Pending/failed counts ---
        row = await db.execute_fetchall(
            "SELECT status, COUNT(*) as cnt FROM jobs WHERE status IN ('pending','failed') GROUP BY status"
        )
        status_counts = {r["status"]: r["cnt"] for r in row}
        pending_count = status_counts.get("pending", 0)
        failed_count = status_counts.get("failed", 0)

        # --- Scan results (only needed columns) ---
        scan_rows = await db.execute_fetchall(
            "SELECT file_path, file_size, video_codec, needs_conversion, "
            "audio_tracks_json, native_language "
            "FROM scan_results WHERE removed_from_list = 0 "
            "AND file_path NOT LIKE '%.converting.%' "
            "AND file_path NOT LIKE '%.remuxing.%'"
        )
        scan_data = [dict(r) for r in scan_rows]
    finally:
        await db.close()

    # ---- Compute all stats in Python ----

    total_saved = 0
    tracked_saved = 0
    tracked_original = 0
    audio_tracks_deleted = 0
    files_with_savings = 0
    files_no_savings = 0
    source_types = defaultdict(int)
    resolutions = defaultdict(int)
    savings_by_source: dict[str, dict] = {}
    size_ranges = [0, 0, 0, 0, 0]  # 0-2, 2-5, 5-10, 10-20, 20+
    size_thresholds = [2 * 1024**3, 5 * 1024**3, 10 * 1024**3, 20 * 1024**3]
    top_savers: list[dict] = []
    saved_by_folder: dict[str, int] = defaultdict(int)
    total_time_minutes = 0.0
    jobs_with_time = 0
    files_audio_cleaned = 0

    # Load configured media directories for library-level grouping
    _media_dirs = []
    try:
        async with db.execute("SELECT path FROM media_dirs ORDER BY LENGTH(path) DESC") as cur:
            _media_dirs = [r["path"].rstrip("/") for r in await cur.fetchall()]
    except Exception:
        pass

    def _get_library_name(file_path: str) -> str:
        """Map a file path to its configured media directory label."""
        for d in _media_dirs:
            if file_path.startswith(d + "/") or file_path.startswith(d):
                # Return the last component of the media dir path as the label
                return d.rstrip("/").split("/")[-1]
        # Fallback: use 3rd path component (e.g. /media/TV1/... -> TV1)
        parts = file_path.split("/")
        return parts[2] if len(parts) >= 3 else "Unknown"

    for j in completed:
        saved = max(0, j["space_saved"] or 0)
        total_saved += saved
        orig = j["original_size"] or 0
        fname = j["file_path"].rsplit("/", 1)[-1]

        if saved > 0:
            files_with_savings += 1
            top_savers.append({"file_name": fname, "space_saved": saved})
            folder = _get_library_name(j["file_path"])
            saved_by_folder[folder] += saved
        else:
            files_no_savings += 1

        if orig > 0:
            tracked_saved += saved
            tracked_original += orig

        # Audio tracks deleted
        tracks = _parse_json(j["audio_tracks_to_remove"])
        audio_tracks_deleted += len(tracks)

        # Audio cleaned flag
        if j["job_type"] in ("audio", "combined") and len(tracks) > 0:
            files_audio_cleaned += 1

        # Source type / resolution
        src = _source_type(fname)
        source_types[src] += 1
        resolutions[_resolution(fname)] += 1

        # Savings by source
        if orig > 0:
            if src not in savings_by_source:
                savings_by_source[src] = {"saved": 0, "original": 0, "count": 0}
            savings_by_source[src]["saved"] += saved
            savings_by_source[src]["original"] += orig
            savings_by_source[src]["count"] += 1

        # Size distribution
        if orig > 0:
            if orig < size_thresholds[0]:
                size_ranges[0] += 1
            elif orig < size_thresholds[1]:
                size_ranges[1] += 1
            elif orig < size_thresholds[2]:
                size_ranges[2] += 1
            elif orig < size_thresholds[3]:
                size_ranges[3] += 1
            else:
                size_ranges[4] += 1

        # Avg time
        if j["started_at"] and j["completed_at"]:
            from datetime import datetime
            try:
                t0 = datetime.fromisoformat(j["started_at"])
                t1 = datetime.fromisoformat(j["completed_at"])
                total_time_minutes += (t1 - t0).total_seconds() / 60
                jobs_with_time += 1
            except Exception:
                pass

    total_completed = len(completed)
    percent_saved = (tracked_saved / tracked_original * 100) if tracked_original > 0 else 0
    avg_per_file = total_saved / total_completed if total_completed > 0 else 0
    avg_time = total_time_minutes / jobs_with_time if jobs_with_time > 0 else 0
    est_remaining_hours = (pending_count * avg_time / 60) if avg_time > 0 and pending_count > 0 else 0

    # Top 10 savers
    top_savers.sort(key=lambda x: x["space_saved"], reverse=True)
    top_savers = top_savers[:10]

    # Top folders
    top_folders = sorted(saved_by_folder.items(), key=lambda x: x[1], reverse=True)[:8]

    # --- Scan-based stats ---
    codecs = defaultdict(int)
    needs_conversion_count = 0
    native_langs = defaultdict(int)
    total_audio_tracks = 0
    audio_langs = defaultdict(int)
    tracks_marked_removal = 0
    removed_langs = defaultdict(int)
    files_needing_audio_cleanup = 0

    for s in scan_data:
        codecs[_codec_label(s["video_codec"])] += 1
        if s["needs_conversion"]:
            needs_conversion_count += 1
        native_langs[(s["native_language"] or "und").upper()] += 1

        tracks = _parse_json(s["audio_tracks_json"])
        total_audio_tracks += len(tracks)
        has_removable = False
        for t in tracks:
            lang = (t.get("language") or "und").upper()
            audio_langs[lang] += 1
            if not t.get("keep", True):
                tracks_marked_removal += 1
                removed_langs[lang] += 1
                has_removable = True
        if has_removable:
            files_needing_audio_cleanup += 1

    already_converted = sum(1 for j in completed if j["job_type"] in ("convert", "combined"))

    # VMAF quality scores. Canonical 3-tier table (v0.3.32+) — see
    # frontend/src/utils/vmaf.ts. The previous "fair" bucket (80–87) was
    # folded into "poor"; old API consumers will see fair=0 if they
    # still ask for it.
    vmaf_stats = {"avg": 0, "count": 0, "excellent": 0, "good": 0, "poor": 0}
    try:
        async with db.execute(
            "SELECT vmaf_score FROM jobs WHERE status='completed' AND vmaf_score IS NOT NULL AND vmaf_score > 0"
        ) as cur:
            vmaf_rows = await cur.fetchall()
        if vmaf_rows:
            scores = [r["vmaf_score"] for r in vmaf_rows]
            vmaf_stats["count"] = len(scores)
            vmaf_stats["avg"] = round(sum(scores) / len(scores), 1)
            vmaf_stats["excellent"] = sum(1 for s in scores if s >= 93)
            vmaf_stats["good"] = sum(1 for s in scores if 87 <= s < 93)
            vmaf_stats["poor"] = sum(1 for s in scores if s < 87)
    except Exception:
        pass

    return {
        # Overview cards
        "total_saved": total_saved,
        "percent_saved": round(percent_saved, 1),
        "avg_per_file": round(avg_per_file),
        "files_processed": total_completed,
        "audio_tracks_deleted": audio_tracks_deleted,
        # Processing results
        "files_with_savings": files_with_savings,
        "files_no_savings": files_no_savings,
        # Summary
        "pending": pending_count,
        "failed": failed_count,
        "avg_time_minutes": round(avg_time, 1),
        "est_remaining_hours": round(est_remaining_hours, 1),
        # Source types (sorted desc)
        "source_types": sorted(source_types.items(), key=lambda x: x[1], reverse=True),
        # Resolutions (sorted desc)
        "resolutions": sorted(resolutions.items(), key=lambda x: x[1], reverse=True),
        # Savings by source
        "savings_by_source": {
            k: {"saved": v["saved"], "original": v["original"], "count": v["count"],
                "percent": round(v["saved"] / v["original"] * 100, 1) if v["original"] > 0 else 0}
            for k, v in sorted(savings_by_source.items(),
                               key=lambda x: x[1]["saved"] / x[1]["original"] if x[1]["original"] > 0 else 0,
                               reverse=True)
        },
        # Size distribution
        "size_distribution": [
            {"label": "0-2 GB", "count": size_ranges[0]},
            {"label": "2-5 GB", "count": size_ranges[1]},
            {"label": "5-10 GB", "count": size_ranges[2]},
            {"label": "10-20 GB", "count": size_ranges[3]},
            {"label": "20+ GB", "count": size_ranges[4]},
        ],
        # Top savers
        "top_savers": top_savers,
        # Top folders
        "top_folders": [{"label": k, "value": v} for k, v in top_folders],
        # Scan-based stats
        "scan_total": len(scan_data),
        "codecs": sorted(codecs.items(), key=lambda x: x[1], reverse=True),
        "needs_conversion": needs_conversion_count,
        "already_converted": already_converted,
        "files_needing_audio_cleanup": files_needing_audio_cleanup,
        "files_audio_cleaned": files_audio_cleaned,
        # Native languages (top 7)
        "native_langs": sorted(native_langs.items(), key=lambda x: x[1], reverse=True)[:7],
        # Audio track stats
        "total_audio_tracks": total_audio_tracks,
        "audio_langs": sorted(audio_langs.items(), key=lambda x: x[1], reverse=True)[:10],
        "tracks_marked_removal": tracks_marked_removal,
        "removed_langs": sorted(removed_langs.items(), key=lambda x: x[1], reverse=True)[:10],
        # VMAF quality
        "vmaf_stats": vmaf_stats,
    }


@router.get("/timeline")
async def get_stats_timeline(days: int = Query(default=30, ge=1, le=365)):
    """Return daily_stats for the last N days with cumulative_saved."""
    db = await connect_db()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        async with db.execute(
            "SELECT * FROM daily_stats WHERE date >= ? ORDER BY date ASC",
            (cutoff,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        # Compute cumulative saved and round fps
        cumulative = 0
        for row in rows:
            cumulative += row.get("space_saved", 0)
            row["cumulative_saved"] = cumulative
            row["avg_fps"] = round(row.get("avg_fps", 0) or 0, 1)

        return {"days": rows}
    finally:
        await db.close()


@router.get("/dashboard")
async def get_dashboard():
    """Return live dashboard data."""
    db = await connect_db()
    try:
        # Running jobs
        async with db.execute(
            "SELECT id, file_path, progress, fps FROM jobs WHERE status = 'running'"
        ) as cur:
            running_rows = await cur.fetchall()
        running_jobs = [{
            "id": r["id"],
            "file_name": r["file_path"].rsplit("/", 1)[-1],
            "progress": r["progress"] or 0,
            "fps": r["fps"],
        } for r in running_rows]

        # Queue stats
        async with db.execute(
            """SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) as pending,
                COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END), 0) as running,
                COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) as completed,
                COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) as failed,
                COALESCE(SUM(CASE WHEN status='completed' AND space_saved > 0 THEN space_saved ELSE 0 END), 0) as total_saved,
                COALESCE(SUM(CASE WHEN status='completed' AND original_size > 0 THEN original_size ELSE 0 END), 0) as total_original
            FROM jobs"""
        ) as cur:
            stats = dict(await cur.fetchone())

        # Codec composition from scan results
        async with db.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN video_codec LIKE '%264%' OR video_codec LIKE '%avc%' THEN 1 ELSE 0 END), 0) as x264,
                COALESCE(SUM(CASE WHEN video_codec LIKE '%265%' OR video_codec LIKE '%hevc%' THEN 1 ELSE 0 END), 0) as x265,
                COALESCE(SUM(CASE WHEN video_codec LIKE '%av1%' THEN 1 ELSE 0 END), 0) as av1,
                COUNT(*) as total
            FROM scan_results WHERE removed_from_list = 0"""
        ) as cur:
            codecs = dict(await cur.fetchone())
        codecs["other"] = codecs["total"] - codecs["x264"] - codecs["x265"] - codecs["av1"]

        # Today's stats — compute live from jobs table for accuracy
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with db.execute(
            """SELECT
                COUNT(*) as jobs_completed,
                COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0) as space_saved,
                COALESCE(AVG(CASE WHEN fps > 0 THEN fps ELSE NULL END), 0) as avg_fps,
                COALESCE(SUM(CASE WHEN original_size > 0 THEN original_size ELSE 0 END), 0) as original_size,
                COALESCE(SUM(CASE WHEN job_type IN ('convert', 'combined') THEN 1 ELSE 0 END), 0) as x264_converted
            FROM jobs WHERE status = 'completed' AND substr(completed_at,1,10) = ?""",
            (today,),
        ) as cur:
            today_row = await cur.fetchone()
        today_stats = dict(today_row) if today_row else {
            "jobs_completed": 0, "space_saved": 0, "avg_fps": 0, "original_size": 0, "x264_converted": 0
        }
        today_stats["avg_fps"] = round(today_stats.get("avg_fps", 0) or 0, 1)

        # Combined FPS from currently running jobs
        combined_fps = sum(r["fps"] or 0 for r in running_rows)
        today_stats["combined_fps"] = round(combined_fps, 0)

        # Disk space — deduplicate by mount point (show parent volume, not each media dir)
        disk_info = []
        seen_devices: dict[int, dict] = {}  # device_id -> info
        async with db.execute("SELECT path, label FROM media_dirs") as cur:
            dir_rows = await cur.fetchall()
        for d in dir_rows:
            try:
                import os
                st = os.stat(d["path"])
                dev = st.st_dev
                if dev in seen_devices:
                    continue  # Same mount point, skip
                usage = shutil.disk_usage(d["path"])
                # Prefer the user-set label from Settings → Library; fall
                # back to the path's 2nd-level segment for legacy rows
                # without one. Pre-v0.3.101 the label was always derived
                # from the path, so a media dir at `/downloads/completed`
                # with UI label "Downloads" was shown as "completed".
                parts = d["path"].rstrip("/").split("/")
                path_derived = parts[2] if len(parts) > 2 else parts[-1]
                volume_name = (d["label"] or "").strip() or path_derived
                seen_devices[dev] = {
                    "path": d["path"],
                    "label": volume_name,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                }
            except OSError:
                pass
        disk_info = list(seen_devices.values())
        total_free = sum(d["free"] for d in disk_info)

        # Bandwidth savings
        bandwidth_pct = 0.0
        if stats["total_original"] > 0:
            bandwidth_pct = round(stats["total_saved"] / stats["total_original"] * 100, 1)

        # Setup state detection
        async with db.execute("SELECT COUNT(*) FROM media_dirs") as cur:
            has_dirs = (await cur.fetchone())[0] > 0
        async with db.execute("SELECT COUNT(*) FROM scan_results WHERE removed_from_list = 0") as cur:
            scan_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'plex_token'"
        ) as cur:
            plex_row = await cur.fetchone()
            has_plex = bool(plex_row and plex_row["value"])
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'setup_dismissed'"
        ) as cur:
            dismissed_row = await cur.fetchone()
            setup_dismissed = bool(dismissed_row and dismissed_row["value"] == "true")

        # Storage projection — estimate future savings
        projection = None
        try:
            async with db.execute(
                "SELECT COALESCE(SUM(space_saved), 0) as total_saved, COUNT(*) as days "
                "FROM daily_stats WHERE date >= ? AND jobs_completed > 0",
                ((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),),
            ) as cur:
                proj_row = await cur.fetchone()
            days_active = max(1, proj_row["days"])
            avg_daily_savings = proj_row["total_saved"] / days_active

            # Get CQ setting for estimation (same curve as scanner page)
            async with db.execute("SELECT value FROM settings WHERE key = 'nvenc_cq'") as cur:
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

            # Count remaining files excluding ignored (matches scanner "needs conversion" filter)
            LOW_BR = 3_000_000

            # Load ignored paths and folders (same as scan results endpoint)
            ignored_paths_set: set[str] = set()
            ignored_folders_list: list[str] = []
            async with db.execute("SELECT file_path, reason FROM ignored_files") as cur:
                for row in await cur.fetchall():
                    p = row["file_path"]
                    reason = row["reason"] or ""
                    if reason in ("plex_label_exempt", "rule_exempt"):
                        continue
                    ignored_paths_set.add(p)
                    if p.endswith("/"):
                        ignored_folders_list.append(p)
            ignored_folders_list.sort()

            # Load skip prefixes from encoding rules
            skip_pf: list[str] = []
            try:
                from backend.rule_resolver import get_skip_prefixes
                raw_pf = await get_skip_prefixes()
                if raw_pf:
                    skip_pf = sorted(set(raw_pf))
            except Exception:
                pass

            import bisect as _bisect

            async with db.execute(
                "SELECT file_path, file_size, duration FROM scan_results "
                "WHERE needs_conversion = 1 AND removed_from_list = 0"
            ) as cur:
                rem_rows = await cur.fetchall()

            remaining_size = 0
            remaining_count = 0
            for rr in rem_rows:
                fp = rr["file_path"]
                dur = rr["duration"] or 0
                sz = rr["file_size"] or 0
                # Skip low bitrate
                if dur > 0 and (sz * 8 / dur) < LOW_BR:
                    continue
                # Skip ignored files
                if fp in ignored_paths_set:
                    continue
                is_folder_ignored = False
                if ignored_folders_list:
                    idx = _bisect.bisect_right(ignored_folders_list, fp) - 1
                    if idx >= 0 and fp.startswith(ignored_folders_list[idx]):
                        is_folder_ignored = True
                if not is_folder_ignored and skip_pf:
                    idx = _bisect.bisect_right(skip_pf, fp) - 1
                    if idx >= 0 and fp.startswith(skip_pf[idx]):
                        is_folder_ignored = True
                if is_folder_ignored:
                    continue
                remaining_size += sz
                remaining_count += 1

            if avg_daily_savings > 0 and remaining_size > 0:
                projected_savings = int(remaining_size * est_pct)
                projected_days = round(projected_savings / avg_daily_savings) if avg_daily_savings > 0 else 0
                async with db.execute(
                    "SELECT COALESCE(AVG(jobs_completed), 0) FROM daily_stats WHERE date >= ? AND jobs_completed > 0",
                    ((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),),
                ) as cur:
                    avg_jobs_row = await cur.fetchone()
                projection = {
                    "avg_daily_savings": int(avg_daily_savings),
                    "remaining_files": remaining_count,
                    "remaining_size": remaining_size,
                    "projected_savings": projected_savings,
                    "projected_days": projected_days,
                    "avg_jobs_per_day": round(avg_jobs_row[0]),
                }
        except Exception:
            pass

        return {
            "running_jobs": running_jobs,
            "queue": {
                "pending": stats["pending"],
                "running": stats["running"],
                "completed": stats["completed"],
                "failed": stats["failed"],
            },
            "total_saved": stats["total_saved"],
            "total_original": stats["total_original"],
            "bandwidth_pct": bandwidth_pct,
            "codecs": codecs,
            "today": today_stats,
            "disk": disk_info,
            "total_free": total_free,
            "projection": projection,
            "setup": {
                "has_dirs": has_dirs,
                "scan_count": scan_count,
                "has_plex": has_plex,
                "has_jobs": stats["completed"] > 0 or stats["pending"] > 0,
                "dismissed": setup_dismissed,
            },
        }
    finally:
        await db.close()


@router.post("/setup/dismiss")
async def dismiss_setup():
    db = await connect_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES ('setup_dismissed', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = 'true'"
        )
        await db.commit()
        return {"status": "dismissed"}
    finally:
        await db.close()


@router.post("/notifications/test")
async def test_notifications_endpoint():
    from backend.notifications import test_notifications
    results = await test_notifications()
    return {"results": results}


# --- Encoder capabilities ---

@router.get("/encoder-caps")
async def get_encoder_caps(force: bool = False):
    """Return which HEVC encoders this host can drive.

    Used by Settings → Encoding to filter the encoder dropdown to only
    options that will actually run, and by the Estimate / Rule editor
    encoder pickers in v0.3.69+. Detection is cached for the process
    lifetime; `?force=1` re-probes (the Settings page exposes a
    "redetect" button for users who plug in / enable hardware after
    container start). v0.3.68+.
    """
    from backend.encoder_caps import detect_encoders
    caps = detect_encoders(force=force)
    return {
        "nvenc": caps.nvenc,
        "qsv": caps.qsv,
        "vaapi": caps.vaapi,
        # Always-present software fallback. Lets the SPA render a single
        # `available` list without special-casing libx265.
        "libx265": True,
        "available": caps.available,
        # Render-node paths picked by the auto-detection (v0.3.90+).
        # Useful for debugging multi-GPU setups: a Settings UI tooltip
        # or a `docker exec` user can confirm which `/dev/dri/renderD*`
        # the QSV / VAAPI commands will pin to.
        "qsv_render_node": caps.qsv_render_node,
        "vaapi_render_node": caps.vaapi_render_node,
    }


# --- Version / Changelog ---

_VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"
_CHANGELOG_FILE = Path(__file__).parent.parent.parent / "CHANGELOG.md"
_GITHUB_REPO = "I-IAL9000/shrinkerr"  # upstream repo for version/update checks
# TTL for the "latest version on GitHub" cache entry. Short enough that
# newly-released versions show up as "update available" to running
# containers within a reasonable window, long enough to stay well below
# GitHub's 60-req/hour unauthenticated rate limit even with a background
# refresher pinging every interval.
_UPDATE_CHECK_TTL_SECONDS = 30 * 60  # 30 minutes
_UPDATE_REFRESH_INTERVAL_SECONDS = 30 * 60  # background refresher cadence
_update_cache: dict = {}  # {version, checked_at}
_changelog_cache: dict = {}  # {mtime, entries}


def _get_current_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip()
    except Exception:
        return "0.0.0"


def _parse_changelog() -> list[dict]:
    """Parse CHANGELOG.md into a list of release entries, newest first.

    Expected format (Keep-a-Changelog):
        ## [VERSION] — YYYY-MM-DD
        Optional free-form intro paragraph.
        ### Added / Changed / Fixed / Removed / Deprecated / Security
        - bullet
        - bullet

    Returns:
        [
          {
            "version": "0.3.0",
            "date": "2026-04-21",
            "intro": "First public tagged release...",  # optional
            "sections": {"Added": [...], "Fixed": [...], ...}
          },
          ...
        ]

    Results are cached by the changelog file's mtime so repeat calls are
    cheap. Returns an empty list if CHANGELOG.md isn't present (e.g. on
    development installs that haven't committed a changelog yet).
    """
    import re

    try:
        stat = _CHANGELOG_FILE.stat()
    except OSError:
        return []

    if _changelog_cache.get("mtime") == stat.st_mtime:
        return _changelog_cache.get("entries", [])

    try:
        text = _CHANGELOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[dict] = []
    current: dict | None = None
    current_section: str | None = None

    # Match both "## [0.3.0] — 2026-04-21" and "## 0.3.0 — 2026-04-21" and
    # plain "## [Unreleased]" (no date). Em dash, en dash, and plain hyphen
    # are all accepted as the separator between version and date.
    ver_pattern = re.compile(
        r"^##\s+\[?(?P<version>[^\]\s]+?)\]?(?:\s*[—–-]\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
    )
    section_pattern = re.compile(r"^###\s+(?P<name>\S.*?)\s*$")
    bullet_pattern = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = ver_pattern.match(line)
        if m:
            # Skip the document-level "# Changelog" heading by only accepting
            # ## (H2) matches; further filter out obviously non-version
            # headings (e.g. if someone wrote "## Links" at the bottom).
            version = m.group("version")
            if not re.match(r"^\d", version) and version.lower() != "unreleased":
                continue
            current = {
                "version": version,
                "date": m.group("date"),
                "intro": "",
                "sections": {},
            }
            current_section = None
            entries.append(current)
            continue

        if current is None:
            continue

        m = section_pattern.match(line)
        if m:
            current_section = m.group("name")
            current["sections"].setdefault(current_section, [])
            continue

        m = bullet_pattern.match(line)
        if m and current_section:
            current["sections"][current_section].append(m.group("text"))
            continue

        # Collect non-bullet, non-section text before the first ### as intro.
        if current_section is None and line.strip():
            # Skip horizontal rules and link-reference-definition lines.
            if line.strip().startswith("---") or re.match(r"^\[[^\]]+\]:\s", line):
                continue
            sep = " " if current["intro"] else ""
            current["intro"] = (current["intro"] + sep + line.strip()).strip()

    # Drop an "Unreleased" entry if it carries no actual content (no
    # section bullets). Keeps the Updates UI tidy — we only surface
    # Unreleased to users once it has real entries in it.
    entries = [
        e for e in entries
        if e["version"].lower() != "unreleased"
        or any(bullets for bullets in e["sections"].values())
    ]

    _changelog_cache["mtime"] = stat.st_mtime
    _changelog_cache["entries"] = entries
    return entries


@router.get("/changelog")
async def get_changelog(limit: int = 0):
    """Return the parsed CHANGELOG.md as a list of release entries, newest first.

    Query params:
        limit — optional cap on number of entries returned. 0 / missing = all.

    Response:
        {
          "current": "0.3.0",
          "entries": [
            {
              "version": "0.3.0",
              "date": "2026-04-21",
              "intro": "First public tagged release ...",
              "sections": {"Added": [...], "Fixed": [...], ...}
            },
            ...
          ]
        }
    """
    entries = _parse_changelog()
    if limit and limit > 0:
        entries = entries[:limit]
    return {
        "current": _get_current_version(),
        "entries": entries,
    }


async def _fetch_latest_release_tag() -> str | None:
    """Hit GitHub's releases/latest and return the tag without the leading 'v'.

    Returns None on network error, rate limit, or missing release. Never
    raises — callers treat None as "no update info available".
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 200:
                return resp.json().get("tag_name", "").lstrip("v") or None
    except Exception as exc:
        print(f"[VERSION] GitHub check failed: {exc}", flush=True)
    return None


def _version_tuple(v: str) -> tuple:
    """Split "0.3.64" → (0, 3, 64). Non-numeric components sort lexicographically
    after numeric ones (so a hypothetical "0.4.0-rc1" trails "0.4.0")."""
    parts: list = []
    for part in (v or "").split("."):
        try:
            parts.append((0, int(part)))
        except ValueError:
            parts.append((1, part))
    return tuple(parts)


def _parse_release_body(version: str, date: str, body: str) -> dict:
    """Parse a GitHub release body into the same `ChangelogEntry` shape that
    `_parse_changelog()` produces for local entries.

    Release bodies are generated by `.github/workflows/release.yml` and
    follow the same Keep-a-Changelog convention as CHANGELOG.md, except
    they have an extra `## Docker images` section appended that we want
    to strip before display.
    """
    import re
    if not body:
        return {"version": version, "date": date, "intro": "", "sections": {}}

    # Drop the `## Docker images` and anything after it. Anything that
    # starts with `## ` after the per-version content is part of the
    # appended pull instructions, not the changelog body.
    cut = body.split("\n## ")
    body_only = cut[0]

    intro_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    section_pat = re.compile(r"^###\s+(?P<name>\S.*?)\s*$")
    bullet_pat = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")

    for raw in body_only.splitlines():
        line = raw.rstrip()
        m = section_pat.match(line)
        if m:
            current_section = m.group("name")
            sections.setdefault(current_section, [])
            continue
        m = bullet_pat.match(line)
        if m and current_section is not None:
            sections[current_section].append(m.group("text"))
            continue
        if current_section is None and line.strip():
            intro_lines.append(line.strip())

    return {
        "version": version,
        "date": date,
        "intro": " ".join(intro_lines).strip(),
        "sections": sections,
    }


# Cached upstream-release-list (separate from `_update_cache` so it can be
# fetched / refreshed on the user's manual click without nuking the
# background-refreshed `latest` tag).
_upstream_changelog_cache: dict = {}


async def _fetch_upstream_changelog(after_version: str) -> list[dict] | None:
    """Fetch the GitHub releases list and return parsed entries newer than
    `after_version`, newest first.

    Returns None on any error (caller falls back to the local CHANGELOG).
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases?per_page=30",
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                return None
            releases = resp.json()
    except Exception as exc:
        print(f"[VERSION] Upstream changelog fetch failed: {exc}", flush=True)
        return None

    out: list[dict] = []
    after_tuple = _version_tuple(after_version) if after_version else None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = (rel.get("tag_name") or "").lstrip("v")
        if not tag:
            continue
        if after_tuple is not None and _version_tuple(tag) <= after_tuple:
            continue
        date = (rel.get("published_at") or "")[:10]
        out.append(_parse_release_body(tag, date, rel.get("body") or ""))
    return out


@router.get("/upstream-changelog")
async def get_upstream_changelog(response: Response, force: bool = False):
    """Return parsed GitHub release entries newer than the running version.

    Used by the "Update available" modal to show the actual new content
    rather than re-rendering the user's installed CHANGELOG.md (which
    only goes up to their installed version, so the topmost entry was
    being mislabelled as LATEST). v0.3.66+.

    Cached for 30 minutes; `?force=1` bypasses the cache (fired by the
    modal on open so a hard refresh sees the freshest data without
    waiting for the background refresher).
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    import time
    current = _get_current_version()
    now = time.time()
    cache_key = current  # invalidate when running version changes
    cached = _upstream_changelog_cache.get(cache_key)
    if not force and cached and cached.get("checked_at", 0) > now - _UPDATE_CHECK_TTL_SECONDS:
        return {"current": current, "entries": cached.get("entries") or [], "source": "github"}
    entries = await _fetch_upstream_changelog(current)
    if entries is None:
        # Network failure — fall back to the local CHANGELOG so the modal
        # at least shows something (probably the user's current version's
        # release notes, which is honest about what we have on hand).
        local_entries = _parse_changelog()
        return {"current": current, "entries": local_entries[:5], "source": "local"}
    _upstream_changelog_cache[cache_key] = {"entries": entries, "checked_at": now}
    return {"current": current, "entries": entries, "source": "github"}


async def refresh_update_check() -> dict:
    """Force a fresh GitHub check and update the cache. Returns the same shape
    `/stats/version` returns. Called by the startup task, the periodic
    background refresher, and explicit `force=1` requests to the endpoint."""
    import time
    current = _get_current_version()
    tag = await _fetch_latest_release_tag()
    if tag is not None:
        _update_cache["version"] = tag
        _update_cache["checked_at"] = time.time()
    # Read from cache (fresh or stale) so we never return an outright null
    # latest just because this one check happened to fail.
    latest = _update_cache.get("version")
    return {
        "current": current,
        "latest": latest,
        "update_available": bool(latest) and latest != current,
    }


@router.get("/version")
async def get_version(response: Response, force: bool = False):
    """Return current version and check for updates.

    Cache semantics:
      - Cached result served if it's fresher than _UPDATE_CHECK_TTL_SECONDS.
      - `?force=1` bypasses the cache (used by the Settings "Check for
        updates" button) so the user can always poke manually.
      - A startup task and a background refresher keep the cache current
        without requiring any user interaction — matching the UX of
        Sonarr/Radarr/Plex where update notifications appear on the
        running version, no image pull required.

    Cache-Control note (v0.3.62): explicitly tell the browser not to
    cache this response. Without it, the browser's heuristic freshness
    rules can serve stale "update_available: false" responses for
    minutes-to-hours, and crucially the cache is keyed per origin —
    accessing Shrinkerr at `http://192.168.x:8088` and
    `https://shrinkerr.example.com` keeps two independent caches, so
    the local-network origin can stay stuck on a stale "no update"
    response while the remote origin (visited less often) re-fetches
    fresh and shows the button. `no-store` keeps both honest.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    import time
    if force:
        return await refresh_update_check()

    current = _get_current_version()
    now = time.time()
    if _update_cache.get("checked_at", 0) > now - _UPDATE_CHECK_TTL_SECONDS:
        latest = _update_cache.get("version")
        return {
            "current": current,
            "latest": latest,
            "update_available": bool(latest) and latest != current,
        }
    return await refresh_update_check()


async def update_check_loop():
    """Background task: refresh the update check at startup and every
    _UPDATE_REFRESH_INTERVAL_SECONDS after. Means the sidebar's 'Update
    available' button surfaces new releases within ~30 min of tag-push,
    even if no user has visited any page that hits /stats/version. Failures
    are logged (via _fetch_latest_release_tag) and retried next interval.
    """
    import asyncio
    # Stagger startup by a few seconds so it doesn't race with DB init.
    await asyncio.sleep(5)
    while True:
        try:
            await refresh_update_check()
        except Exception as exc:
            print(f"[VERSION] Background update refresh failed: {exc}", flush=True)
        await asyncio.sleep(_UPDATE_REFRESH_INTERVAL_SECONDS)


@router.get("/system")
async def get_system_metrics():
    """Real-time system metrics — GPU, CPU, RAM, disk I/O, network, Plex streams, Shrinkerr jobs."""
    from backend.system_metrics import get_all_metrics

    metrics = await get_all_metrics()

    # Add Plex stream info
    try:
        from backend.plex import get_active_streams
        streams = await get_active_streams()
        metrics["plex"] = streams
    except Exception:
        metrics["plex"] = {"total": 0, "transcoding": 0, "direct": 0, "sessions": []}

    # Add Shrinkerr job info
    try:
        db = await connect_db()
        try:
            async with db.execute(
                "SELECT COUNT(*) as c FROM jobs WHERE status = 'running'"
            ) as cur:
                row = await cur.fetchone()
                running = row["c"] if row else 0
            async with db.execute(
                "SELECT COUNT(*) as c FROM jobs WHERE status = 'pending'"
            ) as cur:
                row = await cur.fetchone()
                pending = row["c"] if row else 0
            async with db.execute(
                "SELECT AVG(fps) as avg_fps FROM jobs WHERE status = 'running' AND fps > 0"
            ) as cur:
                row = await cur.fetchone()
                avg_fps = round(row["avg_fps"], 1) if row and row["avg_fps"] else 0
            # Extended stats
            async with db.execute(
                """SELECT
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    COALESCE(SUM(CASE WHEN status='completed' AND space_saved > 0 THEN space_saved ELSE 0 END), 0) as total_saved,
                    COALESCE(AVG(CASE WHEN status='completed' AND fps > 0 THEN fps ELSE NULL END), 0) as lifetime_avg_fps
                FROM jobs"""
            ) as cur:
                row = await cur.fetchone()
                completed = row["completed"] or 0 if row else 0
                failed = row["failed"] or 0 if row else 0
                total_saved = row["total_saved"] or 0 if row else 0
                lifetime_avg_fps = round(row["lifetime_avg_fps"], 1) if row and row["lifetime_avg_fps"] else 0
            # Today's stats
            from datetime import date
            today_str = date.today().isoformat()
            async with db.execute(
                """SELECT COUNT(*) as c,
                    COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0) as saved
                FROM jobs WHERE status='completed' AND substr(completed_at,1,10) = ?""",
                (today_str,),
            ) as cur:
                row = await cur.fetchone()
                today_completed = row["c"] or 0 if row else 0
                today_saved = row["saved"] or 0 if row else 0
        finally:
            await db.close()

        metrics["shrinkerr"] = {
            "running_jobs": running,
            "pending_jobs": pending,
            "avg_fps": avg_fps,
            "completed_jobs": completed,
            "failed_jobs": failed,
            "total_saved": total_saved,
            "lifetime_avg_fps": lifetime_avg_fps,
            "today_completed": today_completed,
            "today_saved": today_saved,
        }
    except Exception:
        metrics["shrinkerr"] = {"running_jobs": 0, "pending_jobs": 0, "avg_fps": 0, "completed_jobs": 0, "failed_jobs": 0, "total_saved": 0, "lifetime_avg_fps": 0, "today_completed": 0, "today_saved": 0}

    return metrics
