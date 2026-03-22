import aiosqlite
from pathlib import Path
from backend.config import settings

DB_PATH = settings.db_path

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
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
                queue_order INTEGER
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
        """)
        await db.commit()
    finally:
        await db.close()
