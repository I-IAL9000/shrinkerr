"""Keep Dolby Vision (v0.10.0).

Profiles 7 and 8 keep it: dovi_tool takes the RPU (the Dolby Vision
metadata) from the original — profile 7's converted to 8.1, as its
enhancement layer isn't kept — the encoder re-encodes the HDR10 (or HLG)
base layer as usual, then dovi_tool puts the RPU back into the new HEVC
stream and mkvmerge rebuilds the file around it with the encode's
timestamps. Every frame keeps its own RPU, so it needs the same frames:
no resizing.

Not converted (as before): profile 5, whose base layer isn't HDR10 (the
colours would go purple and green); a resize; AV1 output; Quick Sync and
VAAPI (8-bit here); discs; and installs without dovi_tool and mkvmerge.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

KEEPABLE = ("dv7", "dv8")
TOOLS = ("dovi_tool", "mkvmerge", "mkvextract")


# Settings → Video → Dolby Vision (v0.10.0): "keep" it (the video isn't
# converted when it can't be kept), "keep_or_hdr10" (else convert the HDR
# under it), "hdr10" (always drop it). Profile 5 has no HDR under it.
MODES = ("keep", "keep_or_hdr10", "hdr10")
_mode = "keep"


def set_mode(mode: str | None) -> None:
    """The saved setting (start-up and Settings), for scans: convertible()."""
    global _mode
    _mode = mode if mode in MODES else "keep"


async def load_mode() -> None:
    """The saved mode, at start-up."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'dolby_vision_mode'") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    set_mode(row[0] if row else None)


def drop_to(hdr_format: str | None, base: str | None) -> str | None:
    """The HDR left when this Dolby Vision is dropped — "hdr10" or "hlg"
    (scanner.dv_base_of) — or None when it can't be: only profiles 7 and 8
    have an HDR picture under it (profile 5's turns purple and green)."""
    return base if hdr_format in KEEPABLE and base in ("hdr10", "hlg") else None


@functools.lru_cache(maxsize=1)
def _strip_filter() -> bool:
    """ffmpeg 7.1+ has the dovi_rpu filter, which strips the metadata."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-bsfs"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip() == "dovi_rpu" for line in out.splitlines())


@functools.lru_cache(maxsize=1)
def _x265_writes_it() -> bool:
    """ffmpeg 7.1+'s libx265 writes Dolby Vision from the source's metadata
    unless told not to (-dolbyvision); older ones have no such option."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx265"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "-dolbyvision" in out


def can_strip() -> bool:
    """Whether an encode can be left with no Dolby Vision: before 7.1 ffmpeg
    has no dovi_rpu filter and copies the source's configuration record into
    the output (v0.10.0)."""
    return _strip_filter()


def strip_args(encoder: str) -> list[str]:
    """Output options that leave no Dolby Vision in an encode that drops it:
    libx265 and SVT-AV1 write it from the source's metadata by default, and
    the dovi_rpu filter removes what's left (its frames' metadata, the
    configuration record)."""
    args = ["-dolbyvision", "0"] if encoder == "libx265" and _x265_writes_it() else []
    if _strip_filter():
        args += ["-bsf:v", "dovi_rpu=strip=1"]
    return args


async def left_in(path: str) -> bool:
    """Whether a file still carries Dolby Vision: its stream's configuration
    record, or its first frame's metadata."""
    from backend.scanner import first_frame_info, hdr_format_of
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries", "stream=side_data_list",
        "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        streams = json.loads(out or b"{}").get("streams") or []
    except ValueError:
        streams = []
    if any((hdr_format_of(s) or "").startswith("dv") for s in streams):
        return True
    frame = await first_frame_info(path)
    return bool(frame) and (hdr_format_of(frame) or "").startswith("dv")


def tools_available() -> bool:
    return all(shutil.which(t) for t in TOOLS)


def convertible(hdr_format: str | None) -> bool:
    """Whether scans offer a Dolby Vision file for conversion (the job
    decides the rest: the encoder, a resize, AV1). Without the tools only
    when the setting drops it."""
    return hdr_format in KEEPABLE and (tools_available() or _mode != "keep")


