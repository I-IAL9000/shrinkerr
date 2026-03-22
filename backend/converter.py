import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional


def build_ffmpeg_cmd(
    input_path: str,
    output_path: str,
    encoder: str = "nvenc",
    cq: int = 20,
    crf: int = 20,
) -> list[str]:
    """Build an ffmpeg command list for converting a file to HEVC."""
    cmd = ["ffmpeg", "-y", "-i", input_path]

    if encoder == "nvenc":
        cmd += [
            "-c:v", "hevc_nvenc",
            "-preset", "p7",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(cq),
            "-b:v", "0",
            "-profile:v", "main10",
            "-pix_fmt", "p010le",
        ]
    else:
        # libx265
        cmd += [
            "-c:v", "libx265",
            "-preset", "medium",
            "-crf", str(crf),
            "-profile:v", "main10",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:rd=4:psy-rd=2.0",
        ]

    cmd += [
        "-c:a", "copy",
        "-c:s", "copy",
        "-map", "0",
        output_path,
    ]
    return cmd


def rename_x264_to_x265(filename: str) -> str:
    """Replace x264/h264 codec identifiers in a filename with x265 (case-insensitive)."""
    result = re.sub(r'\bx264\b', 'x265', filename, flags=re.IGNORECASE)
    result = re.sub(r'\bh264\b', 'x265', result, flags=re.IGNORECASE)
    return result


def get_output_path(input_path: str) -> str:
    """Return the final output path: rename codec tag and change extension to .mkv."""
    p = Path(input_path)
    new_stem = rename_x264_to_x265(p.stem)
    return str(p.parent / (new_stem + ".mkv"))


def get_temp_path(input_path: str) -> str:
    """Return a temporary conversion path in the same directory as input."""
    p = Path(input_path)
    return str(p.parent / (p.stem + ".converting.mkv"))


def parse_ffmpeg_progress(line: str, duration: float) -> Optional[dict]:
    """
    Parse an ffmpeg stderr line for progress information.

    Returns a dict with keys: progress (0-100 float), fps (float or None),
    eta_seconds (int or None). Returns None if the line lacks time info.
    """
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
        progress = min(100.0, elapsed / duration * 100)
        remaining = duration - elapsed
        fps_match = re.search(r'fps=\s*(\d+(?:\.\d+)?)', line)
        fps_val = float(fps_match.group(1)) if fps_match else None
        if fps_val and fps_val > 0:
            # eta based on fps is unreliable without frame count; use time ratio
            eta_seconds = int(remaining) if remaining > 0 else 0
        else:
            eta_seconds = int(remaining) if remaining > 0 else 0
    else:
        fps_val = None

    fps_match = re.search(r'fps=\s*(\d+(?:\.\d+)?)', line)
    fps_val = float(fps_match.group(1)) if fps_match else None

    return {
        "progress": round(progress, 2),
        "fps": fps_val,
        "eta_seconds": eta_seconds,
    }


async def convert_file(
    input_path: str,
    encoder: str,
    duration: float,
    progress_callback: Optional[Callable] = None,
) -> dict:
    """
    Convert a video file to HEVC.

    Checks free disk space (needs at least the original file size free),
    runs ffmpeg, parses progress, verifies output, deletes original, and
    renames the temp file to its final path.

    Returns a dict with: success (bool), output_path (str), space_saved (int),
    error (str or None).
    """
    input_path = str(input_path)
    p = Path(input_path)

    # Check free disk space
    try:
        original_size = p.stat().st_size
    except OSError as exc:
        return {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}

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

    temp_path = get_temp_path(input_path)
    final_path = get_output_path(input_path)

    from backend.config import settings

    cmd = build_ffmpeg_cmd(input_path, temp_path, encoder=encoder)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # ffmpeg writes progress using \r (carriage return), not \n.
        # Read in small chunks and split on \r to parse progress lines.
        buffer = ""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.decode(errors="replace")
            # Split on \r or \n to find progress lines
            while "\r" in buffer or "\n" in buffer:
                # Find earliest delimiter
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
                if progress_callback and line:
                    parsed = parse_ffmpeg_progress(line, duration)
                    if parsed:
                        await progress_callback(**parsed)

        await asyncio.wait_for(proc.wait(), timeout=settings.ffmpeg_timeout)

        if proc.returncode != 0:
            # Clean up temp file if it exists
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "success": False,
                "output_path": None,
                "space_saved": 0,
                "error": f"ffmpeg exited with code {proc.returncode}",
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

    # Verify output exists and has non-zero size
    temp = Path(temp_path)
    if not temp.exists() or temp.stat().st_size == 0:
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": "Output file missing or empty after conversion",
        }

    output_size = temp.stat().st_size
    space_saved = original_size - output_size

    # Delete original, rename temp to final
    try:
        p.unlink()
        temp.rename(final_path)
    except OSError as exc:
        return {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}

    return {
        "success": True,
        "output_path": final_path,
        "space_saved": space_saved,
        "error": None,
    }
