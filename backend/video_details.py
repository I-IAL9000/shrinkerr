"""Video details for files scanned before 0.10.0, which stored only the height:
width, frame rate, aspect ratio, bit depth, HDR, scan type and video bitrate.

A full scan reads them, but over the network that's 12+ hours for a big
library, and the Picture filters (10-bit, Hi10P, interlaced, VFR) and the file
panel need them now. So they're read on their own: a file's when its panel
opens (read_details), the rest in the background, a few at a time
(fill_missing). Just ffprobe, without subtitle language detection, which reads
up to 30 minutes of a file."""
import asyncio
from typing import Callable, Optional

from backend import scanner
from backend.database import connect_db
from backend.resolution import resolution_tier

COLUMNS = ("video_width", "video_height", "hdr_format", "video_fps", "video_bit_depth",
           "video_interlaced", "video_vfr", "video_dar", "video_bitrate")
# Not read yet: no width (a scan before 0.10.0; a conversion since then stored
# the frame rate but not the width), or the width alone (0.10 test builds of
# 9-10 Oct 2026; a scan since then stores the bit depth even when the file
# doesn't say its frame rate). Not discs (probing one reads the disc over the
# network, up to 3 minutes) or files whose probe failed.
TO_READ = ("removed_from_list = 0 AND disc_type IS NULL AND COALESCE(probe_status, 'ok') = 'ok' "
           "AND (COALESCE(video_width, 0) = 0 OR (video_fps IS NULL AND video_bit_depth IS NULL))")
# The last scan_results id the fill tried. A new key when what's read grows,
# so a test build that already went through the list goes through it again.
CURSOR_KEY = "video_details_fill_cursor_2"
# Not one file of a batch read: the share is likely down (or stalls), so the
# same files are tried again after these waits (seconds), then passed by.
RETRY_WAITS = (300, 600, 1200, 2400, 3600, 3600)


def detail_columns(probe: dict) -> dict:
    """The scan_results columns a probe gives; unknown is NULL, as a scan stores it."""
    facts = scanner.video_facts(probe)

    def flag(value):
        return None if value is None else int(value)
    return {
        "video_width": probe.get("video_width") or 0,
        "video_height": probe.get("video_height") or 0,
        "hdr_format": probe.get("hdr_format"),
        "video_fps": facts["video_fps"] or None,
        "video_bit_depth": facts["video_bit_depth"] or None,
        "video_interlaced": flag(facts["video_interlaced"]),
        "video_vfr": flag(facts["video_vfr"]),
        "video_dar": facts["video_dar"] or None,
        "video_bitrate": facts["video_bitrate"],
    }


async def _probe(path: str) -> Optional[dict]:
    probe = await scanner.probe_file(path, detect_und_subs=False)
    return detail_columns(probe) if probe else None


async def _store(updates: list[tuple[tuple, dict]], cursor: Optional[int] = None) -> None:
    """updates: ((id, file_path, file_size) as read before the probe, columns).
    A row whose file changed meanwhile (a conversion finished) keeps its own."""
    db = await connect_db()
    try:
        if updates:
            await db.executemany(
                f"UPDATE scan_results SET {', '.join(f'{c} = ?' for c in COLUMNS)} "
                "WHERE id = ? AND file_path = ? AND file_size IS ?",
                [[cols[c] for c in COLUMNS] + list(row) for row, cols in updates])
        if cursor is not None:
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (CURSOR_KEY, str(cursor)))
        await db.commit()
    finally:
        await db.close()


async def read_details(file_path: str) -> Optional[dict]:
    """A listed file's video details and resolution tier, read from the file
    first if its scan didn't. None: not a file in the Scanner list."""
    db = await connect_db()
    try:
        async with db.execute(f"SELECT id, file_size, {', '.join(COLUMNS)}, ({TO_READ}) AS to_read "
                              "FROM scan_results WHERE file_path = ?", (file_path,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    if row is None:
        return None
    details = {c: row[c] for c in COLUMNS}
    if row["to_read"]:
        fresh = await _probe(file_path)
        if fresh:
            await _store([((row["id"], file_path, row["file_size"]), fresh)])
            details = fresh
    return {**details, "resolution": resolution_tier(details["video_width"], details["video_height"], file_path)}


async def fill_missing(busy: Callable[[], bool] = lambda: False, batch: int = 50, concurrency: int = 2) -> int:
    """Read every listed file still missing its details, in id order, a batch
    at a time. The position is saved with each batch, so a restart carries on
    where it stopped and each file is tried once (one gone from disk too).
    Waits while busy() (a scan is running), and when not one file of a batch
    reads (RETRY_WAITS). Returns the files read."""
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (CURSOR_KEY,)) as cur:
            saved = await cur.fetchone()
        position = int(saved[0]) if saved else 0
        async with db.execute(f"SELECT COUNT(*) FROM scan_results WHERE id > ? AND {TO_READ}", (position,)) as cur:
            total = (await cur.fetchone())[0]
    finally:
        await db.close()
    if total:
        print(f"[DETAILS] Reading the video details of {total} file(s) scanned before 0.10.0", flush=True)
    sem = asyncio.Semaphore(concurrency)

    async def one(row: tuple):
        async with sem:
            return row, await _probe(row[1])

    read = tried = failures = 0
    while True:
        if busy():
            await asyncio.sleep(60)
            continue
        db = await connect_db()
        try:
            async with db.execute(f"SELECT id, file_path, file_size FROM scan_results WHERE id > ? AND {TO_READ} "
                                  "ORDER BY id LIMIT ?", (position, batch)) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]
        finally:
            await db.close()
        if not rows:
            break
        results = await asyncio.gather(*(one(r) for r in rows))
        done = [(row, cols) for row, cols in results if cols]
        if not done and failures < len(RETRY_WAITS):
            # Moving on would pass by every file while the share is down
            # (the position is saved): try these again later.
            if failures == 0:
                print(f"[DETAILS] Couldn't read any of {len(rows)} file(s) (share not mounted?); "
                      "trying them again later", flush=True)
            await asyncio.sleep(RETRY_WAITS[failures])
            failures += 1
            continue
        failures = 0
        position = rows[-1][0]
        await _store(done, cursor=position)
        read += len(done)
        tried += len(rows)
        if tried // 1000 != (tried - len(rows)) // 1000:
            print(f"[DETAILS] {tried}/{total} files", flush=True)
    if total:
        print(f"[DETAILS] Done: read {read} of {tried} file(s)", flush=True)
    return read


async def video_details_loop() -> None:
    """fill_missing() after the startup backfills, pausing during scans, until
    it gets through: after an error (a locked database) it carries on later
    from its saved position."""
    await asyncio.sleep(120)
    from backend.routes.scan import scan_is_actively_running
    wait = 600
    while True:
        try:
            if await fill_missing(busy=scan_is_actively_running):
                from backend.websocket import ws_manager
                await ws_manager.send_scan_results_changed(added=0, removed=0)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[DETAILS] Reading video details failed: {exc}; trying again in {wait // 60} min",
                  flush=True)
        await asyncio.sleep(wait)
        wait = min(wait * 2, 3600)