def why_not_kept(hdr_format: str | None, *, encoder: str, output_codec: str | None,
                 target_resolution: str | None, disc_type: str | None) -> str | None:
    """Why this Dolby Vision can't be kept through an encode — "disc",
    "profile" (not 7 or 8), "tools", "encoder", "codec" (not HEVC) or
    "resize" — or None when it can. The message said all of them at once,
    so it didn't say what to change (v0.10.0)."""
    if disc_type:
        return "disc"
    if hdr_format not in KEEPABLE:
        return "profile"
    if not tools_available():
        return "tools"
    if encoder in ("qsv", "vaapi"):
        return "encoder"
    if (output_codec or "hevc") != "hevc":
        return "codec"
    if (target_resolution or "copy") != "copy":
        return "resize"
    return None


def can_keep(hdr_format: str | None, *, encoder: str, output_codec: str | None, target_resolution: str | None,
             disc_type: str | None) -> bool:
    return why_not_kept(hdr_format, encoder=encoder, output_codec=output_codec,
                        target_resolution=target_resolution, disc_type=disc_type) is None


async def _run(*args: str, stdin=None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdin=stdin, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    return proc.returncode, err.decode(errors="replace").strip()


async def _hevc_into(source: str, *dovi_args: str) -> tuple[int, str]:
    """The source's first video stream as an HEVC bitstream, piped into dovi_tool."""
    read, write = os.pipe()
    try:
        ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", "-i", source, "-map", "0:V:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "pipe:1",
            stdout=write, stderr=asyncio.subprocess.PIPE)
    finally:
        os.close(write)
    try:
        code, err = await _run("dovi_tool", *dovi_args, stdin=read)
    finally:
        os.close(read)
    _, ff_err = await ffmpeg.communicate()
    if ffmpeg.returncode:
        return ffmpeg.returncode, ff_err.decode(errors="replace").strip()
    return code, err


async def rpu_frames(rpu: str) -> int | None:
    proc = await asyncio.create_subprocess_exec("dovi_tool", "info", "--summary", "-i", rpu,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    m = re.search(r"Frames:\s*(\d+)", out.decode(errors="replace"))
    return int(m[1]) if m else None


async def video_frames(path: str) -> int | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
        "stream=nb_read_packets", "-of", "csv=p=0", path, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        return int(out.decode().strip().split(",")[0])
    except ValueError:
        return None


async def keep(source: str, output: str, hdr_format: str) -> str | None:
    """Put the source's Dolby Vision into `output` (replaced in place);
    why it couldn't, or None."""
    from backend.scanner import first_frame_info, hdr_format_of
    out = Path(output)
    work = tempfile.mkdtemp(prefix=".shrinkerr-dovi-", dir=out.parent)
    rpu, base, injected = (os.path.join(work, n) for n in ("rpu.bin", "base.hevc", "dv.hevc"))
    timestamps, rebuilt = os.path.join(work, "timestamps.txt"), os.path.join(work, "out.converting.mkv")
    try:
        # The base layer stays HDR10 / HLG, and 10-bit.
        src, enc = await first_frame_info(source), await first_frame_info(output)
        if (not src or not enc or enc.get("color_transfer") != src.get("color_transfer")
                or not any(d in (enc.get("pix_fmt") or "") for d in ("10", "12"))):
            return "the encode lost the HDR base layer"
        mode = ("-m", "2") if hdr_format == "dv7" else ()  # profile 7 → 8.1
        code, err = await _hevc_into(source, *mode, "extract-rpu", "-", "-o", rpu)
        if code or not os.path.getsize(rpu):
            return f"couldn't read the Dolby Vision metadata ({err or code})"
        rpus, frames = await rpu_frames(rpu), await video_frames(output)
        if rpus is None or rpus != frames:
            return f"the encode has {frames} frames and the Dolby Vision metadata {rpus}"
        for step in (
            ("ffmpeg", "-v", "error", "-nostdin", "-i", output, "-map", "0:v:0", "-c:v", "copy",
             "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", base),
            ("dovi_tool", "inject-rpu", "-i", base, "--rpu-in", rpu, "-o", injected),
            ("mkvextract", output, "timestamps_v2", f"0:{timestamps}"),
            ("mkvmerge", "-q", "-o", rebuilt, "--timestamps", f"0:{timestamps}", injected, "-D", output),
        ):
            code, err = await _run(*step)
            if code not in (0, 1) or (code == 1 and step[0] != "mkvmerge"):  # mkvmerge: 1 = warnings
                return f"{step[0]} failed ({err or code})"
        frame = await first_frame_info(rebuilt)
        if not frame or not (hdr_format_of(frame) or "").startswith("dv"):
            return "the rebuilt file has no Dolby Vision"
        out.unlink()  # never rename onto an existing file (an SMB share's recycle bin keeps it)
        os.rename(rebuilt, output)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
