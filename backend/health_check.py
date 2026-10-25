"""Video file health checking.

Two modes:
- quick:    ffprobe the file — verifies container/streams parse (~sub-second per file)
- thorough: decode the entire file with ffmpeg, capturing any decoder errors
            (~duration / 10 roughly, depends on codec & hardware)

Returned dict shape:
    {
        "status": "healthy" | "corrupt" | "warnings",
        "errors": [str, ...],       # lines ffprobe/ffmpeg emitted on stderr
        "check_type": "quick" | "thorough",
        "duration_seconds": float,  # wall time of the check
    }

Classification rules:
    * ffprobe/ffmpeg emits nothing and exits 0           → "healthy"
    * All stderr lines match a known benign pattern       → "warnings"
      (file is still considered healthy for purposes of
      auto-ignore / queue decisions — we just surface the
      messages so the user can see what was noticed)
    * Any stderr line matches a known *fatal* pattern     → "corrupt"
      (an audio decoder's lost frame — GLITCH_PATTERNS — only past
      AUDIO_GLITCHES of them: a few are a warning)
    * Unrecognised stderr lines OR non-zero exit          → "corrupt"

The benign patterns below are documented false positives that crop up on
otherwise perfectly playable releases. The most common by far is x264
encoders using 5+ reference frames at 1080p, which technically violates
H.264 Level 4.0 but every decoder in existence tolerates it. Without this
allow-list, ~80% of scene releases would be flagged "corrupt."
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Literal


HealthStatus = Literal["healthy", "corrupt", "warnings"]


# Patterns that mean "technically out-of-spec but plays fine everywhere" —
# purely informational. Matched case-insensitively against each stderr line.
BENIGN_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    # H.264 encoders that use more reference frames than the level allows.
    # Classic x264 behaviour — harmless.
    r"number of reference frames.*exceeds max",
    # MKVs with slightly non-monotonic audio/subtitle DTS. Trivial.
    r"application provided invalid, non monotonically increasing dts",
    r"non-monotonous dts",
    # Duration estimation; happens whenever the container doesn't store
    # duration at the top — ffprobe falls back to bitrate. Not corruption.
    r"estimating duration from bitrate",
    # B-frame parsing quirks — the decoder recovers automatically.
    r"co located pocs unavailable",
    # A frame that refers to a picture from before the first keyframe — a
    # broadcast recording that starts mid-GOP, or one a clean-up cut before
    # v0.10.0 — is decoded without it; pictures it can't decode are
    # "concealing … errors" (fatal, below).
    r"\[h264 @ [^\]]+\]\s*mmco: unref short failure",
    # Edge-list warnings on some mp4/m4v remuxes. Plays fine.
    r"edit list elements are not supported",
    r"edit list starts at a non-zero offset",
    # DVD/Bluray passthrough streams that lack explicit stream duration.
    r"stream \d+.*duration not set",
    # PGS/subtitle-only warnings.
    r"could not find codec parameters for stream.*subtitle",
    # mov/mp4 fragmented container chatter.
    r"found duplicated moov atom",
    # mkv: known-fine hint that ffmpeg prints for certain encoders.
    r"using cpu capabilities",
    # Atmos / TrueHD informational notices.
    r"substream \d+: skipping",
    # Minor timestamp rounding warnings.
    r"past duration.*too large",
    # Unknown-but-decodable private data — not corruption.
    r"unknown cuvid format",
    # Bitstream filter parsing — ffmpeg does NOT fail here.
    r"svc_extension_flag not implemented",
    # eac3 (Dolby Digital Plus) decoder is strict about exponent encoding;
    # many streaming-service rips trip these warnings but play perfectly in
    # Plex / MPV / VLC / hardware decoders. Codec-scoped to avoid shadowing
    # real decode errors from other streams.
    r"\[eac3 @ [^\]]+\]\s*expacc \d+ is out-of-range",
    r"\[eac3 @ [^\]]+\]\s*exponent \d+ is out-of-range",  # the same check's other message (v0.10.0)
    r"\[eac3 @ [^\]]+\]\s*error decoding the audio block",
))


# Patterns that ALWAYS mean the file is broken, even if earlier lines looked
# benign. Any match → corrupt regardless of the rest.
FATAL_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"moov atom not found",
    r"invalid nal unit size",
    r"invalid data found when processing input",
    r"error while decoding stream",
    r"decoder_generic_error",
    r"error splitting the input into nal units",
    r"truncating packet of size",
    r"end of file",                     # in context, means premature EOF
    r"concealing \d+ dc, \d+ ac, \d+ mv errors",
    r"frame_type mismatch",
    r"invalid starting bit",
    r"header missing",
    r"failed to read header",
    r"no frame!",
    r"file ended prematurely",
))


# An audio decoder losing a frame now and then — a DVD rip's MP3 track with
# a damaged frame plays fine, the player skips it — is a warning up to
# AUDIO_GLITCHES times in the file (v0.10.0: one "Header missing" marked a
# playable episode corrupt); past that the audio is damaged.
GLITCH_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\[(?:mp3|mp3float|mp2|mp2float|mp1|mp1float|aac|ac3|ac3_fixed|eac3) @ [^\]]+\]\s*header missing",
))
AUDIO_GLITCHES = 10

# ffmpeg collapses a line said again into this (v0.10.0): the line before it,
# N more times — not a message of its own.
_REPEATED = re.compile(r"^last message repeated (\d+) times?$", re.IGNORECASE)


def _is_glitch(line: str) -> bool:
    return any(p.search(line) for p in GLITCH_PATTERNS)


def classify_errors(stderr_text: str, returncode: int) -> tuple[HealthStatus, list[str]]:
    """Classify ffprobe/ffmpeg stderr into (status, cleaned_errors).

    Returns the health status label plus the stderr lines we want to surface
    to the user (deduplicated, capped at 10 to avoid log spam).
    """
    # A few audio glitches are benign; many are damage (counted before the dedup).
    lines: list[str] = []
    glitches = 0
    for ln in (ln.strip() for ln in (stderr_text or "").splitlines()):
        repeated = _REPEATED.match(ln)
        if repeated:
            glitches += int(repeated.group(1)) if lines and _is_glitch(lines[-1]) else 0
        elif ln:
            lines.append(ln)
            glitches += _is_glitch(ln)
    # Deduplicate while preserving order — decoders often repeat the same warning
    # thousands of times, which isn't useful.
    seen: set[str] = set()
    unique: list[str] = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique.append(ln)

    capped = unique[:10]

    def glitch(ln: str) -> bool:
        return glitches <= AUDIO_GLITCHES and _is_glitch(ln)

    # Any fatal match → corrupt, full stop.
    for ln in unique:
        if any(p.search(ln) for p in FATAL_PATTERNS) and not glitch(ln):
            return "corrupt", capped

    # Non-zero returncode without a recognised fatal line — still treat as
    # corrupt but let the stderr speak for itself.
    if returncode != 0:
        return "corrupt", capped or [f"exit code {returncode}"]

    # No stderr at all → healthy.
    if not unique:
        return "healthy", []

    # If every line matches a benign pattern, the file is effectively healthy
    # but we surface the messages under the "warnings" label.
    if all(glitch(ln) or any(p.search(ln) for p in BENIGN_PATTERNS) for ln in unique):
        return "warnings", capped

    # Unrecognised stderr → treat as corrupt. Better to surface false
    # positives than silently ignore genuinely bad files; the allow-list
    # above is the lever for tuning this over time.
    return "corrupt", capped


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

    status, errors = classify_errors(err, rc)
    return {
        "status": status,
        "errors": errors,
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
    # Thorough decode with -xerror — ffmpeg stops at the first real error, so
    # any stderr is likely meaningful. We still run it through the classifier
    # so benign decoder chatter (reference-frame warnings, past-duration
    # notices) doesn't mark cleanly-decoded files as corrupt.
    stderr_blob = "\n".join(errors_collected)
    status, errors = classify_errors(stderr_blob, rc)
    return {
        "status": status,
        "errors": errors,
        "check_type": "thorough",
        "duration_seconds": round(time.monotonic() - t0, 2),
    }


async def run_check(file_path: str, mode: str = "quick", progress_cb=None, duration_seconds_hint: float = 0) -> dict:
    """Dispatch to quick_check or thorough_check based on mode."""
    if mode == "thorough":
        return await thorough_check(file_path, progress_cb=progress_cb, duration_seconds_hint=duration_seconds_hint)
    return await quick_check(file_path)
