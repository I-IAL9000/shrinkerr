import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("shrinkerr.converter")


def _str_to_bool(v) -> bool:
    """Coerce a settings-table string to bool. Strings are stored in the DB
    as lowercase 'true' / 'false'; anything else falls through to False."""
    return str(v).lower() == "true"


# ─────────────────────────────────────────────────────────────────────────
# Single source of truth for encoding-related settings the converter reads
# at encode time. Each entry is `(key, default_if_absent, coerce_fn)`.
#
# `default_if_absent` semantics:
#   - A concrete value (int, str, bool, float) → key is ALWAYS present in
#     the returned dict; the default is used when the DB row is missing.
#   - `_ABSENT` sentinel → key is only present in the returned dict when the
#     DB actually has it, matching the old behavior where callers did
#     `live.get("foo", their_own_default)`. Avoids changing existing
#     caller assumptions about when a key will be `None` vs missing.
#
# Adding a new setting? One line here and it flows end-to-end to the
# encoder. No risk of the "setting saved to DB but never read" class of bug
# that bit vmaf_min_score (v0.3.1 fix) and had been lurking before that.
# ─────────────────────────────────────────────────────────────────────────
_ABSENT: object = object()

_ENCODING_SETTINGS: tuple[tuple[str, object, Callable], ...] = (
    # Encoder selection + quality
    ("default_encoder",                  "nvenc",   str),
    ("nvenc_cq",                         20,        int),
    ("libx265_crf",                      20,        int),
    ("nvenc_preset",                     "p6",      str),
    ("libx265_preset",                   "medium",  str),
    # Process limits
    ("ffmpeg_timeout",                   21600,     int),
    ("ffprobe_timeout",                  30,        int),
    # Audio
    ("audio_codec",                      "copy",    str),
    ("audio_bitrate",                    128,       int),
    ("auto_convert_lossless",            _ABSENT,   _str_to_bool),
    ("lossless_target_codec",            _ABSENT,   str),
    ("lossless_target_bitrate",          _ABSENT,   int),
    # Output shaping
    ("target_resolution",                _ABSENT,   str),
    ("custom_ffmpeg_flags",              _ABSENT,   str),
    ("filename_suffix",                  _ABSENT,   str),
    # Post-conversion
    ("trash_original_after_conversion",  _ABSENT,   _str_to_bool),
    ("backup_original_days",             _ABSENT,   int),
    ("backup_folder",                    _ABSENT,   str),
    # VMAF
    ("vmaf_analysis_enabled",            _ABSENT,   _str_to_bool),
    ("vmaf_min_score",                   _ABSENT,   float),
)


def _apply_coercion(raw, coerce: Callable, fallback):
    """Coerce a raw DB string through `coerce`, falling back to `fallback`
    on any type error. Isolates the per-row try/except so the settings
    loader stays readable."""
    try:
        return coerce(raw)
    except (TypeError, ValueError):
        return fallback


