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
    # Intel QSV (hevc_qsv) — uses ICQ-style global_quality (lower = better,
    # ~similar range to NVENC's CQ). Preset names match NVENC's veryslow…
    # veryfast ladder. v0.3.67+.
    ("qsv_cq",                           22,        int),
    ("qsv_preset",                       "medium",  str),
    # `look_ahead` enables QSV's frame-lookahead rate control. Slight
    # quality bump at the cost of throughput (often 10-20% slower).
    # Off by default — opt-in for users who want quality > speed.
    # v0.3.93+.
    ("qsv_lookahead",                    False,     _str_to_bool),
    # Intel/AMD VAAPI (hevc_vaapi) — uses CQP rate-control with a fixed QP.
    # `compression_level` is 0–7 where lower means more analysis / better
    # quality at the same bitrate (driver-specific, but 4 is a sane median).
    # v0.3.67+.
    ("vaapi_qp",                         22,        int),
    ("vaapi_compression_level",          4,         int),
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
    qsv_cq: int = 22,
    qsv_preset: str = "medium",
    qsv_lookahead: bool = False,
    vaapi_qp: int = 22,
    vaapi_compression_level: int = 4,
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
        qsv_cq=qsv_cq, qsv_preset=qsv_preset, qsv_lookahead=qsv_lookahead,
        vaapi_qp=vaapi_qp, vaapi_compression_level=vaapi_compression_level,
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
    qsv_cq: int = 22,
    qsv_preset: str = "medium",
    qsv_lookahead: bool = False,
    vaapi_qp: int = 22,
    vaapi_compression_level: int = 4,
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
    # Hardware-device init for VAAPI / QSV. Both must come BEFORE -i.
    #
    # Render-node selection (v0.3.90+): pre-v0.3.90 we hardcoded
    # `/dev/dri/renderD128`. On a multi-GPU host (e.g. NUC9 with both
    # an Intel iGPU and an NVIDIA Quadro), PCI enumeration often puts
    # the discrete card at renderD128 and the Intel iGPU at renderD129
    # — meaning our libva init would land on the NVIDIA driver and
    # fail to load iHD. encoder_caps now reads
    # `/sys/class/drm/<node>/device/uevent` for each render node and
    # picks the right one per encoder (i915-only for QSV, i915 or
    # amdgpu/radeon for VAAPI). Falls back to renderD128 only if
    # detection failed entirely.
    #
    # QSV note: on Linux, QSV sits on top of VAAPI. The two-step
    # pattern below initialises a VAAPI device bound to the render
    # node, then creates a QSV context that *adopts* it (the `@va`
    # syntax). NVENC and libx265 still need no pre-input args (NVENC
    # reads the GPU via the CUDA driver; libx265 is software).
    cmd = ["ffmpeg", "-y"]
    if encoder in ("vaapi", "qsv"):
        from backend.encoder_caps import detect_encoders
        caps = detect_encoders()
        if encoder == "vaapi":
            node = caps.vaapi_render_node or "/dev/dri/renderD128"
            cmd += ["-vaapi_device", node]
        else:  # qsv
            node = caps.qsv_render_node or "/dev/dri/renderD128"
            cmd += [
                "-init_hw_device", f"vaapi=va:{node}",
                "-init_hw_device", "qsv=qsv@va",
            ]
    cmd += ["-i", input_path]

    # Add external subtitle files as additional inputs (input 1, 2, 3, ...)
    ext_subs = external_subtitle_files or []
    for es in ext_subs:
        cmd += ["-i", es["path"]]

    # Resolution scaling (applied before video encoder)
    scale = RESOLUTION_MAP.get(target_resolution)
    if encoder == "vaapi":
        # VAAPI needs frames on the GPU before they hit hevc_vaapi. The
        # hwupload filter does the CPU→GPU copy; format=nv12 forces the
        # 8-bit 4:2:0 layout the hardware encoder expects. Combined with
        # an optional scale stage so we stay on a single -vf chain.
        if scale:
            cmd += ["-vf", f"scale={scale},format=nv12,hwupload"]
        else:
            cmd += ["-vf", "format=nv12,hwupload"]
    elif encoder == "qsv":
        # hevc_qsv accepts software-decoded frames directly — ffmpeg
        # uploads to the iGPU internally. So scaling stays on the CPU
        # side here (sw scale → encoder), which is fine for typical
        # workloads. Hardware-decode + scale_qsv would be faster but is
        # an optimisation for a later release.
        if scale:
            cmd += ["-vf", f"scale={scale}"]
    elif scale:
        # NVENC / libx265 — software scale (matches pre-v0.3.67 behaviour).
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
    elif encoder == "qsv":
        # Intel Quick Sync HEVC. `global_quality` is QSV's ICQ-mode
        # quality target — closest analogue to NVENC's CQ. 8-bit `main`
        # profile for compatibility with Gen9 / older Quick Sync; 10-bit
        # `main10` is supported on Gen11+ / Arc but we keep the safe
        # default and let users opt in via custom_ffmpeg_flags. v0.3.67+.
        cmd += [
            "-c:v", "hevc_qsv",
            "-preset", qsv_preset,
            "-global_quality", str(qsv_cq),
            "-profile:v", "main",
        ]
        # Optional look-ahead rate control (v0.3.93+). Slight quality
        # bump at typical 10-20% throughput cost. Off by default.
        if qsv_lookahead:
            cmd += ["-look_ahead", "1"]
    elif encoder == "vaapi":
        # Intel/AMD VAAPI HEVC. CQP rate control via -qp. Output frames
        # are already on the GPU (hwupload filter above), so no -pix_fmt
        # is needed — the encoder consumes vaapi surfaces directly.
        # `compression_level` (0–7, lower = more analysis) is the VAAPI
        # equivalent of preset speed, driver-dependent. v0.3.67+.
        cmd += [
            "-c:v", "hevc_vaapi",
            "-qp", str(vaapi_qp),
            "-compression_level", str(vaapi_compression_level),
            "-profile:v", "main",
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

    # Muxer settings — ffmpeg defaults restored in v0.3.42.
    #
    # `-max_muxing_queue_size 9999` (added v0.3.37, removed v0.3.42) bumped
    # the muxer's per-stream queue from the default 2048 packets to 9999.
    # In retrospect that *also* weakened the natural back-pressure that
    # keeps concurrent ffmpeg sessions politely sharing the GPU encoder —
    # similar issue to the awaited DB write that v0.3.40 broke. With more
    # room in the muxer queue, the encoder kept producing flat-out, the
    # muxer accumulated packets, and concurrent sessions exposed GPU
    # scheduling unfairness when Plex Transcoder was also active. The
    # pre-strip pass added in v0.3.39 already handles the original
    # motivating case (Breathless-style files with 30+ subtitle streams)
    # by removing them in a separate `-c copy` pass before the encode, so
    # the queue bump isn't needed even for that scenario.
    #
    # `-fflags +flush_packets` was reverted in v0.3.38 for similar reasons
    # (forcing per-packet flushes cost ~20% throughput).
    pass  # placeholder so the diff is small; nothing appended to cmd

    cmd += [output_path]
    return cmd


def _hevc_tag_for_encoder(encoder: str | None) -> str:
    """Pick the right codec/encoder label for the output filename.

    Scene convention distinguishes encoder from codec:
      - x264/x265  = the specific software encoder (libx264, libx265)
      - h264/h265  = the codec standard, encoder-agnostic

    libx265 → `x265` (correct, it *is* that encoder)
    NVENC  → `h265` (NVENC is not x265; using `x265` on NVENC output
             misrepresents what produced the file and triggers "this
             isn't a real x265 encode" complaints from picky users and
             scene release-matching heuristics).
    """
    return "x265" if (encoder or "").lower() == "libx265" else "h265"


def rename_source_to_target_codec(filename: str, encoder: str | None = None) -> str:
    """Rewrite x264/h264/AVC codec tags in `filename` for the output encoder.

    The target label depends on `encoder` — see `_hevc_tag_for_encoder`.
    Keeps the old behaviour (always `x265`) when `encoder` is None for
    back-compat with callers that haven't been updated yet.
    """
    target = _hevc_tag_for_encoder(encoder) if encoder is not None else "x265"
    result = re.sub(r'\bx264\b', target, filename, flags=re.IGNORECASE)
    result = re.sub(r'\bh264\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bAVC\b', target, result)
    # Remove "Remux" since re-encoded files are no longer remuxes
    result = re.sub(r'\s*\bRemux\b\s*', ' ', result, flags=re.IGNORECASE).strip()
    # Clean up any double spaces left behind
    result = re.sub(r'  +', ' ', result)
    return result


# Backwards-compat alias. Existing call sites that don't (yet) know the
# encoder fall through to "x265" as before; new call sites should pass
# `encoder=` and use the new name.
rename_x264_to_x265 = rename_source_to_target_codec


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


def get_output_path(input_path: str, suffix: str = "", encoder: str | None = None) -> str:
    """Return the final output path: rename codec tag, add suffix, and change extension to .mkv.

    `encoder` is threaded through so libx265 output gets `x265` and
    NVENC output gets `h265` — the scene convention that distinguishes
    software-encoder tags from codec tags.
    """
    p = Path(input_path)
    new_stem = rename_source_to_target_codec(p.stem, encoder=encoder)
    if suffix:
        new_stem = new_stem + suffix
    return str(p.parent / (new_stem + ".mkv"))


def get_temp_path(input_path: str) -> str:
    """Return a temporary conversion path in the same directory as input."""
    p = Path(input_path)
    return str(p.parent / (p.stem + ".converting.mkv"))


async def _prestrip_subtitles(
    *,
    input_path: str,
    subtitle_streams: list[dict],
    audio_streams_to_keep: list[dict] | None,
    subtitle_streams_to_remove: set,
) -> str | None:
    """Fast `-c copy` remux pass that drops unwanted subtitle streams.

    Returns the path to the stripped file on success, None on failure
    (caller falls back to single-pass encoding).

    Used by `convert_file` when many subtitle streams need removal — see
    the two-pass workflow comment there for the rationale. The output
    file lives in the same directory as the input with a `.stripped.mkv`
    suffix so it's adjacent to its source for filesystem-locality and
    can be removed by the same media-dir cleanup if anything goes wrong.
    """
    p = Path(input_path)
    out_path = str(p.parent / (p.stem + ".stripped.mkv"))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", input_path]
    # Always keep the first video stream and any attachments. Audio: either
    # keep all (when no inline audio removal is in play) or only the
    # explicitly-listed kept streams.
    cmd += ["-map", "0:v:0"]
    if audio_streams_to_keep is not None:
        for stream in audio_streams_to_keep:
            idx = stream.get("stream_index")
            if idx is not None:
                cmd += ["-map", f"0:{idx}"]
    else:
        cmd += ["-map", "0:a"]
    # Subtitles: keep only the ones not in the removal set.
    kept_count = 0
    for sub in subtitle_streams:
        idx = sub.get("index")
        if idx is None or idx in subtitle_streams_to_remove:
            continue
        cmd += ["-map", f"0:{idx}"]
        kept_count += 1
    cmd += ["-map", "0:t?"]  # attachments (fonts etc.)
    cmd += ["-c", "copy", out_path]

    print(
        f"[CONVERT] Pre-strip pass: drop {len(subtitle_streams_to_remove)} subs, "
        f"keep {kept_count} subs (-c copy, no re-encode)",
        flush=True,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # 5-minute ceiling — pre-strip is I/O bound and ~30s is typical for
        # a 2 GB file; anything over 5 minutes means we hit a network mount
        # stall or similar, in which case bailing and falling back to the
        # single-pass encode is better than blocking the whole job.
        try:
            await asyncio.wait_for(proc.wait(), timeout=300)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            print(f"[CONVERT] Pre-strip pass timed out — falling back to single-pass encode", flush=True)
            try:
                Path(out_path).unlink(missing_ok=True)
            except OSError:
                pass
            return None

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            stderr_tail = stderr_bytes.decode(errors="replace")[-500:]
            print(f"[CONVERT] Pre-strip pass failed (rc={proc.returncode}): {stderr_tail}", flush=True)
            try:
                Path(out_path).unlink(missing_ok=True)
            except OSError:
                pass
            return None

        return out_path
    except Exception as exc:
        print(f"[CONVERT] Pre-strip pass crashed ({exc}) — falling back to single-pass encode", flush=True)
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass
        return None


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


def parse_ffmpeg_progress(
    line: str,
    duration: float,
    start_time: float = 0,
    total_frames: Optional[int] = None,
) -> Optional[dict]:
    """
    Parse an ffmpeg stderr line for progress information.

    Returns a dict with keys: progress (0-100 float), fps (float or None),
    eta_seconds (int or None). Returns None if the line lacks any usable
    progress info.

    Sources, in priority order:
      1. `time=HH:MM:SS` field — the muxer-side committed-output position.
         Reliable on most files. Used for progress = elapsed / duration.
      2. `frame=N` field plus `total_frames` argument — fallback used when
         time= is `N/A`. ffmpeg emits `time=N/A` when the muxer can't
         commit valid output timestamps yet (`-c:a copy` on sources with
         non-monotonic audio timestamps is the common cause — encoder is
         producing frames fine, but the muxer's clock is parked at N/A
         throughout the encode). The frame counter still advances honestly
         in that case, so we use `current_frame / total_frames` as a
         drop-in replacement for the time-based ratio. v0.3.43+.
    """
    fps_match = re.search(r'fps=\s*(\d+(?:\.\d+)?)', line)
    fps_val = float(fps_match.group(1)) if fps_match else None

    # Compute progress from BOTH sources when available, then take the
    # higher number. Rationale (v0.3.44+): time= reflects the muxer's
    # committed-output position, which can lag the encoder by tens of
    # seconds when audio packets with non-monotonic PTS or other timing
    # quirks delay timestamp commits. Meanwhile frame= reflects what the
    # encoder has actually produced. Both can be present on the same line;
    # using whichever is higher means we never under-report when the
    # muxer's clock is stuck behind the encoder. Falls all the way back to
    # None only when neither source is parseable on this line.
    time_ratio: Optional[float] = None
    frame_ratio: Optional[float] = None

    time_match = re.search(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', line)
    if time_match and duration and duration > 0:
        hours = int(time_match.group(1))
        minutes = int(time_match.group(2))
        seconds = float(time_match.group(3))
        elapsed = hours * 3600 + minutes * 60 + seconds
        time_ratio = elapsed / duration

    frame_match = re.search(r'frame=\s*(\d+)', line)
    if frame_match and total_frames and total_frames > 0:
        cur_frame = int(frame_match.group(1))
        frame_ratio = cur_frame / total_frames

    if time_ratio is None and frame_ratio is None:
        return None

    progress_ratio = max(
        time_ratio if time_ratio is not None else 0.0,
        frame_ratio if frame_ratio is not None else 0.0,
    )

    progress = min(100.0, progress_ratio * 100)
    eta_seconds = None
    if start_time > 0 and progress_ratio > 0.01:
        wall_elapsed = time.monotonic() - start_time
        eta_seconds = int(wall_elapsed / progress_ratio * (1 - progress_ratio))

    return {
        "progress": round(progress, 2),
        "fps": fps_val,
        "eta_seconds": eta_seconds,
    }


async def _probe_vmaf_stream(path: str) -> dict:
    """Lightweight ffprobe for VMAF-relevant video-stream properties only.

    Returns a dict with: width, height, fps (float), frame_count (int or None),
    pix_fmt, color_range, color_space, duration. All fields fall back to
    empty/None on probe failure or missing metadata so the caller can
    continue to the actual VMAF run — we never want diagnostics to break
    the main path.
    """
    info = {
        "width": 0, "height": 0, "fps": None, "frame_count": None,
        "pix_fmt": "", "color_range": "", "color_space": "", "duration": None,
    }
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,"
            "pix_fmt,color_range,color_space,duration",
            path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return info
        import json as _j
        data = _j.loads(stdout.decode() or "{}")
        streams = data.get("streams", [])
        if not streams:
            return info
        s = streams[0]
        info["width"] = int(s.get("width") or 0)
        info["height"] = int(s.get("height") or 0)
        info["pix_fmt"] = s.get("pix_fmt") or ""
        info["color_range"] = s.get("color_range") or ""
        info["color_space"] = s.get("color_space") or ""
        # Duration as float
        try:
            info["duration"] = float(s.get("duration") or 0) or None
        except (TypeError, ValueError):
            info["duration"] = None
        # Frame rate: prefer r_frame_rate ("24000/1001"), fall back to avg
        fr = s.get("r_frame_rate") or s.get("avg_frame_rate") or ""
        if "/" in fr:
            num, _, den = fr.partition("/")
            try:
                n = float(num); d = float(den)
                if d > 0:
                    info["fps"] = n / d
            except (TypeError, ValueError):
                pass
        elif fr:
            try:
                info["fps"] = float(fr)
            except (TypeError, ValueError):
                pass
        # nb_frames is often absent (esp. after re-encode without -fflags);
        # we just report None in that case rather than hang on a counted probe.
        try:
            nbf = s.get("nb_frames")
            if nbf and str(nbf).isdigit():
                info["frame_count"] = int(nbf)
        except Exception:
            pass
    except Exception:
        # Swallow everything — this is best-effort diagnostic data.
        pass
    return info


def _vmaf_probe_summary(label: str, info: dict) -> str:
    """Format a probe dict as a single compact log line."""
    parts = [f"{label}:"]
    if info.get("width") and info.get("height"):
        parts.append(f"{info['width']}x{info['height']}")
    if info.get("fps"):
        parts.append(f"{info['fps']:.3f}fps")
    if info.get("frame_count"):
        parts.append(f"{info['frame_count']}f")
    elif info.get("duration"):
        parts.append(f"{info['duration']:.1f}s")
    if info.get("pix_fmt"):
        parts.append(info["pix_fmt"])
    if info.get("color_range"):
        parts.append(f"range={info['color_range']}")
    if info.get("color_space"):
        parts.append(f"cs={info['color_space']}")
    return " ".join(parts)


def _is_bimodal_vmaf(result: dict) -> bool:
    """Heuristic for "VMAF measurement got desynced mid-window" vs "real bad encode".

    The fingerprint: a chunk of frames scored ~0 (frames compared after
    desync) while another chunk scored ~100 (frames compared before
    desync). The arithmetic mean lands somewhere in between, but min and
    max sit at the extremes. A genuinely bad encode has min ≈ mean ≈ max.

    Cuts: min < 20 AND max ≥ 90. Tight enough that "noticeable but real"
    quality drops (e.g., posterised animation that genuinely scores 70-85)
    don't get retried, loose enough to catch the 0/100 split this is built
    for.
    """
    mn = result.get("min")
    mx = result.get("max")
    return mn is not None and mx is not None and mn < 20 and mx >= 90


async def _run_libvmaf_pass(
    *,
    input_path: str,
    temp_path: str,
    seek: float,
    duration: float,
    ref_pipeline: str,
    dist_pipeline: str,
    json_path,
    fps_for_progress: float,
    progress_callback,
    step_label: str,
) -> dict:
    """Run libvmaf for a single seek window and return a result dict.

    Returns:
        {
            "score": float | None,             # pooled mean, rounded to 1dp
            "min": float | None,
            "max": float | None,
            "harmonic_mean": float | None,
            "error": str | None,               # populated on ffmpeg / parse failure
            "stderr_tail": list[str],          # last few stderr lines for diag logging
            "seek": float,                     # echoed back so caller can match
        }

    The filter chain (range fix → fps fix → format → scale2ref → libvmaf)
    is built from the supplied ref_pipeline/dist_pipeline so that probe-
    derived bits (target_fps, range, etc.) only get computed once per job
    and reused across retries.
    """
    import re as _re
    import json as _vjson

    vmaf_filter = (
        f"[0:v]{ref_pipeline}[ref_norm];"
        f"[1:v]{dist_pipeline}[dist_norm];"
        f"[dist_norm][ref_norm]scale2ref=flags=bicubic[dist][ref];"
        f"[dist][ref]libvmaf=model=version=vmaf_v0.6.1:n_threads=4:"
        f"log_fmt=json:log_path={json_path}:shortest=1"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-ss", f"{seek:.3f}", "-i", input_path,
        "-ss", f"{seek:.3f}", "-i", temp_path,
        "-t", f"{duration:.3f}",
        "-filter_complex", vmaf_filter,
        "-f", "null", "-",
    ]

    total_frames = max(1, int(duration * fps_for_progress))
    if progress_callback:
        await progress_callback(progress=0, fps=0, eta_seconds=None, step=step_label)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    err_lines: list[str] = []
    buf = ""
    start = time.monotonic()
    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            break
        buf += chunk.decode(errors="replace")
        while "\r" in buf or "\n" in buf:
            r_pos = buf.find("\r")
            n_pos = buf.find("\n")
            if r_pos == -1: pos = n_pos
            elif n_pos == -1: pos = r_pos
            else: pos = min(r_pos, n_pos)
            line = buf[:pos].strip()
            buf = buf[pos + 1:]
            if not line:
                continue
            err_lines.append(line)
            fm = _re.search(r'frame=\s*(\d+)', line)
            if not fm or not progress_callback:
                continue
            frame = int(fm.group(1))
            pct = min(99.0, frame / total_frames * 100)
            fps_match = _re.search(r'fps=\s*([\d.]+)', line)
            analyse_fps = float(fps_match.group(1)) if fps_match else 0.0
            eta = None
            elapsed = time.monotonic() - start
            if pct > 1.0:
                eta = int(elapsed / (pct / 100) * (1 - pct / 100))
            await progress_callback(
                progress=pct, fps=analyse_fps, eta_seconds=eta, step=step_label,
            )

    timeout = max(300.0, duration * 3.0)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {
            "score": None, "min": None, "max": None, "harmonic_mean": None,
            "error": f"VMAF run exceeded {timeout:.0f}s timeout",
            "stderr_tail": err_lines[-5:], "seek": seek,
        }

    if proc.returncode != 0:
        tail = " | ".join(err_lines[-5:])[:500]
        return {
            "score": None, "min": None, "max": None, "harmonic_mean": None,
            "error": (f"ffmpeg rc={proc.returncode}: {tail}" if tail else f"ffmpeg rc={proc.returncode}"),
            "stderr_tail": err_lines[-5:], "seek": seek,
        }

    if not Path(json_path).exists():
        return {
            "score": None, "min": None, "max": None, "harmonic_mean": None,
            "error": "VMAF JSON not produced",
            "stderr_tail": err_lines[-5:], "seek": seek,
        }

    try:
        vdata = _vjson.loads(Path(json_path).read_text())
        pooled = vdata.get("pooled_metrics", {}).get("vmaf", {})
        mean = pooled.get("mean")
        return {
            "score": round(mean, 1) if mean is not None else None,
            "min": pooled.get("min"),
            "max": pooled.get("max"),
            "harmonic_mean": pooled.get("harmonic_mean"),
            "error": None,
            "stderr_tail": err_lines[-5:],
            "seek": seek,
        }
    except Exception as exc:
        return {
            "score": None, "min": None, "max": None, "harmonic_mean": None,
            "error": f"VMAF JSON parse failed: {exc}",
            "stderr_tail": err_lines[-5:], "seek": seek,
        }


async def remeasure_vmaf(
    source_path: str,
    encoded_path: str,
    *,
    duration_hint: float | None = None,
    progress_callback=None,
) -> dict:
    """Re-run VMAF analysis against an existing source/encoded pair.

    Used by the "Re-measure suspect VMAF scores" workflow (v0.3.32+) to
    refresh scores on completed jobs without re-encoding. Goes through the
    same bimodal-retry path as `convert_file` so a previously-bogus score
    can land on a clean second-seek result.

    Returns:
        {
            "score": float | None,
            "uncertain": bool,        # True iff every pass came back bimodal
            "error": str | None,
            "min": float | None,
            "max": float | None,
        }

    Both files must exist on disk; if either is missing returns
    `{"score": None, "uncertain": False, "error": "...source/encoded missing..."}`.
    """
    if not Path(source_path).exists():
        return {"score": None, "uncertain": False, "error": f"source missing: {source_path}", "min": None, "max": None}
    if not Path(encoded_path).exists():
        return {"score": None, "uncertain": False, "error": f"encoded missing: {encoded_path}", "min": None, "max": None}

    # Probe duration from the source if not provided.
    duration = duration_hint or 0.0
    src_info = await _probe_vmaf_stream(source_path)
    dst_info = await _probe_vmaf_stream(encoded_path)
    if duration <= 0:
        try:
            duration = float(src_info.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0

    # Pick sampling window — 30s at 33% of file (matches convert_file's
    # heuristic), or whole file when very short.
    if duration > 30:
        primary_seek = max(0.0, duration * 0.33)
        window_dur = 30.0
    elif duration > 0:
        primary_seek = 0.0
        window_dur = duration
    else:
        primary_seek = 0.0
        window_dur = 30.0

    # Build the same normalisation pipeline convert_file uses.
    target_fps = src_info.get("fps") or dst_info.get("fps")
    fps_clause = f"fps=fps={target_fps:.6f}" if target_fps and target_fps > 0 else ""
    range_clause = "scale=in_range=auto:out_range=tv:flags=bicubic"
    ref_chain = [range_clause]
    dist_chain = [range_clause]
    if fps_clause:
        ref_chain.append(fps_clause)
        dist_chain.append(fps_clause)
    ref_chain.append("format=yuv420p")
    dist_chain.append("format=yuv420p")
    ref_pipeline = ",".join(ref_chain)
    dist_pipeline = ",".join(dist_chain)

    fps_for_progress = target_fps or 24.0
    vmaf_dir = Path("/tmp/shrinkerr_vmaf")
    vmaf_dir.mkdir(parents=True, exist_ok=True)
    import re as _re_rm
    import uuid as _uuid_rm
    safe_stem = _re_rm.sub(r"[^A-Za-z0-9._-]", "_", Path(source_path).stem)[:20]
    json_paths_to_cleanup: list[Path] = []

    async def _one_pass(seek: float, label: str) -> dict:
        jp = vmaf_dir / f"{safe_stem}_{_uuid_rm.uuid4().hex[:8]}_vmaf.json"
        json_paths_to_cleanup.append(jp)
        return await _run_libvmaf_pass(
            input_path=source_path,
            temp_path=encoded_path,
            seek=seek,
            duration=window_dur,
            ref_pipeline=ref_pipeline,
            dist_pipeline=dist_pipeline,
            json_path=jp,
            fps_for_progress=fps_for_progress,
            progress_callback=progress_callback,
            step_label=label,
        )

    primary = await _one_pass(primary_seek, "VMAF remeasure")
    runs = [primary]
    if _is_bimodal_vmaf(primary) and duration > 90 and window_dur > 0:
        alt_pct = 0.66 if primary_seek < duration * 0.5 else 0.33
        alt_seek = max(0.0, min(duration - window_dur, duration * alt_pct))
        if abs(alt_seek - primary_seek) >= 60:
            runs.append(await _one_pass(alt_seek, "VMAF remeasure (retry)"))

    # Cleanup JSON tempfiles unconditionally — score is already parsed.
    for jp in json_paths_to_cleanup:
        try:
            Path(jp).unlink(missing_ok=True)
        except OSError:
            pass

    scored = [r for r in runs if r.get("score") is not None]
    if not scored:
        first_error = next((r.get("error") for r in runs if r.get("error")), "VMAF returned no score")
        return {"score": None, "uncertain": False, "error": first_error, "min": None, "max": None}

    best = max(scored, key=lambda r: r["score"])
    return {
        "score": best["score"],
        "uncertain": _is_bimodal_vmaf(best),
        "error": None,
        "min": best.get("min"),
        "max": best.get("max"),
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
    # Pass the effective encoder so the output filename picks the right
    # codec tag: `x265` for libx265, `h265` for NVENC (see
    # rename_source_to_target_codec for the rationale).
    final_path = get_output_path(input_path, suffix=filename_suffix, encoder=encoder)
    nvenc_preset = override_preset if override_preset is not None else live_settings.get("nvenc_preset", "p6")
    libx265_preset = override_libx265_preset if override_libx265_preset is not None else live_settings.get("libx265_preset", "medium")
    cq = override_cq if override_cq is not None else live_settings.get("nvenc_cq", 20)
    crf = override_crf if override_crf is not None else live_settings.get("libx265_crf", 20)
    # Intel QSV / VAAPI knobs. No per-job overrides yet — the rule engine
    # and the estimate modal will gain them in a later phase. For now they
    # come from the DB only. v0.3.67+.
    qsv_cq = live_settings.get("qsv_cq", 22)
    qsv_preset = live_settings.get("qsv_preset", "medium")
    qsv_lookahead = bool(live_settings.get("qsv_lookahead", False))
    vaapi_qp = live_settings.get("vaapi_qp", 22)
    vaapi_compression_level = live_settings.get("vaapi_compression_level", 4)
    audio_codec = override_audio_codec if override_audio_codec is not None else live_settings.get("audio_codec", "copy")
    audio_bitrate = override_audio_bitrate if override_audio_bitrate is not None else live_settings.get("audio_bitrate", 128)

    # Probe file for audio/subtitle stream details
    lossless_conversion = None
    audio_stream_codecs = None
    subtitle_streams = None
    audio_streams_to_keep: Optional[list] = None  # inline keep-list (if tracks_to_remove given)
    probe_audio_tracks: list = []
    # Source video fps captured at probe time. Used to compute total
    # expected frames for the progress callback's frame-count fallback —
    # ffmpeg sometimes reports `time=N/A` instead of HH:MM:SS when the
    # muxer can't commit valid output timestamps (e.g. -c:a copy on a
    # WEBDL with non-monotonic audio PTS), in which case we fall back to
    # frame-counter-based progress = current_frame / total_expected.
    # v0.3.43+.
    source_video_fps: float = 0.0
    try:
        from backend.scanner import probe_file
        probe_data = await probe_file(input_path)
        if probe_data:
            # Subtitle streams for safe mapping (skip unsupported codecs)
            # Map probe format to what build_ffmpeg_cmd expects
            raw_subs = probe_data.get("subtitle_tracks", [])
            subtitle_streams = [{"codec_name": s.get("codec", ""), "index": s.get("stream_index")} for s in raw_subs]

            probe_audio_tracks = probe_data.get("audio_tracks") or []
            source_video_fps = float(probe_data.get("video_fps") or 0.0)

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

    if encoder == "libx265":
        active_preset, active_quality = libx265_preset, f"crf={crf}"
    elif encoder == "qsv":
        active_preset, active_quality = qsv_preset, f"global_quality={qsv_cq}"
    elif encoder == "vaapi":
        active_preset, active_quality = f"compression_level={vaapi_compression_level}", f"qp={vaapi_qp}"
    else:
        active_preset, active_quality = nvenc_preset, f"cq={cq}"
    print(f"[CONVERT] Settings: encoder={encoder}, preset={active_preset}, {active_quality}, audio={audio_codec}, resolution={target_resolution}", flush=True)
    if audio_streams_to_keep is not None:
        removed_count = len(probe_audio_tracks) - len(audio_streams_to_keep)
        print(f"[CONVERT] Inline audio removal: keeping {len(audio_streams_to_keep)} of {len(probe_audio_tracks)} ({removed_count} removed)", flush=True)

    sub_remove_set = set(subtitle_tracks_to_remove or [])
    if sub_remove_set:
        print(f"[CONVERT] Inline subtitle removal: {len(sub_remove_set)} track(s) to drop", flush=True)

    # Two-pass workflow for files with many unwanted subtitle streams (v0.3.39+).
    #
    # Background: this two-pass workflow was added in v0.3.43–v0.3.44 to
    # work around what looked like an ffmpeg stall — frame= kept advancing
    # but time= froze and the progress bar pinned, with `speed=` reading
    # ~1× instead of the expected ~5× on files with many unmapped sub
    # streams. The "fix" was a fast `-c copy` remux pass to strip the
    # unwanted subs before the main encode.
    #
    # Hindsight (v0.3.55): the actual bug was on our side — the progress
    # parser only read `time=` and went stale when ffmpeg paused emitting
    # it. v0.3.43–v0.3.44 added the frame= fallback that actually fixed
    # the visible stall. Since `speed=` is computed as `time/wall_clock`,
    # the "1× vs 5×" measurements that motivated the prestrip were
    # themselves reading the stale time= value — i.e. measurement
    # artefact, not real encoder slowdown.
    #
    # The prestrip's cost is concrete: an extra ~30s–1min I/O-bound pass
    # plus a full input-size temp write, every time a file has 6+ subs to
    # drop. The benefit is no longer believed to exist. Disabled by
    # raising the threshold past anything realistic. The function and
    # call block stay so a single-line revert can re-enable it if real
    # encoder slowdown does turn up. v0.3.55+.
    _PRESTRIP_SUB_THRESHOLD = 9999
    prestrip_path: str | None = None
    encode_input_path = input_path  # what the encoder reads from (gets swapped after pre-strip)
    if len(sub_remove_set) >= _PRESTRIP_SUB_THRESHOLD and subtitle_streams:
        prestrip_path = await _prestrip_subtitles(
            input_path=input_path,
            subtitle_streams=subtitle_streams,
            audio_streams_to_keep=audio_streams_to_keep,
            subtitle_streams_to_remove=sub_remove_set,
        )
        if prestrip_path:
            # Subs (and any unwanted audio) are gone from the stripped file —
            # main encode now operates on a clean 5-7 stream input. Re-probe
            # to discover the post-strip stream indices, then reset all the
            # "what to drop / keep" inputs since strip already did the
            # filtering. Don't reassign `input_path` — sidecar operations
            # (external subtitle renames, scan_results updates) must still
            # see the original source path.
            from backend.scanner import probe_file as _reprobe
            new_probe = await _reprobe(prestrip_path)
            if new_probe:
                new_subs = new_probe.get("subtitle_tracks") or []
                subtitle_streams = [
                    {"codec_name": s.get("codec", ""), "index": s.get("stream_index")}
                    for s in new_subs
                ]
                # Rebuild probe_audio_tracks/audio_stream_codecs from the new
                # layout so the main encode sees post-strip audio indices
                # (matters when audio_codec != copy and the build_ffmpeg_cmd
                # iterates per-audio-stream).
                probe_audio_tracks = new_probe.get("audio_tracks") or []
                audio_stream_codecs = [t.get("codec", "") for t in probe_audio_tracks]
            encode_input_path = prestrip_path
            # Reset the inline keep-lists — strip already enforced them.
            # Default `-map 0:a` then maps everything that's left (all
            # kept), and an empty sub_remove_set means no further filtering
            # in the main pass.
            audio_streams_to_keep = None
            sub_remove_set = set()
            print(f"[CONVERT] Pre-strip done — main encode runs on {prestrip_path}", flush=True)

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
        encode_input_path, temp_path, encoder=encoder,
        nvenc_preset=nvenc_preset, libx265_preset=libx265_preset,
        qsv_cq=qsv_cq, qsv_preset=qsv_preset, qsv_lookahead=qsv_lookahead,
        vaapi_qp=vaapi_qp, vaapi_compression_level=vaapi_compression_level,
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
    # VMAF sampling strategy: 30-second window at 33% into the file, seeked
    # via input-level `-ss` on both inputs (accurate seek, default in modern
    # ffmpeg) so both streams emerge from the decoder with matching PTS
    # before the filter graph touches them. No filter-level `trim` — that's
    # what used to cause the bimodal-score failure mode when source had
    # VFR timestamps, non-zero start_pts, or interlaced field ordering.
    #
    # 0.3.3 briefly switched to whole-file compare for TV-sized content as
    # belt-and-suspenders while we were chasing the real cause (which turned
    # out to be fps + colour-range mismatch, not sampling). Now that those
    # are normalised in the filter graph, 30-second sampling is reliable
    # again and roughly 50× faster on a 25-minute episode.
    if duration > 30:
        vmaf_seek = max(0.0, duration * 0.33)
        vmaf_duration = 30.0
    elif duration > 0:
        # Very short file — compare the whole thing from frame zero.
        vmaf_seek = 0.0
        vmaf_duration = duration
    else:
        # Duration unknown (probe failed) — sample the first 30s.
        vmaf_seek = 0.0
        vmaf_duration = 30.0

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
        # Total expected frames for the progress callback's frame-count
        # fallback when ffmpeg reports `time=N/A`. Computed from probe
        # duration × source fps; set to None when we don't know the source
        # fps (parser then falls back to "no progress update" rather than
        # emitting bogus values from a nonsense divisor).
        progress_total_frames: Optional[int] = None
        if duration > 0 and source_video_fps > 0:
            progress_total_frames = max(1, int(duration * source_video_fps))
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
                    parsed = parse_ffmpeg_progress(
                        line, duration,
                        start_time=encode_start_time,
                        total_frames=progress_total_frames,
                    )
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
        if prestrip_path:
            try: Path(prestrip_path).unlink(missing_ok=True)
            except OSError: pass
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

    # If the converted file is LARGER than the original, discard it.
    #
    # v0.3.69 change: this used to KEEP the encoded file when track removal
    # had happened inline ("the user wanted those tracks gone, the encoded
    # file has them gone, discarding would lose that work"). The trade-off
    # was that the user ended up with a *larger* file just so they didn't
    # lose the cleanup. The queue.py finalisation now requeues an audio-
    # only follow-up job when this happens, which applies the cleanup to
    # the original (smaller) file without the failed video re-encode. Net:
    # cleanup still gets done AND the file shrinks.
    #
    # `had_track_removal` is still computed for the encoding_stats payload
    # so the completed-jobs report can show users exactly what cleanup
    # was lined up and will be retried as audio-only.
    had_track_removal = bool(audio_tracks_to_remove) or bool(subtitle_tracks_to_remove) or bool(external_sub_files)
    if space_saved < 0:
        print(
            f"[CONVERT] Output ({output_size}) is LARGER than original ({original_size}), discarding"
            + (" (audio/sub cleanup will be retried as audio-only follow-up)" if had_track_removal else ""),
            flush=True,
        )
        try:
            temp.unlink()
        except OSError:
            pass
        if prestrip_path:
            try: Path(prestrip_path).unlink(missing_ok=True)
            except OSError: pass
        # Capture the same encoding_stats payload a successful encode would
        # write, so the completed-jobs report shows the original-vs-discarded
        # comparison (size, bitrate, settings used). Without this the row
        # rendered with no body at all — users couldn't see WHY the encode
        # was rejected or what threshold to tune. v0.3.55+.
        encode_time_skipped = time.monotonic() - encode_start_time
        return {
            "success": True,  # Not an error, just no savings
            "output_path": input_path,  # Keep original path
            "space_saved": 0,
            "error": None,
            "skipped_larger": True,
            # Signal to queue.py finalisation: a follow-up audio-only job
            # should be queued so the cleanup work that the discarded
            # encode would have included still happens on the original
            # file. Empty when there was no cleanup work to begin with.
            # v0.3.69+.
            "had_track_removal": had_track_removal,
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
                # ratio will be negative here (the whole point — encode grew the
                # file). Frontend renders negative ratios in a warning colour
                # so it doesn't look like a successful saving.
                "ratio": round((1 - output_size / original_size) * 100, 1) if original_size > 0 else 0,
                "encode_seconds": round(encode_time_skipped, 1),
                "duration": duration,
                "input_bitrate": round(original_size * 8 / duration / 1_000_000, 2) if duration > 0 else None,
                "output_bitrate": round(output_size * 8 / duration / 1_000_000, 2) if duration > 0 else None,
                "skipped_larger": True,
                "had_track_removal": had_track_removal,
            },
        }

    # VMAF analysis — compare original vs encoded BEFORE the original is moved/deleted.
    # `vmaf_error` carries the reason to the caller (queue.py / worker_mode.py) so a
    # file_event can be logged to the Activity page even when VMAF fails silently
    # inside ffmpeg. Without this, a failure would leave no trace in the UI.
    vmaf_score = None
    vmaf_error: str | None = None
    # Set to True only when EVERY VMAF pass came back bimodal (min~0/max~100
    # split → libvmaf desynced mid-window on every seek we tried). The
    # recorded score is still the user's best estimate but the UI surfaces a
    # "measurement-suspect" glyph so a user staring at a "Poor" tier on a
    # visually-fine encode knows they shouldn't trust it. v0.3.32+.
    vmaf_uncertain = False
    # Always log the decision — previously a false `vmaf_enabled` silently skipped
    # the whole block, making "why didn't VMAF run?" impossible to answer without
    # re-reading settings and re-running.
    print(
        f"[CONVERT] VMAF decision: enabled={vmaf_enabled} "
        f"(raw setting={_vmaf_setting!r})",
        flush=True,
    )
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
                #
                # Sanitize the stem — the json path is inlined into
                # ffmpeg's -filter_complex as `libvmaf=...:log_path=X:...`,
                # and filter-arg syntax treats apostrophes, backslashes,
                # colons, spaces and brackets as structural. Unbalanced
                # quotes (e.g. "Grey's Anatomy...") silently break the
                # libvmaf arg, the ffmpeg process exits non-zero, and
                # the job completes with no score. Keep only alnum / _ / - .
                import re as _re_vmaf
                import uuid as _uuid
                _safe_stem = _re_vmaf.sub(r"[^A-Za-z0-9._-]", "_", Path(input_path).stem)[:20]
                _vmaf_id = f"{_safe_stem}_{_uuid.uuid4().hex[:8]}"
                vmaf_json_path = vmaf_dir / f"{_vmaf_id}_vmaf.json"

                # Probe both video streams BEFORE the compare. We use this for
                #   (a) fps normalization in the filter graph — forcing both
                #       streams to the source's frame rate kills the last
                #       common source of bimodal scores (VFR source vs CFR
                #       encode producing different frame counts, so libvmaf's
                #       frame-pair comparisons desync mid-file), and
                #   (b) diagnostic logging — a single side-by-side line of
                #       width / height / fps / pix_fmt / color_range lets us
                #       spot a format mismatch at a glance next time someone
                #       reports a wrong-looking score. Cheap enough (<1s per
                #       probe) to run unconditionally.
                src_info = await _probe_vmaf_stream(input_path)
                dst_info = await _probe_vmaf_stream(temp_path)
                print(f"[CONVERT] VMAF inputs — {_vmaf_probe_summary('ref', src_info)} | {_vmaf_probe_summary('dist', dst_info)}", flush=True)

                # Pick target fps for normalization. Prefer source fps; fall
                # back to encoded fps, then to no-op. Forcing both streams
                # through the same `fps` filter guarantees they emerge with
                # identical frame counts and rates, which is what libvmaf
                # needs for valid pairwise comparison. This is the canonical
                # fix for "sibling episodes score 49 and 96 on identical
                # settings" — the fps mismatch used to leave libvmaf comparing
                # frame N of one stream against a time-shifted frame N of the
                # other after any drift accumulated.
                target_fps = src_info.get("fps") or dst_info.get("fps")
                if target_fps and target_fps > 0:
                    # Use rational form so ffmpeg does exact arithmetic for
                    # common TV rates (23.976 = 24000/1001, etc.).
                    fps_clause = f"fps=fps={target_fps:.6f}"
                else:
                    fps_clause = ""

                # Color-range normalization. If the source is tagged "tv"
                # (limited 16-235) and the encode ended up "pc" (full 0-255)
                # — or tags are missing and ffmpeg assumes differently per
                # pipeline branch — every pixel value is systematically
                # shifted and VMAF cratered scores on a visually-correct
                # encode. `scale=in_range=auto:out_range=tv` auto-detects
                # the input range (honouring the stream tag) and forces
                # output to limited range on BOTH sides, so they definitely
                # agree.
                range_clause = "scale=in_range=auto:out_range=tv:flags=bicubic"

                # Assemble per-stream normalisation pipeline:
                #   range fix → fps fix → pixel format → scale2ref → libvmaf
                ref_chain = [range_clause]
                dist_chain = [range_clause]
                if fps_clause:
                    ref_chain.append(fps_clause)
                    dist_chain.append(fps_clause)
                ref_chain.append("format=yuv420p")
                dist_chain.append("format=yuv420p")
                ref_pipeline = ",".join(ref_chain)
                dist_pipeline = ",".join(dist_chain)

                # Total frame count for the sampled window — used to map the
                # frame=NN progress lines to 0–100%. Prefer the real source
                # fps from the probe, fall back to 24 only if the probe
                # failed. Previously hardcoded to 24fps, which underestimated
                # total frames on 29.97/30fps content and made the progress
                # bar peg at 99% long before the run actually finished.
                vmaf_fps_for_progress = (src_info.get("fps") or dst_info.get("fps") or 24.0)
                # Track every JSON path we generate so the cleanup pass at the
                # end of the block can remove all of them, not just the primary.
                vmaf_json_paths_to_cleanup: list[Path] = [vmaf_json_path]

                # Primary VMAF pass — runs at the configured seek point. The
                # entire run (ffmpeg subprocess, progress streaming, JSON
                # parse) is delegated to the module-level helper; we get back
                # a result dict with score / min / max / harmonic_mean / error.
                print(
                    f"[CONVERT] Running VMAF analysis ({vmaf_duration:.0f}s sample at "
                    f"{vmaf_seek:.0f}s, target_fps={target_fps or 'n/a'})...",
                    flush=True,
                )
                result_primary = await _run_libvmaf_pass(
                    input_path=input_path,
                    temp_path=temp_path,
                    seek=vmaf_seek,
                    duration=vmaf_duration,
                    ref_pipeline=ref_pipeline,
                    dist_pipeline=dist_pipeline,
                    json_path=vmaf_json_path,
                    fps_for_progress=vmaf_fps_for_progress,
                    progress_callback=progress_callback,
                    step_label="VMAF analysis",
                )
                vmaf_results = [result_primary]

                # Bimodal-retry: if libvmaf desynced mid-window (the 0/100
                # split we kept seeing on otherwise-fine encodes), the
                # primary's score is bogus. Retry at a different seek so a
                # different region of the file is analysed; if that one comes
                # back clean we trust it. The retry only fires when
                # _is_bimodal_vmaf returns true, so well-behaved encodes never
                # pay the extra ~30s. Skipped on very short files where there
                # isn't enough headroom to seek somewhere meaningfully
                # different (60s minimum gap between the two windows).
                if _is_bimodal_vmaf(result_primary) and duration > 90 and vmaf_duration > 0:
                    # Pick an alternate seek that's at least 60s away from the
                    # primary. Prefer 66% of duration; if primary already sat
                    # past mid-file, go back to 33% instead. Clamp so
                    # `seek + duration` stays within the file.
                    alt_pct = 0.66 if vmaf_seek < duration * 0.5 else 0.33
                    alt_seek = max(0.0, min(duration - vmaf_duration, duration * alt_pct))
                    if abs(alt_seek - vmaf_seek) >= 60:
                        _vmaf_id_alt = f"{_safe_stem}_{_uuid.uuid4().hex[:8]}"
                        vmaf_json_path_alt = vmaf_dir / f"{_vmaf_id_alt}_vmaf.json"
                        vmaf_json_paths_to_cleanup.append(vmaf_json_path_alt)
                        print(
                            f"[CONVERT] Primary VMAF run looked bimodal "
                            f"(min={result_primary.get('min'):.1f}, max={result_primary.get('max'):.1f}) — "
                            f"retrying at {alt_seek:.0f}s to rule out a measurement desync.",
                            flush=True,
                        )
                        result_alt = await _run_libvmaf_pass(
                            input_path=input_path,
                            temp_path=temp_path,
                            seek=alt_seek,
                            duration=vmaf_duration,
                            ref_pipeline=ref_pipeline,
                            dist_pipeline=dist_pipeline,
                            json_path=vmaf_json_path_alt,
                            fps_for_progress=vmaf_fps_for_progress,
                            progress_callback=progress_callback,
                            step_label="VMAF retry",
                        )
                        vmaf_results.append(result_alt)

                # Pick the run with the highest score. If the encode is
                # genuinely fine, both runs converge on a near-perfect mean;
                # if one desynced and the other didn't, the clean one wins.
                # Errored runs (no score) are filtered out so a transient
                # ffmpeg crash on the retry doesn't drag the primary down.
                scored_runs = [r for r in vmaf_results if r.get("score") is not None]
                if scored_runs:
                    best = max(scored_runs, key=lambda r: r["score"])
                    vmaf_score = best["score"]
                    _min = best.get("min")
                    _max = best.get("max")
                    _hm = best.get("harmonic_mean")
                    extra = []
                    if _min is not None: extra.append(f"min={_min:.1f}")
                    if _max is not None: extra.append(f"max={_max:.1f}")
                    if _hm is not None: extra.append(f"harmonic_mean={_hm:.1f}")
                    suffix = (" [" + " ".join(extra) + "]") if extra else ""
                    seek_suffix = f" (seek={best.get('seek', vmaf_seek):.0f}s)" if len(vmaf_results) > 1 else ""
                    print(f"[CONVERT] VMAF score: {vmaf_score}{suffix}{seek_suffix}", flush=True)

                    # If even the BEST run was bimodal, every window we tried
                    # had a desync. Persist the score (it's the user's best
                    # estimate of perceptual quality) but flag it so the UI
                    # can show "measurement-suspect" rather than a misleading
                    # "Poor" tier. The cross-check below will run regardless.
                    if _is_bimodal_vmaf(best):
                        vmaf_uncertain = True
                        print(
                            "[CONVERT] All VMAF passes returned bimodal distributions — "
                            "flagging score as measurement-uncertain. The encode is "
                            "almost certainly visually fine; consider re-measuring "
                            "from Settings or trusting the SSIM/PSNR cross-check below.",
                            flush=True,
                        )
                    elif _min is not None and _max is not None and vmaf_score < 80 and _max >= 90:
                        # Soft bimodal warning (didn't trip the retry threshold
                        # but still a wide spread) — keep the existing log so
                        # users see "manual spot-check recommended" guidance.
                        print(
                            "[CONVERT] VMAF distribution looks bimodal "
                            f"(mean {vmaf_score}, max {_max:.1f}). "
                            "This often indicates temporal/resolution "
                            "misalignment rather than a real quality "
                            "problem — manual spot-check recommended.",
                            flush=True,
                        )
                else:
                    # No run produced a score. Surface the first error for
                    # the user (and the rest in the log).
                    error_tails = [r.get("error") for r in vmaf_results if r.get("error")]
                    vmaf_error = error_tails[0] if error_tails else "VMAF returned no score"
                    print(f"[CONVERT] VMAF failed ({vmaf_error})", flush=True)

                # Diagnostic cross-check: when VMAF reports a low score (or
                # when we flagged the result as uncertain), re-measure the
                # same window with SSIM and PSNR. All three metrics work from
                # the same pixel data but with very different algorithms —
                # if VMAF says "poor" but SSIM is >0.98 and PSNR is >40 dB,
                # the encode is actually fine and VMAF is the measurement
                # artefact (common on animation / flat-coloured content,
                # which is outside VMAF's training distribution). Only runs
                # on suspicious scores so it doesn't slow down the 99% case.
                if vmaf_score is not None and (vmaf_score < 80 or vmaf_uncertain):
                    try:
                        import re as _re
                        # Cross-check sample: use the same window the BEST
                        # VMAF run analysed — that way SSIM/PSNR are
                        # measuring exactly what VMAF was scoring, so we
                        # can compare apples-to-apples when deciding
                        # whether VMAF was wrong.
                        xcheck_seek = (
                            scored_runs and max(scored_runs, key=lambda r: r["score"]).get("seek", vmaf_seek)
                            or vmaf_seek
                        )
                        xcheck_dur = min(30.0, vmaf_duration) if vmaf_duration > 0 else 30.0
                        xcheck_filter = (
                            f"[0:v]{ref_pipeline}[ref_x];"
                            f"[1:v]{dist_pipeline}[dist_x];"
                            f"[dist_x][ref_x]scale2ref=flags=bicubic[dx][rx];"
                            f"[dx][rx]ssim;[dx][rx]psnr"
                        )
                        # Dedicated progress phase for the cross-check
                        # — different step label so the UI shows this
                        # is a separate stage, and progress resets
                        # from 0 rather than jumping back from 99%.
                        if progress_callback:
                            await progress_callback(
                                progress=0, fps=0, eta_seconds=None,
                                step="Quality cross-check",
                            )
                        xc_total_frames = max(1, int(xcheck_dur * vmaf_fps_for_progress))
                        # ffmpeg's `ssim` and `psnr` filters print
                        # results to stderr on exit. `-stats` gives
                        # us frame progress lines alongside.
                        xcheck_cmd = [
                            "ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-stats",
                            "-ss", f"{xcheck_seek:.3f}", "-i", input_path,
                            "-ss", f"{xcheck_seek:.3f}", "-i", temp_path,
                            "-t", f"{xcheck_dur:.3f}",
                            "-filter_complex", xcheck_filter,
                            "-f", "null", "-",
                        ]
                        xc_proc = await asyncio.create_subprocess_exec(
                            *xcheck_cmd,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        # Stream stderr so we can emit progress as
                        # the cross-check runs, rather than blocking
                        # in `communicate()` for up to 2 minutes with
                        # a dead progress bar.
                        xc_buf = ""
                        xc_stderr_chunks: list[str] = []
                        _xc_start = time.monotonic()
                        while True:
                            xc_chunk = await xc_proc.stderr.read(4096)
                            if not xc_chunk:
                                break
                            xc_dec = xc_chunk.decode(errors="replace")
                            xc_stderr_chunks.append(xc_dec)
                            xc_buf += xc_dec
                            while "\r" in xc_buf or "\n" in xc_buf:
                                r_pos = xc_buf.find("\r")
                                n_pos = xc_buf.find("\n")
                                if r_pos == -1: pos = n_pos
                                elif n_pos == -1: pos = r_pos
                                else: pos = min(r_pos, n_pos)
                                xc_line = xc_buf[:pos].strip()
                                xc_buf = xc_buf[pos + 1:]
                                if not xc_line or not progress_callback:
                                    continue
                                fm2 = _re.search(r'frame=\s*(\d+)', xc_line)
                                if not fm2:
                                    continue
                                xc_frame = int(fm2.group(1))
                                xc_pct = min(99.0, xc_frame / xc_total_frames * 100)
                                fps_m2 = _re.search(r'fps=\s*([\d.]+)', xc_line)
                                xc_analyse_fps = float(fps_m2.group(1)) if fps_m2 else 0.0
                                xc_elapsed = time.monotonic() - _xc_start
                                xc_eta = None
                                if xc_pct > 1.0:
                                    xc_eta = int(xc_elapsed / (xc_pct / 100) * (1 - xc_pct / 100))
                                await progress_callback(
                                    progress=xc_pct,
                                    fps=xc_analyse_fps,
                                    eta_seconds=xc_eta,
                                    step="Quality cross-check",
                                )
                        await asyncio.wait_for(xc_proc.wait(), timeout=120)
                        xc_text = "".join(xc_stderr_chunks)
                        ssim_m = _re.search(r"SSIM[^A]*All:\s*([\d.]+)", xc_text)
                        psnr_m = _re.search(r"PSNR[^a]*average:\s*([\d.]+)", xc_text)
                        ssim_v = float(ssim_m.group(1)) if ssim_m else None
                        psnr_v = float(psnr_m.group(1)) if psnr_m else None
                        parts = []
                        if ssim_v is not None: parts.append(f"SSIM={ssim_v:.4f}")
                        if psnr_v is not None: parts.append(f"PSNR={psnr_v:.2f}dB")
                        if parts:
                            verdict = ""
                            # SSIM > 0.98 or PSNR > 40 dB = transparent/
                            # near-transparent quality. If VMAF disagrees
                            # with both, it's almost certainly wrong.
                            if ((ssim_v is not None and ssim_v >= 0.98) or
                                (psnr_v is not None and psnr_v >= 40.0)):
                                verdict = (
                                    " → SSIM/PSNR say the encode is "
                                    "actually fine; VMAF score is a "
                                    "measurement artefact (common on "
                                    "animation / flat-coloured content)."
                                )
                            print(
                                f"[CONVERT] Quality cross-check ({xcheck_dur:.0f}s sample): "
                                + ", ".join(parts) + verdict,
                                flush=True,
                            )
                        else:
                            print(
                                "[CONVERT] Quality cross-check produced no "
                                "SSIM/PSNR output — skipping.",
                                flush=True,
                            )
                    except Exception as xc_exc:
                        print(f"[CONVERT] Quality cross-check failed: {xc_exc}", flush=True)
                # Clean up every JSON file the helper produced (primary +
                # any retry). Old code only removed the primary, leaving
                # /tmp full of stale `*_vmaf.json` files after a few months
                # of bimodal retries.
                for _jp in vmaf_json_paths_to_cleanup:
                    try:
                        Path(_jp).unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                vmaf_error = "libvmaf not available"
                print(f"[CONVERT] VMAF skipped — {vmaf_error}", flush=True)
        except Exception as vmaf_exc:
            import traceback as _tb
            vmaf_error = f"{type(vmaf_exc).__name__}: {vmaf_exc}"
            print(f"[CONVERT] VMAF analysis failed: {vmaf_error}\n{_tb.format_exc()}", flush=True)
    else:
        print(
            f"[CONVERT] VMAF skipped — vmaf_analysis_enabled is false "
            f"(raw setting={_vmaf_setting!r})",
            flush=True,
        )

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
        if prestrip_path:
            try: Path(prestrip_path).unlink(missing_ok=True)
            except OSError: pass
        return {
            "success": True,              # the encode process worked; we just didn't accept the output
            "output_path": input_path,    # original untouched
            "space_saved": 0,
            "error": None,
            "vmaf_score": vmaf_score,
            "vmaf_uncertain": vmaf_uncertain,
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
            # Refuse if the target path is a symlink — an attacker who can
            # place a symlink in the backup folder named like the source
            # file could otherwise redirect the rename to anywhere the
            # container user can write (e.g. /etc/cron.d/root). Explicit
            # check before rename closes the gap since Path.rename happily
            # follows a pre-existing symlink on Linux.
            if backup_path.is_symlink():
                raise OSError(
                    f"Refusing to rename into backup path — destination is a symlink: {backup_path}"
                )
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
        # Same symlink check for the final output rename. The common case
        # is benign (final_path doesn't exist at all) but defense-in-depth
        # catches the case where an attacker placed a symlink that the
        # converter would follow.
        _final = Path(final_path)
        if _final.is_symlink():
            raise OSError(
                f"Refusing to overwrite symlink at final output path: {final_path}"
            )
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

    # Clean up the pre-strip temp file (if we did a two-pass run). The main
    # encode now references temp_path → final_path; the stripped intermediate
    # has served its purpose.
    if prestrip_path:
        try:
            Path(prestrip_path).unlink(missing_ok=True)
        except OSError as exc:
            print(f"[CONVERT] Could not remove pre-strip temp {prestrip_path}: {exc}", flush=True)

    encode_time = time.monotonic() - encode_start_time
    return {
        "success": True,
        "output_path": final_path,
        "space_saved": space_saved,
        "error": None,
        "backup_path": result_backup_path,
        "vmaf_score": vmaf_score,
        "vmaf_error": vmaf_error,
        "vmaf_uncertain": vmaf_uncertain,
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
