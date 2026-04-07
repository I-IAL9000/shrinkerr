import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("squeezarr.converter")


async def get_live_encoding_settings() -> dict:
    """Read encoding settings from the DB at call time (not the frozen config singleton)."""
    import json
    import aiosqlite
    from backend.database import DB_PATH

    defaults = {
        "default_encoder": "nvenc",
        "nvenc_cq": 20,
        "libx265_crf": 20,
        "nvenc_preset": "p6",
        "ffmpeg_timeout": 21600,
        "ffprobe_timeout": 30,
        "audio_codec": "copy",
        "audio_bitrate": 128,
    }
    try:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT key, value FROM settings") as cur:
                rows = await cur.fetchall()
                db_settings = {r["key"]: r["value"] for r in rows}
        finally:
            await db.close()

        result = dict(defaults)
        if "nvenc_cq" in db_settings:
            result["nvenc_cq"] = int(db_settings["nvenc_cq"])
        if "libx265_crf" in db_settings:
            result["libx265_crf"] = int(db_settings["libx265_crf"])
        if "nvenc_preset" in db_settings:
            result["nvenc_preset"] = db_settings["nvenc_preset"]
        if "default_encoder" in db_settings:
            result["default_encoder"] = db_settings["default_encoder"]
        if "ffmpeg_timeout" in db_settings:
            result["ffmpeg_timeout"] = int(db_settings["ffmpeg_timeout"])
        if "audio_codec" in db_settings:
            result["audio_codec"] = db_settings["audio_codec"]
        if "audio_bitrate" in db_settings:
            result["audio_bitrate"] = int(db_settings["audio_bitrate"])
        if "auto_convert_lossless" in db_settings:
            result["auto_convert_lossless"] = db_settings["auto_convert_lossless"].lower() == "true"
        if "lossless_target_codec" in db_settings:
            result["lossless_target_codec"] = db_settings["lossless_target_codec"]
        if "lossless_target_bitrate" in db_settings:
            result["lossless_target_bitrate"] = int(db_settings["lossless_target_bitrate"])
        if "target_resolution" in db_settings:
            result["target_resolution"] = db_settings["target_resolution"]
        if "trash_original_after_conversion" in db_settings:
            result["trash_original_after_conversion"] = db_settings["trash_original_after_conversion"].lower() == "true"
        if "backup_original_days" in db_settings:
            result["backup_original_days"] = int(db_settings["backup_original_days"])
        if "custom_ffmpeg_flags" in db_settings:
            result["custom_ffmpeg_flags"] = db_settings["custom_ffmpeg_flags"]
        if "backup_folder" in db_settings:
            result["backup_folder"] = db_settings["backup_folder"]
        if "vmaf_analysis_enabled" in db_settings:
            result["vmaf_analysis_enabled"] = db_settings["vmaf_analysis_enabled"].lower() == "true"
        if "filename_suffix" in db_settings:
            result["filename_suffix"] = db_settings["filename_suffix"]
        return result
    except Exception as exc:
        print(f"[CONVERT] Failed to read DB settings, using defaults: {exc}", flush=True)
        return defaults