async def get_live_encoding_settings() -> dict:
    """Read encoding settings from the DB at call time (not the frozen config singleton).

    Returns a dict keyed by setting name, with values coerced to the right
    Python type per _ENCODING_SETTINGS above. On DB errors, falls back to
    the hard-coded defaults (the ones with concrete default values; _ABSENT
    entries are simply omitted).
    """
    import aiosqlite
    from backend.database import DB_PATH

    # Base result = the concrete-default keys. _ABSENT entries are skipped.
    result: dict = {key: default for key, default, _ in _ENCODING_SETTINGS if default is not _ABSENT}

    try:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT key, value FROM settings") as cur:
                db_settings = {r["key"]: r["value"] for r in await cur.fetchall()}
        finally:
            await db.close()
    except Exception as exc:
        print(f"[CONVERT] Failed to read DB settings, using defaults: {exc}", flush=True)
        return result

    for key, default, coerce in _ENCODING_SETTINGS:
        if key not in db_settings:
            continue  # absent in DB → leave concrete default in place or skip (for _ABSENT keys)
        coerced = _apply_coercion(db_settings[key], coerce, default if default is not _ABSENT else None)
        if coerced is None and default is _ABSENT:
            continue  # malformed value on an optional key → don't introduce a None
        result[key] = coerced
    return result


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
    libx265_preset: str = "medium",
    audio_codec: str = "copy",
    audio_bitrate: int = 128,
    lossless_conversion: dict | None = None,
    audio_stream_codecs: list[str] | None = None,
    target_resolution: str = "copy",
    subtitle_streams: list[dict] | None = None,
    # NEW: inline track removal. When provided, these override the default "-map 0:a"
    # and are mapped explicitly by source stream index so the output contains exactly
    # the user's desired tracks — no separate remux pass needed.
    # audio_streams_to_keep: list of dicts with {stream_index, codec, profile} in OUTPUT order
    # subtitle_streams_to_remove: set/list of source stream indices to exclude
) -> list[str]:
    """Build an ffmpeg command list for converting a file to HEVC.

    lossless_conversion: if set, dict with 'codec' and 'bitrate' for lossless audio streams.
    audio_stream_codecs: list of codec names per audio stream (from ffprobe), needed for per-stream lossless conversion.
    target_resolution: "copy", "1080p", "720p", or "480p".
    """
    return _build_ffmpeg_cmd_impl(
        input_path, output_path, encoder=encoder, cq=cq, crf=crf,
        nvenc_preset=nvenc_preset, libx265_preset=libx265_preset,
        audio_codec=audio_codec, audio_bitrate=audio_bitrate,
        lossless_conversion=lossless_conversion,
        audio_stream_codecs=audio_stream_codecs,
        target_resolution=target_resolution,
        subtitle_streams=subtitle_streams,
        audio_streams_to_keep=None,
        subtitle_streams_to_remove=None,
    )


