import asyncio
import shutil
from pathlib import Path
from typing import Callable, Optional


def build_remux_cmd(
    input_path: str,
    output_path: str,
    keep_audio_indices: list[int],
) -> list[str]:
    """
    Build an ffmpeg command to remux keeping only the specified audio stream indices.

    Maps all video, subtitle, and attachment streams plus only the requested audio
    streams. All streams are copied without re-encoding.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-map", "0:v?",
        "-map", "0:s?",
        "-map", "0:t?",
    ]
    for idx in keep_audio_indices:
        cmd += ["-map", f"0:{idx}"]
    cmd += ["-c", "copy", output_path]
    return cmd


async def remux_audio(
    input_path: str,
    keep_audio_indices: list[int],
    progress_callback: Optional[Callable] = None,
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

    cmd = build_remux_cmd(input_path, temp_path, keep_audio_indices)

    from backend.config import settings

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr; optionally report progress
        while True:
            line_bytes = await proc.stderr.readline()
            if not line_bytes:
                break
            if progress_callback:
                line = line_bytes.decode(errors="replace").strip()
                if line:
                    await progress_callback(line=line)

        await asyncio.wait_for(proc.wait(), timeout=settings.ffmpeg_timeout)

        if proc.returncode != 0:
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
