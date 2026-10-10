"""Before / after frames (v0.10.0): a conversion grabs the same three
moments from the original and the new file, while both exist, for the
comparison slider in the job's details.

Frames are JPEGs, at most WIDTH pixels wide (square pixels: anamorphic DVDs
look right), in <data>/frames/<job id>/; the last KEEP_JOBS jobs keep
theirs. They travel as base64 (remote workers send them with the job's
result) and reach the UI as data URLs, which need no separate request — or
auth — per image.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

POINTS = (0.2, 0.5, 0.8)
WIDTH = 960
KEEP_JOBS = 100
# Square pixels, at most WIDTH wide, even sizes; 10-bit / HDR sources to 8-bit JPEG.
_SCALE = (f"scale=w='trunc(min({WIDTH},iw*sar)/2)*2':h='trunc(ow*ih/(iw*sar)/2)*2',setsar=1")


def frames_dir() -> Path:
    from backend import database
    return Path(database.DB_PATH).parent / "frames"


async def _grab_one(path: str, seconds: float) -> bytes | None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", path, "-frames:v", "1", "-an", "-sn",
        "-vf", _SCALE, "-pix_fmt", "yuvj420p", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1",
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


def save(job_id: int, frames: list[dict]) -> int:
    """Store a job's frames (and forget the oldest jobs'); how many."""
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
    jobs = sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
    for old in jobs[:-KEEP_JOBS]:
        shutil.rmtree(old, ignore_errors=True)
    return saved


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
