"""Before / after frames (v0.10.0): a conversion grabs the same three
moments from the original and the new file, while both exist, for the
comparison slider in the job's details.

Frames are JPEGs, at most WIDTH pixels wide (square pixels: anamorphic DVDs
look right), in <data>/frames/<job id>/; jobs in Review and the KEEP_JOBS
most recently completed keep theirs (store). They travel as base64 (remote workers send them with the job's
result) and reach the UI as data URLs, which need no separate request — or
auth — per image.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
from collections.abc import Container
from pathlib import Path

POINTS = (0.2, 0.5, 0.8)
# 1080p as it is, to judge an encode by (it was 960 wide, and soft); 4K is
# shown at 1920 too, to keep the files a reasonable size (a few hundred KB).
WIDTH = 1920
KEEP_JOBS = 100
# Square pixels, at most WIDTH wide, even sizes; 10-bit / HDR sources to 8-bit JPEG.
_SCALE = (f"scale=w='trunc(min({WIDTH},iw*sar)/2)*2':h='trunc(ow*ih/(iw*sar)/2)*2',setsar=1")


def frames_dir() -> Path:
    from backend import database
    return Path(database.DB_PATH).parent / "frames"


async def _grab_one(path: str, seconds: float) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", path, "-frames:v", "1", "-an", "-sn",
        "-vf", _SCALE, "-pix_fmt", "yuvj420p", "-q:v", "2", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out if proc.returncode == 0 and out else None


async def grab(original: str, converted: str, duration: float) -> list[dict]:
    """[{"t", "before", "after"}] (base64 JPEGs); points where either file
    gave no frame are left out."""
    if not duration or duration <= 0:
        return []
    out = []
    for point in POINTS:
        t = round(duration * point, 3)
        before, after = await _grab_one(original, t), await _grab_one(converted, t)
        if before and after:
            out.append({"t": t, "before": base64.b64encode(before).decode(),
                        "after": base64.b64encode(after).decode()})
    return out


def save(job_id: int, frames: list[dict], keep: Container[int] | None = None, prune: bool = True) -> int:
    """Store a job's frames and, with `prune`, forget the other jobs' but
    `keep`'s (None: the newest KEEP_JOBS by id); how many."""
    if not frames:
        return 0
    root = frames_dir()
    folder = root / str(int(job_id))
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    for i, frame in enumerate(frames):
        try:
            before, after = base64.b64decode(frame["before"]), base64.b64decode(frame["after"])
            t = float(frame["t"])
        except (KeyError, TypeError, ValueError):
            continue
        (folder / f"{i}_{t:.3f}_before.jpg").write_bytes(before)
        (folder / f"{i}_{t:.3f}_after.jpg").write_bytes(after)
        saved += 1
    if not prune:
        return saved
    jobs = sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
    old = jobs[:-KEEP_JOBS] if keep is None else [p for p in jobs if int(p.name) not in keep and int(p.name) != job_id]
    for folder in old:
        shutil.rmtree(folder, ignore_errors=True)
    return saved


async def store(job_id: int, frames: list[dict]) -> int:
    """save(), keeping the frames people can still look at: every job in
    Review, compared before it's approved however many conversions came
    since, and the KEEP_JOBS most recently completed. Pruning by job id
    deleted an approved review's frames: its id is as old as its conversion."""
    if not frames:
        return 0
    from backend.database import connect_db
    try:
        db = await connect_db()
        try:
            async with db.execute(
                "SELECT id FROM jobs WHERE status = 'review' OR review_approved = 1 UNION "
                "SELECT id FROM (SELECT id FROM jobs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT ?)",
                (KEEP_JOBS,)) as cur:
                keep = {r[0] for r in await cur.fetchall()}
        finally:
            await db.close()
    except Exception as exc:  # keep everything rather than lose the conversion's frames
        print(f"[FRAMES] Couldn't read which jobs keep their frames ({exc}); none deleted this time", flush=True)
        keep = None
    saved = await asyncio.to_thread(save, job_id, frames, keep, keep is not None)
    if saved:
        await _record(((saved, job_id),))
    return saved


async def _record(counts) -> None:
    """jobs.frames_count: a job without frames that had some was pruned."""
    from backend.database import connect_db
    try:
        db = await connect_db()
        try:
            await db.executemany("UPDATE jobs SET frames_count = ? WHERE id = ?", counts)
            await db.commit()
        finally:
            await db.close()
    except Exception as exc:
        print(f"[FRAMES] Couldn't record frame counts ({exc})", flush=True)


async def record_existing() -> None:
    """Count the frames saved before jobs recorded it (at start-up), so they
    too say they were deleted once they're pruned."""
    def counts():
        root = frames_dir()
        if not root.is_dir():
            return []
        return [(n, int(p.name)) for p in root.iterdir() if p.is_dir() and p.name.isdigit()
                for n in [sum(1 for _ in p.glob("*_before.jpg"))] if n]
    found = await asyncio.to_thread(counts)
    if found:
        await _record(found)


async def why_missing(job_id: int) -> str:
    """Why a job has no frames: "deleted" (it had some: pruned, see store),
    "off" (they're turned off), else "unknown" (before 0.10, a disc…)."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute("SELECT frames_count FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            return "deleted"
        async with db.execute("SELECT value FROM settings WHERE key = 'compare_frames'") as cur:
            setting = await cur.fetchone()
    finally:
        await db.close()
    return "off" if setting and str(setting[0]).lower() == "false" else "unknown"


def load(job_id: int) -> list[dict]:
    """[{"t", "before", "after"}] with data URLs, in time order."""
    folder = frames_dir() / str(int(job_id))
    if not folder.is_dir():
        return []
    out = []
    for before in sorted(folder.glob("*_before.jpg")):  # "<i>_<t>_before.jpg", i < 10
        after = before.with_name(before.name.replace("_before.jpg", "_after.jpg"))
        if not after.is_file():
            continue
        out.append({"t": float(before.name.split("_")[1]),
                    "before": "data:image/jpeg;base64," + base64.b64encode(before.read_bytes()).decode(),
                    "after": "data:image/jpeg;base64," + base64.b64encode(after.read_bytes()).decode()})
    return out