def _build_ffmpeg_cmd_impl(
    input_path: str,
    output_path: str,
    encoder: str = "nvenc",
    cq: int = 20,
    crf: int = 20,
    nvenc_preset: str = "p6",
    libx265_preset: str = "medium",
    audio_codec: str = "copy",
    audio_bitrate: int = 128,
    lossless_conversion: dict | None = None,
    audio_stream_codecs: list[str] | None = None,
    target_resolution: str = "copy",
    subtitle_streams: list[dict] | None = None,
    audio_streams_to_keep: list[dict] | None = None,
    # External subtitle files to merge into the output.
    # Each dict: {path, codec, language, forced}
    external_subtitle_files: list[dict] | None = None,
    subtitle_streams_to_remove: set | None = None,
) -> list[str]:
    cmd = ["ffmpeg", "-y", "-i", input_path]

    # Add external subtitle files as additional inputs (input 1, 2, 3, ...)
    ext_subs = external_subtitle_files or []
    for es in ext_subs:
        cmd += ["-i", es["path"]]

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
            "-preset", libx265_preset,
            "-crf", str(crf),
            "-profile:v", "main10",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:rd=4:psy-rd=2.0",
        ]

    # Map ONLY the first video stream (0:v:0) — NOT all video streams.
    # Some files have cover art (PNG/JPEG attached_pic) registered as extra video streams.
    # Using "-map 0:v" maps ALL of them, causing ffmpeg to re-encode the cover as HEVC,
    # which corrupts the output stream layout and confuses players like Sonarr/Plex.
    cmd += ["-map", "0:v:0"]

    # Audio mapping + codec args
    # Two paths:
    #   (a) Explicit keep-list (inline track removal): map each kept audio stream by
    #       source stream_index, and set per-stream codec based on source codec+profile.
    #   (b) Default: map all audio streams, apply global codec logic.
    if audio_streams_to_keep is not None:
        # Explicit audio streams — these came from user selection (+ native-first reorder)
        target_lossless_codec = (lossless_conversion or {}).get("codec")
        target_lossless_bitrate = (lossless_conversion or {}).get("bitrate")
        for out_idx, track in enumerate(audio_streams_to_keep):
            src_idx = track.get("stream_index")
            cmd += ["-map", f"0:{src_idx}"]
            src_codec = (track.get("codec") or "").lower()
            src_profile = (track.get("profile") or "")
            if target_lossless_codec and is_lossless_audio(src_codec, src_profile):
                cmd += [f"-c:a:{out_idx}"] + _audio_codec_args(target_lossless_codec, target_lossless_bitrate)
            else:
                cmd += [f"-c:a:{out_idx}"] + _audio_codec_args(audio_codec, audio_bitrate)
    else:
        # Default path: all audio streams
        cmd += ["-map", "0:a"]
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
                    args = _audio_codec_args(audio_codec, audio_bitrate)
                    cmd += [f"-c:a:{idx}"] + args
        else:
            args = _audio_codec_args(audio_codec, audio_bitrate)
            cmd += ["-c:a"] + args

    # Map subtitle streams. Matroska accepts many text/image codecs as-is (copy),
    # but some codecs (notably mp4's `mov_text`) need to be transcoded to a
    # matroska-friendly format (srt) or the mux will fail.
    # Text subs that can be copied directly into mkv:
    COPYABLE_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt"}
    # Text subs that need conversion to srt (mkv can't copy these as-is):
    CONVERTIBLE_TEXT_SUBS = {"mov_text", "tx3g"}
    # Image-based subs that copy cleanly to mkv:
    IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "hdmv_text_subtitle", "pgs", "vobsub"}
    SUPPORTED_SUB_CODECS = COPYABLE_TEXT_SUBS | CONVERTIBLE_TEXT_SUBS | IMAGE_SUBS

    to_remove = set(subtitle_streams_to_remove or [])
    sub_codec_args: list[str] = []  # per-output-stream codec args, appended after maps
    mapped_any_sub = False
    out_sub_idx = 0
    if subtitle_streams:
        for sub in subtitle_streams:
            codec = (sub.get("codec_name") or "").lower()
            idx = sub.get("index")
            if idx is None:
                continue
            if idx in to_remove:
                continue  # user asked to remove this subtitle track
            if codec not in SUPPORTED_SUB_CODECS:
                print(f"[CONVERT] Skipping unsupported subtitle stream #{idx} codec={codec or 'unknown'}", flush=True)
                continue
            cmd += ["-map", f"0:{idx}"]
            if codec in CONVERTIBLE_TEXT_SUBS:
                sub_codec_args += [f"-c:s:{out_sub_idx}", "srt"]
            else:
                sub_codec_args += [f"-c:s:{out_sub_idx}", "copy"]
            mapped_any_sub = True
            out_sub_idx += 1
        if mapped_any_sub:
            cmd += sub_codec_args
    else:
        # No subtitle info available — skip subs rather than risk unsupported codecs
        print("[CONVERT] No subtitle stream info from probe — skipping subtitle mapping", flush=True)

    # Map external subtitle files (additional inputs)
    # These come after embedded subs, and need per-stream codec + metadata
    if ext_subs:
        for i, es in enumerate(ext_subs):
            input_idx = i + 1  # input 0 is the video, 1+ are external subs
            cmd += ["-map", f"{input_idx}:s"]
            codec = (es.get("codec") or "subrip").lower()
            # For text subs going into mkv: copy if natively supported, else convert to srt
            if codec in ("subrip", "srt"):
                cmd += [f"-c:s:{out_sub_idx}", "srt"]
            elif codec in ("ass", "ssa"):
                cmd += [f"-c:s:{out_sub_idx}", "copy"]
            elif codec in ("webvtt",):
                cmd += [f"-c:s:{out_sub_idx}", "srt"]
            else:
                cmd += [f"-c:s:{out_sub_idx}", "copy"]
            # Set language metadata
            lang = es.get("language") or "und"
            cmd += [f"-metadata:s:s:{out_sub_idx}", f"language={lang}"]
            # Set forced disposition
            if es.get("forced"):
                cmd += [f"-disposition:s:{out_sub_idx}", "forced"]
            out_sub_idx += 1
        print(f"[CONVERT] Merging {len(ext_subs)} external subtitle(s)", flush=True)

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
    override_libx265_preset: Optional[str] = None,
    override_target_resolution: Optional[str] = None,
    nice: bool = False,
    pre_settings: Optional[dict] = None,
    # Inline track removal — when passed, tracks in these sets are EXCLUDED from
    # the output in the same ffmpeg pass as the video conversion. Avoids a second
    # remux pass whose stream indices wouldn't match the converted file.
    audio_tracks_to_remove: Optional[list] = None,
    subtitle_tracks_to_remove: Optional[list] = None,
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

    # Read live settings from DB — or use pre_settings if provided (worker mode, no local DB)
    if pre_settings is not None:
        live_settings = pre_settings
    else:
        live_settings = await get_live_encoding_settings()
    filename_suffix = live_settings.get("filename_suffix", "")
    final_path = get_output_path(input_path, suffix=filename_suffix)
    nvenc_preset = override_preset if override_preset is not None else live_settings.get("nvenc_preset", "p6")
    libx265_preset = override_libx265_preset if override_libx265_preset is not None else live_settings.get("libx265_preset", "medium")
    cq = override_cq if override_cq is not None else live_settings.get("nvenc_cq", 20)
    crf = override_crf if override_crf is not None else live_settings.get("libx265_crf", 20)
    audio_codec = override_audio_codec if override_audio_codec is not None else live_settings.get("audio_codec", "copy")
    audio_bitrate = override_audio_bitrate if override_audio_bitrate is not None else live_settings.get("audio_bitrate", 128)

    # Probe file for audio/subtitle stream details
    lossless_conversion = None
    audio_stream_codecs = None
    subtitle_streams = None
    audio_streams_to_keep: Optional[list] = None  # inline keep-list (if tracks_to_remove given)
    probe_audio_tracks: list = []
    try:
        from backend.scanner import probe_file
        probe_data = await probe_file(input_path)
        if probe_data:
            # Subtitle streams for safe mapping (skip unsupported codecs)
            # Map probe format to what build_ffmpeg_cmd expects
            raw_subs = probe_data.get("subtitle_tracks", [])
            subtitle_streams = [{"codec_name": s.get("codec", ""), "index": s.get("stream_index")} for s in raw_subs]

            probe_audio_tracks = probe_data.get("audio_tracks") or []

            # Lossless audio auto-conversion
            if live_settings.get("auto_convert_lossless", False) and probe_audio_tracks:
                target_codec = live_settings.get("lossless_target_codec", "eac3")
                target_bitrate = live_settings.get("lossless_target_bitrate", 640)
                audio_stream_codecs = [t.get("codec", "unknown") for t in probe_audio_tracks]
                audio_stream_profiles = [t.get("profile", "") for t in probe_audio_tracks]
                has_lossless = any(is_lossless_audio(c, p) for c, p in zip(audio_stream_codecs, audio_stream_profiles))
                if has_lossless:
                    lossless_conversion = {"codec": target_codec, "bitrate": target_bitrate, "profiles": audio_stream_profiles}
                    lossless_names = [c for c, p in zip(audio_stream_codecs, audio_stream_profiles) if is_lossless_audio(c, p)]
                    print(f"[CONVERT] Lossless audio detected ({', '.join(lossless_names)}), converting to {target_codec} {target_bitrate}k", flush=True)
    except Exception as exc:
        print(f"[CONVERT] Failed to probe file: {exc}", flush=True)

    # Build inline keep-list for audio:
    #   - Filter out any tracks in audio_tracks_to_remove
    #   - Reorder so native-language tracks come first (default playback track)
    # This runs even when no tracks are being removed so every conversion produces
    # a file with the native-language track on stream 1.
    if probe_audio_tracks:
        remove_set = set(audio_tracks_to_remove or [])
        kept = [t for t in probe_audio_tracks if t.get("stream_index") not in remove_set]

        # Determine native language — prefer the scan_results value (populated from
        # TMDB/Sonarr API, more reliable than track ordering in the file).
        native_lang = None
        try:
            import aiosqlite as _aiosqlite
            from backend.database import DB_PATH as _DB_PATH
            db_nl = await _aiosqlite.connect(_DB_PATH)
            db_nl.row_factory = _aiosqlite.Row
            try:
                async with db_nl.execute(
                    "SELECT native_language FROM scan_results WHERE file_path = ?",
                    (input_path,),
                ) as cur:
                    row = await cur.fetchone()
                if row and row["native_language"]:
                    native_lang = row["native_language"]
            finally:
                await db_nl.close()
        except Exception:
            pass
        # Fall back to track-based detection only if scan_results didn't have it
        if not native_lang:
            try:
                from backend.scanner import detect_native_language
                native_lang = detect_native_language(probe_audio_tracks)
            except Exception:
                native_lang = None

        # Reorder: native-language tracks first (if enabled in settings)
        try:
            from backend.scanner import languages_match, _is_cleanup_enabled
            if _is_cleanup_enabled("reorder_native_audio") and native_lang and native_lang.lower() != "und":
                native = [t for t in kept if languages_match((t.get("language") or "").lower(), native_lang.lower())]
                others = [t for t in kept if t not in native]
                if native and (not kept or native[0] is not kept[0]):
                    kept = native + others
                    print(f"[CONVERT] Reordered audio: native ({native_lang}) tracks first", flush=True)
        except Exception:
            pass

        # Only set the inline keep-list if we're actually changing something
        # (removing tracks or reordering). Otherwise fall through to the default
        # "-map 0:a" path to avoid no-op ffmpeg complexity.
        order_changed = [t.get("stream_index") for t in kept] != [t.get("stream_index") for t in probe_audio_tracks]
        if kept and (remove_set or order_changed):
            audio_streams_to_keep = kept
        elif not kept:
            print("[CONVERT] Warning: audio_tracks_to_remove would drop ALL tracks — ignoring", flush=True)

    target_resolution = override_target_resolution if override_target_resolution is not None else live_settings.get("target_resolution", "copy")

    active_preset = libx265_preset if encoder == "libx265" else nvenc_preset
    active_quality = f"crf={crf}" if encoder == "libx265" else f"cq={cq}"
    print(f"[CONVERT] Settings: encoder={encoder}, preset={active_preset}, {active_quality}, audio={audio_codec}, resolution={target_resolution}", flush=True)
    if audio_streams_to_keep is not None:
        removed_count = len(probe_audio_tracks) - len(audio_streams_to_keep)
        print(f"[CONVERT] Inline audio removal: keeping {len(audio_streams_to_keep)} of {len(probe_audio_tracks)} ({removed_count} removed)", flush=True)

    sub_remove_set = set(subtitle_tracks_to_remove or [])
    if sub_remove_set:
        print(f"[CONVERT] Inline subtitle removal: {len(sub_remove_set)} track(s) to drop", flush=True)

    # Load external subtitle files to merge (if the setting is enabled)
    external_sub_files: list[dict] | None = None
    try:
        from backend.scanner import _is_cleanup_enabled
        if _is_cleanup_enabled("merge_external_subs"):
            import aiosqlite as _aiosqlite
            from backend.database import DB_PATH as _DB_PATH
            db_es = await _aiosqlite.connect(_DB_PATH)
            db_es.row_factory = _aiosqlite.Row
            try:
                async with db_es.execute(
                    "SELECT subtitle_tracks_json FROM scan_results WHERE file_path = ?",
                    (input_path,),
                ) as cur:
                    row_es = await cur.fetchone()
                if row_es and row_es["subtitle_tracks_json"]:
                    import json as _json
                    all_sub_tracks = _json.loads(row_es["subtitle_tracks_json"])
                    ext_subs_to_merge = [
                        t for t in all_sub_tracks
                        if t.get("external") and t.get("keep", True) and t.get("external_path")
                    ]
                    if ext_subs_to_merge:
                        external_sub_files = [
                            {"path": t["external_path"], "codec": t.get("codec", "subrip"),
                             "language": t.get("language", "und"), "forced": t.get("forced", False)}
                            for t in ext_subs_to_merge
                            if os.path.exists(t["external_path"])
                        ]
                        if external_sub_files:
                            print(f"[CONVERT] Will merge {len(external_sub_files)} external subtitle file(s)", flush=True)
            finally:
                await db_es.close()
    except Exception as exc:
        print(f"[CONVERT] External sub loading failed (non-fatal): {exc}", flush=True)

    cmd = _build_ffmpeg_cmd_impl(
        input_path, temp_path, encoder=encoder,
        nvenc_preset=nvenc_preset, libx265_preset=libx265_preset,
        cq=cq, crf=crf, audio_codec=audio_codec, audio_bitrate=audio_bitrate,
        lossless_conversion=lossless_conversion,
        audio_stream_codecs=audio_stream_codecs,
        target_resolution=target_resolution,
        subtitle_streams=subtitle_streams,
        audio_streams_to_keep=audio_streams_to_keep,
        subtitle_streams_to_remove=sub_remove_set if sub_remove_set else None,
        external_subtitle_files=external_sub_files,
    )

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

    # Verify output exists and has non-zero size.
    #
    # Retry with a short wait: on networked filesystems (NFS, SMB) and under
    # heavy I/O load we've occasionally seen the stat() fire a hair before
    # ffmpeg's final flush finishes propagating the file size back to the
    # client. Also re-scan the directory for any *.converting.mkv file —
    # rarely, ffmpeg ends up using a slightly different path if the stem
    # contains unusual characters. Only then give up, and include enough
    # diagnostic detail for the user to see WHY we gave up.
    temp = Path(temp_path)

    async def _resolve_output() -> tuple[Path | None, str]:
        # 1. Happy path — the expected temp path exists with content.
        if temp.exists() and temp.stat().st_size > 0:
            return temp, "expected path, first check"
        # 2. Short wait then re-check. 3x500ms covers NFS write latency without
        # unreasonably delaying the common case.
        for attempt in range(3):
            await asyncio.sleep(0.5)
            if temp.exists() and temp.stat().st_size > 0:
                return temp, f"expected path after {(attempt + 1) * 500}ms wait"
        # 3. Did the final (renamed) output already appear? This can happen
        # if a previous run completed the rename but we mis-tracked state.
        final = Path(final_path)
        if final.exists() and final.stat().st_size > 0:
            return final, "already-renamed final path"
        # 4. Scan the parent directory for any .converting.mkv file younger
        # than when we started — ffmpeg might have written to a nearby path.
        try:
            parent = temp.parent
            candidates = []
            for f in parent.glob("*.converting.mkv"):
                try:
                    st = f.stat()
                    if st.st_size > 0:
                        candidates.append((f, st.st_size, st.st_mtime))
                except OSError:
                    continue
            if candidates:
                # Pick the most-recently-modified one.
                candidates.sort(key=lambda x: x[2], reverse=True)
                return candidates[0][0], f"recovered via directory scan ({candidates[0][0].name})"
        except Exception:
            pass
        return None, "no output file found"

    resolved, how = await _resolve_output()
    if resolved is None:
        # Build a diagnostic snapshot so the next failure is debuggable.
        try:
            parent = temp.parent
            dir_listing = sorted([f.name for f in parent.iterdir()])[:25]
        except Exception:
            dir_listing = ["<unable to list directory>"]
        input_exists = Path(input_path).exists()
        input_size = Path(input_path).stat().st_size if input_exists else 0
        diag = (
            f"Output file missing or empty after conversion.\n"
            f"- expected temp: {temp_path}\n"
            f"- expected final: {final_path}\n"
            f"- ffmpeg exit code: 0 (reported success)\n"
            f"- source file intact: {input_exists} ({input_size} bytes)\n"
            f"- nearby files: {dir_listing}\n"
            f"Source file was NOT touched — you can safely retry."
        )
        print(f"[CONVERT] {diag}", flush=True)
        return {
            "success": False,
            "output_path": None,
            "space_saved": 0,
            "error": diag,
            "source_intact": input_exists,
        }

    # Use the resolved path (may differ from `temp_path` if recovery kicked in)
    if str(resolved) != temp_path:
        print(f"[CONVERT] Output resolved via fallback: {how} → {resolved}", flush=True)
        temp_path = str(resolved)
        temp = resolved

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

    # If the converted file is LARGER than the original, discard it and keep original.
    # EXCEPTION: if we did inline track removal (audio/subtitle), keep the output even
    # if the video codec conversion alone was a loss — the user explicitly asked for
    # those tracks to be removed, and they're already gone from the output file.
    # Discarding would lose the track removal work and leave unwanted tracks in place.
    had_track_removal = bool(audio_tracks_to_remove) or bool(subtitle_tracks_to_remove) or bool(external_sub_files)
    if space_saved < 0 and not had_track_removal:
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
    if space_saved < 0 and had_track_removal:
        print(f"[CONVERT] Output is larger but keeping it — tracks were removed inline ({abs(space_saved)} bytes growth, but unwanted tracks are gone)", flush=True)
        space_saved = 0  # Don't report negative savings

    # VMAF analysis — compare original vs encoded BEFORE the original is moved/deleted
    vmaf_score = None
    if vmaf_enabled:
        try:
            from backend.test_encode import check_vmaf_available
            if await check_vmaf_available():
                vmaf_dir = Path("/tmp/shrinkerr_vmaf")
                vmaf_dir.mkdir(parents=True, exist_ok=True)
                # Unique per-job filename. Previously we used `stem[:20]`,
                # which collided whenever two concurrent jobs' filenames
                # shared a 20-char prefix (same-series TV episodes, same
                # movie franchise, etc.) — the collision meant one of the
                # two libvmaf outputs clobbered the other, and the loser
                # recorded no VMAF score. The stem prefix is preserved for
                # human-debuggable leftover-file names in /tmp; the uuid
                # suffix guarantees collision-free concurrent writes.
                import uuid as _uuid
                _vmaf_id = f"{Path(input_path).stem[:20]}_{_uuid.uuid4().hex[:8]}"
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

    # ------------------------------------------------------------------
    # VMAF threshold enforcement: if the user configured a minimum acceptable
    # VMAF score and this encode didn't clear it, reject the output and keep
    # the original in place. We only apply the threshold when we actually
    # have a score — a failed/unavailable VMAF run is NOT grounds for
    # rejection (treated the same as threshold=0).
    # ------------------------------------------------------------------
    try:
        vmaf_min_raw = live_settings.get("vmaf_min_score", 0) or 0
        vmaf_min_score = float(vmaf_min_raw)
    except (TypeError, ValueError):
        vmaf_min_score = 0.0

    vmaf_rejected = False
    vmaf_reject_reason = None
    if vmaf_score is not None and vmaf_min_score > 0 and vmaf_score < vmaf_min_score:
        vmaf_rejected = True
        vmaf_reject_reason = (
            f"VMAF {vmaf_score} is below the configured minimum of "
            f"{vmaf_min_score:g} — encode rejected, original kept."
        )
        print(f"[CONVERT] {vmaf_reject_reason}", flush=True)
        # Delete the encoded temp file so the user doesn't end up with a
        # stray low-quality copy sitting next to the original.
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError as unlink_exc:
            print(
                f"[CONVERT] Failed to delete rejected temp file {temp_path}: {unlink_exc}",
                flush=True,
            )
        # No subtitle renames to undo — those only happen on the success
        # path after the original is replaced, and we bail before that.
        return {
            "success": True,              # the encode process worked; we just didn't accept the output
            "output_path": input_path,    # original untouched
            "space_saved": 0,
            "error": None,
            "vmaf_score": vmaf_score,
            "vmaf_rejected": True,
            "vmaf_reject_reason": vmaf_reject_reason,
            "vmaf_min_score": vmaf_min_score,
            "ffmpeg_command": full_command,
            "ffmpeg_log": "\n".join(all_lines[-500:]),
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
                "encode_seconds": time.monotonic() - encode_start_time,
            },
        }

    # Handle original file: backup, trash, or delete
    try:
        backup_days = live_settings.get("backup_original_days", 0)
        use_trash = live_settings.get("trash_original_after_conversion", False)

        result_backup_path = None
        if backup_days and backup_days > 0:
            # Move original to backup folder (custom or .shrinkerr_backup in same dir)
            custom_backup = live_settings.get("backup_folder", "")
            if custom_backup:
                # Centralized backup: preserve relative path structure
                backup_dir = Path(custom_backup)
                # Create a subdirectory mirroring the parent folder name
                backup_dir = backup_dir / p.parent.name
                backup_dir.mkdir(parents=True, exist_ok=True)
            else:
                # New per-directory backup folder. If the user already has an
                # old .squeezarr_backup folder from a previous install, keep
                # writing to that one so their existing backups stay in a
                # single location until they move/clean it up themselves.
                legacy = p.parent / ".squeezarr_backup"
                backup_dir = legacy if legacy.exists() else (p.parent / ".shrinkerr_backup")
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

    # Handle external subtitle files after successful conversion
    _should_delete_ext_subs = False
    if external_sub_files:
        try:
            from backend.scanner import _is_cleanup_enabled as _ice
            _should_delete_ext_subs = _ice("delete_external_subs_after_merge")
        except Exception:
            pass
    if external_sub_files and _should_delete_ext_subs:
        # Delete external subs that were merged into the output
        for es in external_sub_files:
            try:
                p = Path(es["path"])
                if p.exists():
                    p.unlink()
                    print(f"[CONVERT] Deleted merged external sub: {p.name}", flush=True)
            except Exception as exc:
                print(f"[CONVERT] Failed to delete external sub {es['path']}: {exc}", flush=True)

    # Rename remaining external subtitle files to match the new filename
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
