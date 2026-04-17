import asyncio
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional


def build_remux_cmd(
    input_path: str,
    output_path: str,
    keep_audio_indices: list[int],
    keep_subtitle_indices: list[int] | None = None,
    external_subtitle_files: list[dict] | None = None,
) -> list[str]:
    """
    Build an ffmpeg command to remux keeping only the specified audio and subtitle stream indices.

    Maps all video and attachment streams plus only the requested audio and subtitle
    streams. External subtitle files (if provided) are added as additional inputs.
    All streams are copied without re-encoding (except text subs that need conversion).
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
    ]
    # Add external subtitle files as additional inputs
    ext_subs = external_subtitle_files or []
    for es in ext_subs:
        cmd += ["-i", es["path"]]

    cmd += ["-map", "0:v?"]

    # Subtitles: if indices provided, map selectively; otherwise keep all
    out_sub_idx = 0
    sub_codec_args: list[str] = []
    if keep_subtitle_indices is not None:
        for idx in keep_subtitle_indices:
            cmd += ["-map", f"0:{idx}"]
            sub_codec_args += [f"-c:s:{out_sub_idx}", "copy"]
            out_sub_idx += 1
    else:
        cmd += ["-map", "0:s?"]
        # Can't set per-stream codec without explicit maps; copy all
        if not ext_subs:
            sub_codec_args = []  # will use global -c copy

    # Map external subtitle inputs
    for i, es in enumerate(ext_subs):
        input_idx = i + 1
        cmd += ["-map", f"{input_idx}:s"]
        codec = (es.get("codec") or "subrip").lower()
        if codec in ("subrip", "srt", "webvtt"):
            sub_codec_args += [f"-c:s:{out_sub_idx}", "srt"]
        elif codec in ("ass", "ssa"):
            sub_codec_args += [f"-c:s:{out_sub_idx}", "copy"]
        else:
            sub_codec_args += [f"-c:s:{out_sub_idx}", "copy"]
        lang = es.get("language") or "und"
        sub_codec_args += [f"-metadata:s:s:{out_sub_idx}", f"language={lang}"]
        if es.get("forced"):
            sub_codec_args += [f"-disposition:s:{out_sub_idx}", "forced"]
        out_sub_idx += 1

    cmd += ["-map", "0:t?"]
    for idx in keep_audio_indices:
        cmd += ["-map", f"0:{idx}"]

    if ext_subs or keep_subtitle_indices is not None:
        # Use per-stream codec args + copy for non-sub streams
        cmd += ["-c:v", "copy", "-c:a", "copy"] + sub_codec_args
    else:
        cmd += ["-c", "copy"]

    cmd += [output_path]
    return cmd


def parse_remux_progress(line: str, duration: float, start_time: float = 0) -> Optional[dict]:
    """Parse ffmpeg stderr for remux progress (same format as conversion)."""
    time_match = re.search(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', line)
    if not time_match:
        return None

    hours = int(time_match.group(1))
    minutes = int(time_match.group(2))
    seconds = float(time_match.group(3))
    elapsed = hours * 3600 + minutes * 60 + seconds

    progress = 0.0
    eta_seconds = None
    if duration and duration > 0:
        progress_ratio = elapsed / duration
        progress = min(100.0, progress_ratio * 100)

        if start_time > 0 and progress_ratio > 0.01:
            wall_elapsed = time.monotonic() - start_time
            eta_seconds = int(wall_elapsed / progress_ratio * (1 - progress_ratio))

    speed_match = re.search(r'speed=\s*([\d.]+)x', line)
    speed = float(speed_match.group(1)) if speed_match else None

    return {
        "progress": round(progress, 2),
        "speed": speed,
        "eta_seconds": eta_seconds,
    }


async def remux_audio(
    input_path: str,
    keep_audio_indices: list[int],
    duration: float = 0,
    progress_callback: Optional[Callable] = None,
    keep_subtitle_indices: list[int] | None = None,
) -> dict:
    """
    Remux a file, keeping only the specified audio streams.

    Runs ffmpeg with stream copy (no re-encoding). Output is always .mkv.
    Verifies output, replaces original.

    Returns a dict with: success (bool), output_path (str), space_saved (int),
    error (str or None).
    """
    input_path = str(input_path)
    p = Path(input_path)

    try:
        original_size = p.stat().st_size
    except OSError as exc:
        return {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}

    # Check free disk space
    stat = shutil.disk_usage(str(p.parent))
    if stat.free < original_size:
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": (
                f"Not enough free disk space: need {original_size} bytes, "
                f"have {stat.free} bytes free"
            ),
        }

    temp_path = str(p.parent / (p.stem + ".remuxing.mkv"))
    final_path = str(p.parent / (p.stem + ".mkv"))

    cmd = build_remux_cmd(input_path, temp_path, keep_audio_indices, keep_subtitle_indices)
    print(f"[REMUX] Starting: {input_path}", flush=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Read stderr in chunks and parse progress (ffmpeg uses \r for progress)
        remux_start = time.monotonic()
        buffer = ""
        last_lines: list[str] = []
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.decode(errors="replace")
            while "\r" in buffer or "\n" in buffer:
                r_pos = buffer.find("\r")
                n_pos = buffer.find("\n")
                if r_pos == -1:
                    pos = n_pos
                elif n_pos == -1:
                    pos = r_pos
                else:
                    pos = min(r_pos, n_pos)
                line = buffer[:pos].strip()
                buffer = buffer[pos + 1:]
                if line:
                    last_lines.append(line)
                    if len(last_lines) > 20:
                        last_lines.pop(0)
                if progress_callback and line and duration > 0:
                    parsed = parse_remux_progress(line, duration, start_time=remux_start)
                    if parsed:
                        await progress_callback(
                            progress=parsed["progress"],
                            eta_seconds=parsed["eta_seconds"],
                            speed=parsed.get("speed"),
                        )

        from backend.converter import get_live_encoding_settings
        live = await get_live_encoding_settings()
        await asyncio.wait_for(proc.wait(), timeout=live.get("ffmpeg_timeout", 21600))

        if proc.returncode != 0:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            error_lines = [l for l in last_lines if not l.startswith("frame=") and not l.startswith("size=")]
            error_detail = "\n".join(error_lines[-10:]) if error_lines else ""
            error_msg = f"ffmpeg exited with code {proc.returncode}"
            if error_detail:
                error_msg += f"\n\n{error_detail}"
            return {
                "success": False,
                "output_path": None,
                "space_saved": 0,
                "error": error_msg,
            }

    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": "ffmpeg timed out",
        }
    except Exception as exc:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            pass
        return {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}

    # Verify output
    temp = Path(temp_path)
    if not temp.exists() or temp.stat().st_size == 0:
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": "Output file missing or empty after remux",
        }

    output_size = temp.stat().st_size
    space_saved = original_size - output_size

    try:
        # Check if we should trash or permanently delete the original
        use_trash = False
        try:
            import aiosqlite
            from backend.database import DB_PATH
            import sqlite3
            db = sqlite3.connect(DB_PATH)
            row = db.execute("SELECT value FROM settings WHERE key = 'trash_original_after_conversion'").fetchone()
            db.close()
            use_trash = row and row[0].lower() == "true"
        except Exception:
            pass

        if use_trash:
            try:
                from send2trash import send2trash
                send2trash(str(p))
            except Exception:
                p.unlink()
        else:
            p.unlink()
        temp.rename(final_path)
    except OSError as exc:
        import os
        print(f"[REMUX] Permission error: {exc}", flush=True)
        print(f"  Original: {p} (exists={p.exists()})", flush=True)
        print(f"  Temp: {temp} (exists={temp.exists()})", flush=True)
        print(f"  Running as uid={os.getuid()}, gid={os.getgid()}", flush=True)
        # Retry: delete original then rename
        try:
            if p.exists():
                p.unlink()
            temp.rename(final_path)
            print(f"[REMUX] Retry succeeded", flush=True)
        except OSError as exc2:
            return {"success": False, "output_path": None, "space_saved": 0, "error": f"{exc} (retry: {exc2})"}

    print(f"[REMUX] Done: saved {space_saved} bytes", flush=True)
    return {
        "success": True,
        "output_path": final_path,
        "space_saved": space_saved,
        "error": None,
    }
