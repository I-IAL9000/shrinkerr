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
import os
import re
import shutil
import tempfile
from pathlib import Path

KEEPABLE = ("dv7", "dv8")
TOOLS = ("dovi_tool", "mkvmerge", "mkvextract")


def tools_available() -> bool:
    return all(shutil.which(t) for t in TOOLS)


def convertible(hdr_format: str | None) -> bool:
    """Whether scans offer a Dolby Vision file for conversion (the job
    decides the rest: the encoder, a resize, AV1)."""
    return hdr_format in KEEPABLE and tools_available()


def can_keep(hdr_format: str | None, *, encoder: str, output_codec: str | None, target_resolution: str | None,
             disc_type: str | None) -> bool:
    return (hdr_format in KEEPABLE and tools_available() and encoder not in ("qsv", "vaapi")
            and (output_codec or "hevc") == "hevc" and (target_resolution or "copy") == "copy" and not disc_type)


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
