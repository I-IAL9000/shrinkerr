"""Video file health checking.

Two modes:
- quick:    ffprobe the file — verifies container/streams parse (~sub-second per file)
- thorough: decode the entire file with ffmpeg, capturing any decoder errors
            (~duration / 10 roughly, depends on codec & hardware)

Returned dict shape:
    {
        "status": "healthy" | "corrupt" | "warnings",
        "errors": [str, ...],       # decoder errors (thorough) or probe errors (quick)
        "check_type": "quick" | "thorough",
        "duration_seconds": float,  # wall time of the check
    }

A file is "corrupt" if ffprobe fails (quick) or ffmpeg emits any error-level
log lines (thorough). "warnings" is reserved for non-fatal anomalies that we
might want to surface separately in the future; for now a check returns either
"healthy" or "corrupt".
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal


HealthStatus = Literal["healthy", "corrupt", "warnings"]


async def quick_check(file_path: str, timeout: int = 30) -> dict:
    """Parse container/streams with ffprobe. Fast; catches truncated/malformed files."""
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-hide_banner",
        "-show_entries", "stream=codec_type",
        "-of", "default=nw=1",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "status": "corrupt",
            "errors": [f"ffprobe timed out after {timeout}s"],
            "check_type": "quick",
            "duration_seconds": round(time.monotonic() - t0, 2),
        }

    err = (stderr.decode(errors="replace") or "").strip()
    rc = proc.returncode or 0

    if rc != 0 or err:
        # Split multi-line errors and keep the first few to avoid bloat
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        return {
            "status": "corrupt",
            "errors": lines[:10] if lines else [f"ffprobe exited {rc}"],
            "check_type": "quick",
            "duration_seconds": round(time.monotonic() - t0, 2),
        }

    return {
        "status": "healthy",
        "errors": [],
        "check_type": "quick",
        "duration_seconds": round(time.monotonic() - t0, 2),
    }


async def thorough_check(file_path: str, timeout: int = 7200, progress_cb=None, duration_seconds_hint: float = 0) -> dict:
    """Fully decode the file; flag any decoder errors.

    Uses ``-f null -`` so no output is written. ``-xerror`` makes ffmpeg exit
    non-zero on decoder errors rather than trying to recover silently. An
    optional ``progress_cb(percent: float)`` is called as the decode advances
    (needs ``duration_seconds_hint`` to convert timestamps to percent).
    """
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v", "error",
        "-hide_banner",
        "-xerror",
        "-progress", "pipe:1",
        "-nostats",
        "-i", file_path,
        "-map", "0:v",
        "-f", "null",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    errors_collected: list[str] = []
    last_pct = 0.0

    async def _drain_stdout():
        nonlocal last_pct
        if proc.stdout is None:
            return
        buf = ""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line.startswith("out_time_ms=") and duration_seconds_hint > 0 and progress_cb:
                    try:
                        us = int(line.split("=", 1)[1])
                        pct = min(99.0, (us / 1_000_000) / duration_seconds_hint * 100.0)
                        if pct - last_pct >= 1.0:
                            last_pct = pct
                            await progress_cb(pct)
                    except Exception:
                        pass

    async def _drain_stderr():
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            txt = line.decode(errors="replace").strip()
            if txt:
                errors_collected.append(txt)

    try:
        await asyncio.wait_for(
            asyncio.gather(_drain_stdout(), _drain_stderr(), proc.wait()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {
            "status": "corrupt",
            "errors": [f"thorough check timed out after {timeout}s"],
            "check_type": "thorough",
            "duration_seconds": round(time.monotonic() - t0, 2),
        }

    rc = proc.returncode or 0
    if rc != 0 or errors_collected:
        return {
            "status": "corrupt",
            "errors": errors_collected[:20] if errors_collected else [f"ffmpeg exited {rc}"],
            "check_type": "thorough",
            "duration_seconds": round(time.monotonic() - t0, 2),
        }

    return {
        "status": "healthy",
        "errors": [],
        "check_type": "thorough",
        "duration_seconds": round(time.monotonic() - t0, 2),
    }


async def run_check(file_path: str, mode: str = "quick", progress_cb=None, duration_seconds_hint: float = 0) -> dict:
    """Dispatch to quick_check or thorough_check based on mode."""
    if mode == "thorough":
        return await thorough_check(file_path, progress_cb=progress_cb, duration_seconds_hint=duration_seconds_hint)
    return await quick_check(file_path)