LOSSLESS_AUDIO_CODECS = {"truehd", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray", "flac", "mlp", "pcm_dvd"}
# DTS profiles that are lossless (plain DTS and DTS Express are lossy)
DTS_LOSSLESS_PROFILES = {"dts-hd ma", "dts-hd hra"}


def is_lossless_audio(codec: str, profile: str = "") -> bool:
    """Check if an audio codec/profile combo is lossless."""
    c = codec.lower()
    if c in LOSSLESS_AUDIO_CODECS:
        return True
    if c == "dts" and profile:
        return profile.lower() in DTS_LOSSLESS_PROFILES
    return False


RESOLUTION_MAP = {
    "1080p": "1920:-2",
    "720p": "1280:-2",
    "480p": "854:-2",
}


def _audio_codec_args(codec: str, bitrate: int) -> list[str]:
    """Return ffmpeg args for a given audio codec."""
    if codec == "copy":
        return ["copy"]
    if codec == "eac3":
        return ["eac3", "-b:a", f"{bitrate}k"]
    if codec == "ac3":
        return ["ac3", "-b:a", f"{bitrate}k"]
    if codec == "aac":
        return ["aac", "-b:a", f"{bitrate}k"]
    if codec == "opus":
        return ["libopus", "-b:a", f"{bitrate}k"]
    if codec == "flac":
        return ["flac"]
    return [codec, "-b:a", f"{bitrate}k"]


def build_ffmpeg_cmd(
    input_path: str,
    output_path: str,
    encoder: str = "nvenc",
    cq: int = 20,
    crf: int = 20,
    nvenc_preset: str = "p6",
    audio_codec: str = "copy",
    audio_bitrate: int = 128,
    lossless_conversion: dict | None = None,
    audio_stream_codecs: list[str] | None = None,
    target_resolution: str = "copy",
    subtitle_streams: list[dict] | None = None,
) -> list[str]:
    """Build an ffmpeg command list for converting a file to HEVC.

    lossless_conversion: if set, dict with 'codec' and 'bitrate' for lossless audio streams.
    audio_stream_codecs: list of codec names per audio stream (from ffprobe), needed for per-stream lossless conversion.
    target_resolution: "copy", "1080p", "720p", or "480p".
    """
    cmd = ["ffmpeg", "-y", "-i", input_path]

    # Resolution scaling (applied before video encoder)
    scale = RESOLUTION_MAP.get(target_resolution)
    if scale:
        if encoder == "nvenc":
            # Use CUDA-accelerated scaling for NVENC
            cmd += ["-vf", f"scale={scale}"]
        else:
            cmd += ["-vf", f"scale={scale}"]

    if encoder == "nvenc":
        cmd += [
            "-c:v", "hevc_nvenc",
            "-preset", nvenc_preset,
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

    # Audio encoding — per-stream lossless conversion or global codec
    if lossless_conversion and audio_stream_codecs:
        target_codec = lossless_conversion["codec"]
        target_bitrate = lossless_conversion["bitrate"]
        profiles = lossless_conversion.get("profiles", [""] * len(audio_stream_codecs))
        for idx, stream_codec in enumerate(audio_stream_codecs):
            profile = profiles[idx] if idx < len(profiles) else ""
            if is_lossless_audio(stream_codec, profile):
                args = _audio_codec_args(target_codec, target_bitrate)
                cmd += [f"-c:a:{idx}"] + args
            else:
                # Also apply global audio_codec to lossy streams if not "copy"
                args = _audio_codec_args(audio_codec, audio_bitrate)
                cmd += [f"-c:a:{idx}"] + args
    else:
        args = _audio_codec_args(audio_codec, audio_bitrate)
        cmd += ["-c:a"] + args

    # Map video and audio
    cmd += ["-map", "0:v", "-map", "0:a"]

    # Map subtitle streams — only include codecs the matroska muxer supports.
    # Unsupported codecs (e.g. codec_id 94213) cause "Subtitle codec not supported" errors.
    SUPPORTED_SUB_CODECS = {
        "subrip", "srt", "ass", "ssa", "webvtt",
        "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle",
        "hdmv_text_subtitle",
    }
    if subtitle_streams:
        for sub in subtitle_streams:
            codec = (sub.get("codec_name") or "").lower()
            idx = sub.get("index")
            if codec in SUPPORTED_SUB_CODECS and idx is not None:
                cmd += ["-map", f"0:{idx}"]
            else:
                print(f"[CONVERT] Skipping unsupported subtitle stream #{idx} codec={codec or 'unknown'}", flush=True)
        if any((s.get("codec_name") or "").lower() in SUPPORTED_SUB_CODECS for s in subtitle_streams):
            cmd += ["-c:s", "copy"]
    else:
        # No subtitle info available — skip subs rather than risk unsupported codecs
        print("[CONVERT] No subtitle stream info from probe — skipping subtitle mapping", flush=True)

    # Map attachments (fonts etc.)
    cmd += ["-map", "0:t?"]

    cmd += [output_path]
    return cmd


def rename_x264_to_x265(filename: str) -> str:
    """Replace x264/h264/AVC codec identifiers in a filename with x265 (case-insensitive)."""
    result = re.sub(r'\bx264\b', 'x265', filename, flags=re.IGNORECASE)
    result = re.sub(r'\bh264\b', 'x265', result, flags=re.IGNORECASE)
    result = re.sub(r'\bAVC\b', 'x265', result)
    # Remove "Remux" since re-encoded files are no longer remuxes
    result = re.sub(r'\s*\bRemux\b\s*', ' ', result, flags=re.IGNORECASE).strip()
    # Clean up any double spaces left behind
    result = re.sub(r'  +', ' ', result)
    return result


# Map ffprobe codec names to common filename tags
AUDIO_CODEC_DISPLAY = {
    "eac3": "EAC3",
    "ac3": "AC3",
    "aac": "AAC",
    "dts": "DTS",
    "truehd": "TrueHD",
    "flac": "FLAC",
    "pcm_s16le": "LPCM",
    "pcm_s24le": "LPCM",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "mp3": "MP3",
    "mp2": "MP2",
}

# DTS profiles reported by ffprobe
DTS_PROFILES = {
    "DTS-HD MA": "DTS-HD MA",
    "DTS-HD HRA": "DTS-HD HRA",
    "DTS Express": "DTS Express",
    "DTS-ES": "DTS-ES",
    "DTS 96/24": "DTS",
}

# Patterns to match audio codec tags in filenames (order matters — match longer first)
AUDIO_FILENAME_PATTERNS = [
    r'DTS[\-\s]?HD[\s\.]?MA',
    r'DTS[\-\s]?HD[\s\.]?HRA',
    r'DTS[\-\s]?HD',
    r'DTS[\-\s]?ES',
    r'Dolby[\s\.]?Digital[\s\.]?Plus',
    r'DD[\+P]',
    r'DDP',
    r'TrueHD[\s\.]?Atmos',
    r'Atmos',
    r'TrueHD',
    r'EAC3',
    r'E\-AC\-3',
    r'AC3',
    r'AC\-3',
    r'DTS',
    r'AAC',
    r'FLAC',
    r'LPCM',
    r'PCM',
    r'Opus',
    r'MP3',
]


def get_audio_display_name(codec: str, profile: str = "") -> str:
    """Get a clean display name for an audio codec from ffprobe data."""
    c = codec.lower()
    # DTS has sub-profiles
    if c == "dts" and profile:
        for key, display in DTS_PROFILES.items():
            if key.lower() in profile.lower():
                return display
        return "DTS"
    return AUDIO_CODEC_DISPLAY.get(c, codec.upper())


def rename_audio_codec_in_filename(filename: str, new_audio_tag: str) -> str:
    """Replace audio codec tags in a filename with the actual primary audio codec."""
    # Build a combined pattern matching any known audio codec tag
    combined = "|".join(AUDIO_FILENAME_PATTERNS)
    # Only replace the first match (the primary audio codec in the filename)
    result = re.sub(combined, new_audio_tag, filename, count=1, flags=re.IGNORECASE)
    return result


def get_output_path(input_path: str, suffix: str = "") -> str:
    """Return the final output path: rename codec tag, add suffix, and change extension to .mkv."""
    p = Path(input_path)
    new_stem = rename_x264_to_x265(p.stem)
    if suffix:
        new_stem = new_stem + suffix
    return str(p.parent / (new_stem + ".mkv"))


def get_temp_path(input_path: str) -> str:
    """Return a temporary conversion path in the same directory as input."""
    p = Path(input_path)
    return str(p.parent / (p.stem + ".converting.mkv"))


SUBTITLE_EXTENSIONS = {".srt", ".sub", ".idx", ".ass", ".ssa", ".sup", ".vtt"}


def rename_external_subtitles(original_path: str, new_stem: str) -> None:
    """Rename external subtitle files that match the original filename stem."""
    p = Path(original_path)
    original_stem = p.stem
    parent = p.parent

    for f in parent.iterdir():
        if (
            f.is_file()
            and f.name.startswith(original_stem)
            and f.suffix.lower() in SUBTITLE_EXTENSIONS
        ):
            # The part after the original stem (e.g. ".eng" in "Movie.x264-GROUP.eng.srt")
            remainder = f.name[len(original_stem):]
            new_name = new_stem + remainder
            new_path = parent / new_name
            try:
                f.rename(new_path)
                print(f"[CONVERT] Renamed subtitle: {f.name} -> {new_name}", flush=True)
            except OSError as exc:
                print(f"[CONVERT] Failed to rename subtitle {f.name}: {exc}", flush=True)


def parse_ffmpeg_progress(line: str, duration: float, start_time: float = 0) -> Optional[dict]:
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
        progress_ratio = elapsed / duration
        progress = min(100.0, progress_ratio * 100)

        if start_time > 0 and progress_ratio > 0.01:
            wall_elapsed = time.monotonic() - start_time
            eta_seconds = int(wall_elapsed / progress_ratio * (1 - progress_ratio))
        else:
            eta_seconds = None
    else:
        pass

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
    proc_callback: Optional[Callable] = None,
    override_preset: Optional[str] = None,
    override_cq: Optional[int] = None,
    override_audio_codec: Optional[str] = None,
    override_audio_bitrate: Optional[int] = None,
    override_crf: Optional[int] = None,
    override_target_resolution: Optional[str] = None,
    nice: bool = False,
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
    print(f"[CONVERT] Starting: {input_path} (encoder={encoder}, duration={duration:.1f}s)", flush=True)

    # Check free disk space
    try:
        original_size = p.stat().st_size
    except OSError as exc:
        print(f"[CONVERT] Cannot stat file: {exc}", flush=True)
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

    # Read live settings from DB (not the config singleton which is frozen at startup)
    live_settings = await get_live_encoding_settings()
    filename_suffix = live_settings.get("filename_suffix", "")
    final_path = get_output_path(input_path, suffix=filename_suffix)
    nvenc_preset = override_preset if override_preset is not None else live_settings.get("nvenc_preset", "p6")
    cq = override_cq if override_cq is not None else live_settings.get("nvenc_cq", 20)
    crf = override_crf if override_crf is not None else live_settings.get("libx265_crf", 20)
    audio_codec = override_audio_codec if override_audio_codec is not None else live_settings.get("audio_codec", "copy")
    audio_bitrate = override_audio_bitrate if override_audio_bitrate is not None else live_settings.get("audio_bitrate", 128)

    # Probe file for audio/subtitle stream details
    lossless_conversion = None
    audio_stream_codecs = None
    subtitle_streams = None
    try:
        from backend.scanner import probe_file
        probe_data = await probe_file(input_path)
        if probe_data:
            # Subtitle streams for safe mapping (skip unsupported codecs)
            # Map probe format to what build_ffmpeg_cmd expects
            raw_subs = probe_data.get("subtitle_tracks", [])
            subtitle_streams = [{"codec_name": s.get("codec", ""), "index": s.get("stream_index")} for s in raw_subs]

            # Lossless audio auto-conversion
            if live_settings.get("auto_convert_lossless", False) and probe_data.get("audio_tracks"):
                target_codec = live_settings.get("lossless_target_codec", "eac3")
                target_bitrate = live_settings.get("lossless_target_bitrate", 640)
                tracks = probe_data["audio_tracks"]
                audio_stream_codecs = [t.get("codec", "unknown") for t in tracks]
                audio_stream_profiles = [t.get("profile", "") for t in tracks]
                has_lossless = any(is_lossless_audio(c, p) for c, p in zip(audio_stream_codecs, audio_stream_profiles))
                if has_lossless:
                    lossless_conversion = {"codec": target_codec, "bitrate": target_bitrate, "profiles": audio_stream_profiles}
                    lossless_names = [c for c, p in zip(audio_stream_codecs, audio_stream_profiles) if is_lossless_audio(c, p)]
                    print(f"[CONVERT] Lossless audio detected ({', '.join(lossless_names)}), converting to {target_codec} {target_bitrate}k", flush=True)
    except Exception as exc:
        print(f"[CONVERT] Failed to probe file: {exc}", flush=True)

    target_resolution = override_target_resolution if override_target_resolution is not None else live_settings.get("target_resolution", "copy")

    print(f"[CONVERT] Settings: encoder={encoder}, preset={nvenc_preset}, cq={cq}, crf={crf}, audio={audio_codec}, resolution={target_resolution}", flush=True)

    cmd = build_ffmpeg_cmd(input_path, temp_path, encoder=encoder, nvenc_preset=nvenc_preset, cq=cq, crf=crf, audio_codec=audio_codec, audio_bitrate=audio_bitrate, lossless_conversion=lossless_conversion, audio_stream_codecs=audio_stream_codecs, target_resolution=target_resolution, subtitle_streams=subtitle_streams)

    # Append custom ffmpeg flags if configured
    custom_flags = live_settings.get("custom_ffmpeg_flags", "")
    if custom_flags.strip():
        # Insert custom flags before the output path (last element)
        import shlex
        extra = shlex.split(custom_flags)
        cmd = cmd[:-1] + extra + cmd[-1:]
        print(f"[CONVERT] Custom ffmpeg flags: {custom_flags}", flush=True)

    # During quiet hours, lower process priority
    if nice:
        cmd = ["nice", "-n", "15", "ionice", "-c", "3"] + cmd
        print(f"[CONVERT] Quiet hours: using nice/ionice", flush=True)

    full_command = " ".join(cmd)
    print(f"[CONVERT] ffmpeg cmd: {' '.join(cmd[:6])} ...", flush=True)

    # VMAF: store original path so we can compare after encoding (if backup keeps it)
    _vmaf_setting = live_settings.get("vmaf_analysis_enabled", "true")
    vmaf_enabled = _vmaf_setting if isinstance(_vmaf_setting, bool) else str(_vmaf_setting).lower() == "true"
    vmaf_original_path = input_path if vmaf_enabled else None
    vmaf_seek = max(0, duration * 0.33) if duration > 30 else 0
    vmaf_duration = min(30, duration - vmaf_seek) if duration > 0 else 30

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        print(f"[CONVERT] ffmpeg started, pid={proc.pid}", flush=True)
        if proc_callback:
            proc_callback(proc)

        # ffmpeg writes progress using \r (carriage return), not \n.
        # Read in small chunks and split on \r to parse progress lines.
        encode_start_time = time.monotonic()
        buffer = ""
        all_lines: list[str] = []  # Full log for conversion history
        last_lines: list[str] = []  # Last N for error reporting
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
                if line:
                    # Keep non-progress lines for the full log (skip repetitive progress spam)
                    if not line.startswith("frame=") and not line.startswith("size="):
                        all_lines.append(line)
                    last_lines.append(line)
                    if len(last_lines) > 20:
                        last_lines.pop(0)
                if progress_callback and line:
                    parsed = parse_ffmpeg_progress(line, duration, start_time=encode_start_time)
                    if parsed:
                        await progress_callback(**parsed)

        await asyncio.wait_for(proc.wait(), timeout=live_settings.get("ffmpeg_timeout", 21600))

        if proc.returncode != 0:
            # Clean up temp file if it exists
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            # Extract meaningful error from ffmpeg output
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

    # Sanity check: output suspiciously small (< 5% of original) = likely corrupt
    min_expected = int(original_size * 0.05)
    if output_size < min_expected and original_size > 10 * 1024 * 1024:  # Only for files > 10MB
        print(f"[CONVERT] Output ({output_size} bytes) is suspiciously small vs original ({original_size} bytes) — likely corrupt, keeping original", flush=True)
        try:
            temp.unlink()
        except OSError:
            pass
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": f"Output file suspiciously small ({output_size} bytes vs {original_size} bytes original) — likely corrupt. Original file preserved.",
        }

    # If the converted file is LARGER than the original, discard it and keep original
    if space_saved < 0:
        print(f"[CONVERT] Output ({output_size}) is LARGER than original ({original_size}), keeping original", flush=True)
        try:
            temp.unlink()
        except OSError:
            pass
        return {
            "success": True,  # Not an error, just no savings
            "output_path": input_path,  # Keep original path
            "space_saved": 0,
            "error": None,
            "skipped_larger": True,
        }

    # VMAF analysis — compare original vs encoded BEFORE the original is moved/deleted
    vmaf_score = None
    if vmaf_enabled:
        try:
            from backend.test_encode import check_vmaf_available
            if await check_vmaf_available():
                vmaf_dir = Path("/tmp/squeezarr_vmaf")
                vmaf_dir.mkdir(parents=True, exist_ok=True)
                _vmaf_id = Path(input_path).stem[:20]
                vmaf_json_path = vmaf_dir / f"{_vmaf_id}_vmaf.json"

                # Use -ss AFTER -i for frame-accurate decode seeking (not keyframe-based)
                # Both files have the same content at the same timestamps
                vmaf_cmd = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", input_path, "-i", temp_path,
                    "-filter_complex",
                    f"[0:v]trim=start={vmaf_seek}:duration={vmaf_duration},setpts=PTS-STARTPTS[ref];"
                    f"[1:v]trim=start={vmaf_seek}:duration={vmaf_duration},setpts=PTS-STARTPTS[dist];"
                    f"[dist][ref]libvmaf=model=version=vmaf_v0.6.1:log_fmt=json:log_path={vmaf_json_path}",
                    "-f", "null", "-",
                ]
                print(f"[CONVERT] Running VMAF analysis ({vmaf_duration:.0f}s sample at {vmaf_seek:.0f}s)...", flush=True)
                # Signal the UI that we're analyzing quality
                if progress_callback:
                    await progress_callback(progress=100, fps=0, eta_seconds=0, step="vmaf analysis")
                # Estimate total frames for progress tracking
                # Use ~24fps as a reasonable default for the sample duration
                vmaf_total_frames = max(1, int(vmaf_duration * 24))

                vmaf_proc = await asyncio.create_subprocess_exec(
                    *vmaf_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Parse stderr for frame progress
                vmaf_buf = ""
                vmaf_err_lines = []
                while True:
                    chunk = await vmaf_proc.stderr.read(4096)
                    if not chunk:
                        break
                    vmaf_buf += chunk.decode(errors="replace")
                    while "\r" in vmaf_buf or "\n" in vmaf_buf:
                        r_pos = vmaf_buf.find("\r")
                        n_pos = vmaf_buf.find("\n")
                        if r_pos == -1: pos = n_pos
                        elif n_pos == -1: pos = r_pos
                        else: pos = min(r_pos, n_pos)
                        line = vmaf_buf[:pos].strip()
                        vmaf_buf = vmaf_buf[pos + 1:]
                        if line:
                            vmaf_err_lines.append(line)
                            # Parse "frame= 123" from ffmpeg progress
                            import re as _re
                            fm = _re.search(r'frame=\s*(\d+)', line)
                            if fm and progress_callback:
                                frame = int(fm.group(1))
                                pct = min(99, frame / vmaf_total_frames * 100)
                                await progress_callback(progress=pct, fps=0, eta_seconds=0, step="vmaf analysis")

                await asyncio.wait_for(vmaf_proc.wait(), timeout=300)
                if vmaf_proc.returncode != 0:
                    print(f"[CONVERT] VMAF failed (rc={vmaf_proc.returncode}): {''.join(vmaf_err_lines[-5:])}", flush=True)
                elif vmaf_json_path.exists():
                    import json as _vjson
                    vdata = _vjson.loads(vmaf_json_path.read_text())
                    vs = vdata.get("pooled_metrics", {}).get("vmaf", {}).get("mean")
                    if vs is not None:
                        vmaf_score = round(vs, 1)
                        print(f"[CONVERT] VMAF score: {vmaf_score}", flush=True)
                    vmaf_json_path.unlink(missing_ok=True)
            else:
                print(f"[CONVERT] VMAF skipped — libvmaf not available", flush=True)
        except Exception as vmaf_exc:
            print(f"[CONVERT] VMAF analysis failed: {vmaf_exc}", flush=True)

    # Handle original file: backup, trash, or delete
    try:
        backup_days = live_settings.get("backup_original_days", 0)
        use_trash = live_settings.get("trash_original_after_conversion", False)

        result_backup_path = None
        if backup_days and backup_days > 0:
            # Move original to backup folder (custom or .squeezarr_backup in same dir)
            custom_backup = live_settings.get("backup_folder", "")
            if custom_backup:
                # Centralized backup: preserve relative path structure
                backup_dir = Path(custom_backup)
                # Create a subdirectory mirroring the parent folder name
                backup_dir = backup_dir / p.parent.name
                backup_dir.mkdir(parents=True, exist_ok=True)
            else:
                backup_dir = p.parent / ".squeezarr_backup"
                backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / p.name
            p.rename(backup_path)
            result_backup_path = str(backup_path)
            print(f"[CONVERT] Original backed up to: {backup_path}", flush=True)
        elif use_trash:
            try:
                from send2trash import send2trash
                send2trash(str(p))
                print(f"[CONVERT] Original moved to trash: {p.name}", flush=True)
            except Exception as trash_exc:
                print(f"[CONVERT] Trash failed ({trash_exc}), falling back to permanent delete", flush=True)
                p.unlink()
        else:
            p.unlink()
        temp.rename(final_path)
    except OSError as exc:
        return {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}

    # Rename external subtitle files to match the new filename
    final_stem = Path(final_path).stem
    rename_external_subtitles(input_path, final_stem)

    encode_time = time.monotonic() - encode_start_time
    return {
        "success": True,
        "output_path": final_path,
        "space_saved": space_saved,
        "error": None,
        "backup_path": result_backup_path,
        "vmaf_score": vmaf_score,
        "ffmpeg_command": full_command,
        "ffmpeg_log": "\n".join(all_lines[-500:]),  # Cap at 500 lines
        "encoding_stats": {
            "encoder": encoder,
            "preset": nvenc_preset,
            "cq": cq,
            "crf": crf,
            "audio_codec": audio_codec,
            "audio_bitrate": audio_bitrate,
            "target_resolution": target_resolution,
            "input_size": original_size,
            "output_size": output_size,
            "ratio": round((1 - output_size / original_size) * 100, 1) if original_size > 0 else 0,
            "encode_seconds": round(encode_time, 1),
            "duration": duration,
            "input_bitrate": round(original_size * 8 / duration / 1_000_000, 2) if duration > 0 else None,
            "output_bitrate": round(output_size * 8 / duration / 1_000_000, 2) if duration > 0 else None,
        },
    }
