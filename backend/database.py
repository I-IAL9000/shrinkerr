import aiosqlite
from pathlib import Path
from backend.config import settings

DB_PATH = settings.db_path

BUSY_TIMEOUT = 30000  # 30 seconds — wait for locks instead of failing


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT}")
    return db


async def connect_db() -> aiosqlite.Connection:
    """Open a DB connection with WAL mode and busy timeout. Use this instead of aiosqlite.connect(DB_PATH)."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT}")
    return db

async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS media_dirs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                label TEXT,
                enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                dir_id INTEGER REFERENCES media_dirs(id),
                file_size INTEGER NOT NULL,
                video_codec TEXT,
                needs_conversion INTEGER DEFAULT 0,
                audio_tracks_json TEXT,
                native_language TEXT,
                scan_timestamp TEXT NOT NULL,
                removed_from_list INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                job_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                encoder TEXT,
                audio_tracks_to_remove TEXT,
                progress REAL DEFAULT 0,
                fps REAL,
                eta_seconds INTEGER,
                error_log TEXT,
                space_saved INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                queue_order INTEGER,
                nvenc_preset TEXT DEFAULT NULL,
                nvenc_cq INTEGER DEFAULT NULL,
                audio_codec TEXT DEFAULT NULL,
                audio_bitrate INTEGER DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_start TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ignored_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE NOT NULL,
                reason TEXT,
                ignored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_type TEXT NOT NULL,
                media_id TEXT NOT NULL,
                original_language TEXT,
                raw_api_language TEXT,
                looked_up_at TEXT NOT NULL,
                UNIQUE(id_type, media_id)
            );
            CREATE TABLE IF NOT EXISTS encoding_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                match_type TEXT NOT NULL DEFAULT '',
                match_value TEXT NOT NULL DEFAULT '',
                match_conditions TEXT DEFAULT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                action TEXT NOT NULL DEFAULT 'encode',
                enabled INTEGER NOT NULL DEFAULT 1,
                encoder TEXT DEFAULT NULL,
                nvenc_preset TEXT DEFAULT NULL,
                nvenc_cq INTEGER DEFAULT NULL,
                libx265_crf INTEGER DEFAULT NULL,
                target_resolution TEXT DEFAULT NULL,
                audio_codec TEXT DEFAULT NULL,
                audio_bitrate INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                jobs_completed INTEGER DEFAULT 0,
                space_saved INTEGER DEFAULT 0,
                original_size INTEGER DEFAULT 0,
                avg_fps REAL DEFAULT 0,
                total_encode_seconds REAL DEFAULT 0,
                x264_converted INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS plex_metadata_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT NOT NULL,
                metadata_type TEXT NOT NULL,
                metadata_value TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                UNIQUE(folder_path, metadata_type, metadata_value)
            );
        """)
        # Migration: add original_size column to jobs if missing
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN original_size INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Migration: add per-job encoding settings columns
        for col, coltype in [
            ("nvenc_preset", "TEXT DEFAULT NULL"),
            ("nvenc_cq", "INTEGER DEFAULT NULL"),
            ("audio_codec", "TEXT DEFAULT NULL"),
            ("audio_bitrate", "INTEGER DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists
        # Migration: add is_new column to scan_results (legacy, kept for compat)
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN is_new INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Migration: add new_detected_at column (ISO timestamp set by watcher, NULL from scanner)
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN new_detected_at TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Migration: add converted flag (set when Squeezarr completes a job for this file)
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN converted INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        # Migration: add file_mtime column to scan_results
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN file_mtime REAL DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Migration: add duration column to scan_results (seconds, from ffprobe)
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN duration REAL DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Migration: add subtitle_tracks_json column to scan_results
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN subtitle_tracks_json TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Migration: add subtitle_tracks_to_remove column to jobs
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN subtitle_tracks_to_remove TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists
        # Migration: add per-job libx265_crf and target_resolution columns
        for col, coltype in [
            ("libx265_crf", "INTEGER DEFAULT NULL"),
            ("target_resolution", "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists
        # Migration: add match_conditions JSON column to encoding_rules
        try:
            await db.execute("ALTER TABLE encoding_rules ADD COLUMN match_conditions TEXT DEFAULT NULL")
        except Exception:
            pass
        # Migration: add priority column to jobs (0=Normal, 1=High, 2=Highest)
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: add queue_priority column to encoding_rules
        try:
            await db.execute("ALTER TABLE encoding_rules ADD COLUMN queue_priority INTEGER DEFAULT NULL")
        except Exception:
            pass
        # Migration: add audio_codec and audio_bitrate columns to encoding_rules
        for col, coltype in [
            ("audio_codec", "TEXT DEFAULT NULL"),
            ("audio_bitrate", "INTEGER DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE encoding_rules ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # Column already exists
        # Migration: pre-computed flags on scan_results for performance
        for col, coltype in [
            ("has_removable_tracks_flag", "INTEGER DEFAULT 0"),
            ("has_removable_subs_flag", "INTEGER DEFAULT 0"),
            ("has_lossless_audio_flag", "INTEGER DEFAULT 0"),
            ("dup_count", "INTEGER DEFAULT 0"),
            ("dup_group", "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE scan_results ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # Migration: add video_height column to scan_results
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN video_height INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: add probe_status column to scan_results
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN probe_status TEXT DEFAULT 'ok'")
        except Exception:
            pass
        # Migration: add vmaf_score to scan_results
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN vmaf_score REAL DEFAULT NULL")
        except Exception:
            pass
        # Migration: add vmaf_score to jobs
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN vmaf_score REAL DEFAULT NULL")
        except Exception:
            pass
        # Migration: add avg_vmaf to daily_stats
        try:
            await db.execute("ALTER TABLE daily_stats ADD COLUMN avg_vmaf REAL DEFAULT 0")
        except Exception:
            pass
        # Migration: undo conversion support
        for col, coltype in [
            ("backup_path", "TEXT DEFAULT NULL"),
            ("original_file_path", "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # Migration: conversion log per file
        for col, coltype in [
            ("ffmpeg_command", "TEXT DEFAULT NULL"),
            ("ffmpeg_log", "TEXT DEFAULT NULL"),
            ("encoding_stats", "TEXT DEFAULT NULL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        # Poster cache table
        await db.execute("""CREATE TABLE IF NOT EXISTS poster_cache (
            folder_path TEXT PRIMARY KEY,
            title TEXT,
            year TEXT,
            poster_url TEXT,
            source TEXT,
            plex_rating_key TEXT,
            image_data TEXT,
            resolved_at TEXT
        )""")
        # Migration: add image_data and metadata to poster_cache
        for col, ctype in [
            ("image_data", "TEXT"),
            ("rating", "REAL"),
            ("genres", "TEXT"),
            ("status", "TEXT"),
            ("network", "TEXT"),
            ("country", "TEXT"),
            ("media_type", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE poster_cache ADD COLUMN {col} {ctype}")
            except Exception:
                pass
        # Migration: add language_source to scan_results (api/heuristic)
        try:
            await db.execute("ALTER TABLE scan_results ADD COLUMN language_source TEXT DEFAULT 'heuristic'")
        except Exception:
            pass
        # Create indexes for fast filtered queries
        await db.execute("CREATE INDEX IF NOT EXISTS idx_plex_meta_folder ON plex_metadata_cache(folder_path)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_order ON jobs(status, queue_order)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_completed ON jobs(status, completed_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_scan_results_filepath ON scan_results(file_path)")

        # Backfill converted flag from completed jobs (for files converted before flag existed)
        # Excludes jobs where no space was saved (ignored/no-op conversions)
        await db.execute("""
            UPDATE scan_results SET converted = 1
            WHERE converted = 0 AND file_path IN (
                SELECT file_path FROM jobs
                WHERE status = 'completed' AND job_type IN ('convert', 'combined') AND space_saved > 0
            )
        """)

        # Backfill VMAF scores from jobs to scan_results (match by original_file_path or file_path)
        await db.execute("""
            UPDATE scan_results SET vmaf_score = (
                SELECT j.vmaf_score FROM jobs j
                WHERE j.vmaf_score IS NOT NULL AND j.vmaf_score > 0
                AND j.status = 'completed'
                AND (j.file_path = scan_results.file_path OR j.original_file_path = scan_results.file_path)
                ORDER BY j.completed_at DESC LIMIT 1
            )
            WHERE vmaf_score IS NULL AND file_path IN (
                SELECT file_path FROM jobs WHERE vmaf_score IS NOT NULL AND vmaf_score > 0 AND status = 'completed'
                UNION
                SELECT original_file_path FROM jobs WHERE vmaf_score IS NOT NULL AND vmaf_score > 0 AND status = 'completed' AND original_file_path IS NOT NULL
            )
        """)

        await db.commit()
    finally:
        await db.close()


async def update_daily_stats_for_job(job: dict) -> None:
    """Update daily_stats aggregation table for a completed job."""
    from datetime import datetime
    completed_at = job.get("completed_at")
    if not completed_at:
        return
    try:
        date_str = datetime.fromisoformat(completed_at).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return

    space_saved = max(0, job.get("space_saved", 0) or 0)
    original_size = job.get("original_size", 0) or 0

    # Compute encode time in seconds
    encode_seconds = 0.0
    started_at = job.get("started_at")
    if started_at and completed_at:
        try:
            t0 = datetime.fromisoformat(started_at)
            t1 = datetime.fromisoformat(completed_at)
            encode_seconds = max(0, (t1 - t0).total_seconds())
        except (ValueError, TypeError):
            pass

    # Check if this was an x264 conversion
    file_path = job.get("file_path", "")
    is_x264 = 1 if job.get("job_type") in ("convert", "combined") else 0

    db = await connect_db()
    try:
        await db.execute(
            """INSERT INTO daily_stats (date, jobs_completed, space_saved, original_size,
                   total_encode_seconds, x264_converted)
               VALUES (?, 1, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   jobs_completed = jobs_completed + 1,
                   space_saved = space_saved + ?,
                   original_size = original_size + ?,
                   total_encode_seconds = total_encode_seconds + ?,
                   x264_converted = x264_converted + ?""",
            (date_str, space_saved, original_size, encode_seconds, is_x264,
             space_saved, original_size, encode_seconds, is_x264),
        )
        # Store average per-job FPS for the day (useful for tracking GPU performance)
        await db.execute(
            """UPDATE daily_stats SET avg_fps = COALESCE(
                   (SELECT AVG(fps) FROM jobs
                    WHERE status='completed' AND substr(completed_at,1,10)=? AND fps > 0),
               0) WHERE date = ?""",
            (date_str, date_str),
        )
        await db.commit()
    finally:
        await db.close()


async def backfill_daily_stats() -> None:
    """One-time backfill of daily_stats from existing completed jobs."""
    db = await connect_db()
    try:
        # Check if backfill needs to run (or re-run if avg_fps is all zeros from a bug)
        async with db.execute("SELECT COUNT(*) FROM daily_stats") as cur:
            count = (await cur.fetchone())[0]
        if count > 0:
            # Check if avg_fps looks like per-job AVG (reasonable) or SUM (inflated)
            async with db.execute("SELECT MAX(avg_fps) FROM daily_stats") as cur:
                max_fps = (await cur.fetchone())[0] or 0
            if 0 < max_fps < 2000:
                return  # Already backfilled correctly with AVG
            # Re-backfill — fix inflated SUM values back to AVG
            await db.execute("DELETE FROM daily_stats")
            print("[DB] Re-backfilling daily_stats (fixing FPS to per-job AVG)...", flush=True)

        print("[DB] Backfilling daily_stats from completed jobs...", flush=True)
        await db.execute(
            """INSERT INTO daily_stats (date, jobs_completed, space_saved, original_size,
                   avg_fps, total_encode_seconds, x264_converted)
               SELECT
                   substr(completed_at,1,10) as d,
                   COUNT(*) as jobs_completed,
                   COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN original_size > 0 THEN original_size ELSE 0 END), 0),
                   COALESCE(AVG(CASE WHEN fps > 0 THEN fps ELSE NULL END), 0),
                   COALESCE(SUM(
                       CASE WHEN started_at IS NOT NULL AND completed_at IS NOT NULL
                       THEN (julianday(completed_at) - julianday(started_at)) * 86400
                       ELSE 0 END
                   ), 0),
                   SUM(CASE WHEN job_type IN ('convert', 'combined') THEN 1 ELSE 0 END)
               FROM jobs
               WHERE status = 'completed' AND completed_at IS NOT NULL
               GROUP BY d
               ORDER BY d"""
        )
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM daily_stats") as cur:
            filled = (await cur.fetchone())[0]
        print(f"[DB] Backfilled {filled} days of stats", flush=True)
    finally:
        await db.close()
