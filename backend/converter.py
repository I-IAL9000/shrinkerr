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
    # v0.5.7: hardware decode toggles. Native pairs (encoder + matching
    # decoder) default on — frames stay on the device, no PCIe transfer.
    # libx265 + NVDEC defaults off because it requires GPU→CPU readback
    # per frame, only a win when the CPU is the bottleneck. Saved as
    # 'true'/'false' strings in DB by routes/settings.py; coerced here.
    ("nvenc_hw_decode",                  True,      _str_to_bool),
    ("qsv_hw_decode",                    True,      _str_to_bool),
    ("vaapi_hw_decode",                  True,      _str_to_bool),
    ("libx265_use_nvdec",                False,     _str_to_bool),
    # v0.5.9: NVENC bit-depth choice. String "10bit" (default) / "8bit" /
    # "auto". The "auto" path probes source pix_fmt and resolves per-job
    # in convert_file. 10-bit needs Pascal+; 8-bit is Maxwell-compatible.
    ("nvenc_bit_depth",                  "10bit",   str),
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
    # v0.5.6: cap ffmpeg's thread count via `-threads N`. 0 = ffmpeg auto
    # (uses all available cores, pre-v0.5.6 behaviour). 1-16 = explicit cap.
    ffmpeg_threads: int = 0,
    # v0.5.7: hardware decode wiring. Caller sets these from live
    # settings + per-encoder availability + per-source-codec gating.
    # When False, no -hwaccel flag is emitted and the filter chain
    # stays in its pre-v0.5.7 software-decode form.
    use_hw_decode: bool = False,
    # 'cuda' / 'qsv' / 'vaapi' — what backend to use when use_hw_decode
    # is True. Determined by caller based on encoder + libx265_use_nvdec.
    hw_decode_backend: str | None = None,
    # When True, frames stay on the device (decoder output_format =
    # backend's native surface). When False (libx265+NVDEC mixed mode),
    # emit `-hwaccel cuda` without `-hwaccel_output_format` so frames
    # are downloaded to CPU memory for libx265.
    hw_decode_keeps_on_device: bool = True,
    # Used for logging only — the codec gating happened in the caller.
    source_codec: str | None = None,
    # v0.5.9: NVENC output bit depth. "10bit" emits main10/p010le (the
    # pre-v0.5.9 hardcoded behaviour); "8bit" emits main/nv12 (Maxwell-
    # compatible, smaller files on most sources). The caller resolves
    # "auto" against the source pix_fmt before reaching this function,
    # so by the time we see it the value is one of "10bit" / "8bit".
    # No-op for non-NVENC encoders.
    nvenc_bit_depth: str = "10bit",
    # v0.7.0: extra args to splice BEFORE the input `-i`. Used for DVD
    # ISO routing (`-f dvdvideo`) where the input path is a bare .iso
    # filename (no protocol prefix) so the demuxer needs an explicit
    # format hint. Also forces the disc-input analysis window since the
    # bare .iso path won't match the `concat:` / `bluray:` startswith
    # check below.
    pre_input_args: list[str] | None = None,
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
    # v0.5.6: optional global thread cap. Emitted as a top-level option
    # before any input so it scopes ffmpeg's overall worker pool. Default
    # 0 means "ffmpeg decides" (use all cores) — historical behaviour.
    # Values 1-2 are useful when parallel_jobs > 1 on older CPUs to avoid
    # 16 threads fighting for 8 cores. Has marginal effect on HW encoders
    # (NVENC/QSV/VAAPI) since the GPU does the heavy lifting — software
    # encode (libx265) is where this matters.
    if ffmpeg_threads and ffmpeg_threads > 0:
        cmd += ["-threads", str(ffmpeg_threads)]
    # v0.5.7: HW decode pre-input flags. Must come before the VAAPI/QSV
    # init_hw_device calls because those refer to the device that will
    # be set up by -hwaccel.
    if use_hw_decode and hw_decode_backend:
        cmd += ["-hwaccel", hw_decode_backend]
        if hw_decode_keeps_on_device:
            cmd += ["-hwaccel_output_format", hw_decode_backend]
        # else: libx265+NVDEC mixed mode — frames downloaded to CPU

        # v0.5.14: NVDEC has a 32-surface hardware limit. The H.264
        # decoder allocates surfaces proportional to its thread count,
        # so on a 12-16-thread host with a dense-ref-frame x264 source
        # ffmpeg's default (nproc) pushes the surface request past 32
        # and CUVID errors with `CUDA_ERROR_INVALID_VALUE`:
        #   "Using more than 32 (34) decode surfaces might cause nvdec
        #    to fail. Try lowering the amount of threads. Using 16."
        # Pin decoder threads to 1 here — the GPU does the actual
        # decode, CPU threads just feed it. This is a per-input scope
        # (it's between the hwaccel flags and `-i input.mkv`) so the
        # encoder side still uses the user's `ffmpeg_threads` setting
        # via the post-encoder `-threads N` from v0.5.9. Applies to
        # NVDEC only — QSV / VAAPI have larger surface budgets and
        # don't hit this limit at realistic thread counts.
        if hw_decode_backend == "cuda":
            cmd += ["-threads", "1"]
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
    # v0.6.2: disc-protocol inputs (`concat:VTS_01_1.VOB|...` for DVD,
    # `bluray:/path` for Blu-ray) need a deeper analysis window for
    # ffmpeg to lock onto the streams and compute duration before the
    # encode pipeline starts. Detected from the input string so this
    # function stays decoupled from disc_type.
    # v0.7.0: pre_input_args (e.g. ["-f", "dvdvideo"] for DVD ISO) implies
    # a disc input — also force the deeper analysis window since the bare
    # .iso path won't match the protocol prefix check below.
    if pre_input_args or input_path.startswith("concat:") or input_path.startswith("bluray:"):
        cmd += ["-analyzeduration", "200M", "-probesize", "200M"]
    if pre_input_args:
        cmd += list(pre_input_args)
    cmd += ["-i", input_path]

    # Add external subtitle files as additional inputs (input 1, 2, 3, ...)
    ext_subs = external_subtitle_files or []
    for es in ext_subs:
        cmd += ["-i", es["path"]]

    # v0.7.12: disable ffmpeg's auto-inserted scale filter on the
    # NVDEC-native CUDA path. When the decoder's video parameters
    # reconfigure mid-stream (a "hwaccel changed" event on certain
    # x264 WEB-DLs with SAR drift or embedded cover art), ffmpeg
    # tries to splice an `auto_scale_0` CPU filter between the demuxer
    # output and our explicit `scale_cuda` filter to bridge the new
    # format — but auto_scale_0 can't accept cuda(tv, unknown) frames,
    # so the encode dies mid-stream after gigabytes of good output:
    #   "Impossible to convert between the formats supported by the
    #    filter 'Parsed_scale_cuda_0' and the filter 'auto_scale_0'"
    # Our explicit `scale_cuda=format=...` already does all the format
    # alignment hevc_nvenc needs. -noautoscale is an OUTPUT option, so
    # it MUST go after all -i inputs (v0.7.12 first cut wrongly placed
    # it before -i, which made ffmpeg reject it as an input option:
    # exit 234 "Error parsing options for input file"). Scoped to the
    # CUDA-native path so QSV/VAAPI/CPU paths are untouched.
    if (
        use_hw_decode
        and hw_decode_backend == "cuda"
        and hw_decode_keeps_on_device
    ):
        cmd += ["-noautoscale"]

    # Resolution scaling + decode/encode filter chain wiring.
    # v0.5.7: when HW decode is on, scale on the device matching the
    # decoder backend; when off, retain pre-v0.5.7 software-scale path.
    scale = RESOLUTION_MAP.get(target_resolution)
    if use_hw_decode and hw_decode_keeps_on_device:
        # Native pair: frames stay on device. Scale with the matching
        # device-native scaler; no hwupload needed.
        if hw_decode_backend == "cuda":
            # NVENC + NVDEC.
            #
            # The `format=` part is REQUIRED even when the user didn't
            # pick a target resolution. Why: NVDEC outputs nv12 CUDA
            # surfaces for 8-bit sources and p010 surfaces for 10-bit
            # sources, while hevc_nvenc with `-pix_fmt p010le` expects
            # p010 input and with `-pix_fmt nv12` expects nv12.
            # ffmpeg's auto-format converter can't bridge CUDA↔CPU
            # pix_fmts, so without an explicit on-GPU format conversion
            # the encoder bombs with:
            #   "Impossible to convert between the formats supported by
            #    the filter 'Parsed_null_0' and the filter 'auto_scale_0'"
            # scale_cuda=format=<target> does the bit-depth alignment
            # on-GPU and is a no-op when the source surface already
            # matches the target. v0.5.7→v0.5.8 fixed the always-p010le
            # case; v0.5.9 made the target depend on nvenc_bit_depth.
            _cuda_target_fmt = "p010le" if nvenc_bit_depth == "10bit" else "nv12"
            if scale:
                cmd += ["-vf", f"scale_cuda={scale}:format={_cuda_target_fmt}"]
            else:
                cmd += ["-vf", f"scale_cuda=format={_cuda_target_fmt}"]
        elif hw_decode_backend == "qsv":
            # QSV + QSV decode. scale_qsv keeps frames on iGPU.
            if scale:
                cmd += ["-vf", f"scale_qsv={scale}"]
        elif hw_decode_backend == "vaapi":
            # VAAPI + VAAPI decode. scale_vaapi keeps frames on DRM device.
            # Still need format=nv12 to match what hevc_vaapi expects.
            if scale:
                cmd += ["-vf", f"scale_vaapi={scale},format=nv12"]
            else:
                cmd += ["-vf", "format=nv12"]
    elif use_hw_decode and not hw_decode_keeps_on_device:
        # libx265 + NVDEC mixed mode: frames downloaded to CPU after
        # decode. Use software scale on the CPU-side frames.
        if scale:
            cmd += ["-vf", f"hwdownload,format=nv12,scale={scale}"]
        else:
            cmd += ["-vf", "hwdownload,format=nv12"]
    else:
        # Software decode — pre-v0.5.7 behaviour.
        if encoder == "vaapi":
            # VAAPI software-decoded input needs hwupload to reach the encoder.
            if scale:
                cmd += ["-vf", f"scale={scale},format=nv12,hwupload"]
            else:
                cmd += ["-vf", "format=nv12,hwupload"]
        elif encoder == "qsv":
            # QSV software-decoded input: ffmpeg uploads internally.
            if scale:
                cmd += ["-vf", f"scale={scale}"]
        elif scale:
            # NVENC / libx265 software scale.
            cmd += ["-vf", f"scale={scale}"]
    # v0.5.7: log the decode/encode pipeline so debugging "is my GPU
    # being used" doesn't require reading ffmpeg's verbose output.
    if use_hw_decode and hw_decode_backend:
        if hw_decode_keeps_on_device:
            _decode_label = f"{hw_decode_backend.upper()} (on-device)"
        else:
            _decode_label = f"{hw_decode_backend.upper()} + CPU readback"
    else:
        _decode_label = f"software ({source_codec or 'unknown'})"
    print(
        f"[CONVERT] Decode: {_decode_label} → Encode: {encoder} "
        f"(source codec: {source_codec or 'unknown'})",
        flush=True,
    )

    if encoder == "nvenc":
        # v0.5.9: profile + pix_fmt are now bit-depth dependent.
        # 10-bit (Pascal+): main10 / p010le — best quality, larger files
        #                   on some sources, blocks Maxwell GTX 9xx silicon
        # 8-bit (Maxwell+): main / nv12 — Maxwell-compatible, smaller files
        #                   on most sources, faster encode, fully sufficient
        #                   for 8-bit source material
        # The caller resolved "auto" against the source pix_fmt before we
        # got here, so by now it's one of "10bit" / "8bit".
        if nvenc_bit_depth == "8bit":
            _nvenc_profile = "main"
            _nvenc_pix_fmt = "nv12"
        else:
            _nvenc_profile = "main10"
            _nvenc_pix_fmt = "p010le"
        cmd += [
            "-c:v", "hevc_nvenc",
            "-preset", nvenc_preset,
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(cq),
            "-b:v", "0",
            "-profile:v", _nvenc_profile,
        ]
        # v0.5.10: emit -pix_fmt ONLY when frames live in CPU memory
        # (software decode path). With HW decode keeping frames on the
        # GPU, `scale_cuda=format=X` already dictates the surface format
        # — adding `-pix_fmt X` here makes ffmpeg believe the encoder
        # expects X in CPU memory, so it tries to auto-insert a scale
        # filter to convert cuda(X) → X (CPU), which it can't do without
        # an explicit hwdownload. That's exactly the error v0.5.8/v0.5.9
        # users hit:
        #   "Impossible to convert between the formats supported by the
        #    filter 'Parsed_scale_cuda_0' and the filter 'auto_scale_0'"
        # QSV/VAAPI never had -pix_fmt set, so they weren't affected.
        _nvenc_native_hw = (
            use_hw_decode
            and hw_decode_keeps_on_device
            and hw_decode_backend == "cuda"
        )
        if not _nvenc_native_hw:
            cmd += ["-pix_fmt", _nvenc_pix_fmt]
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

    # v0.5.9: emit `-threads N` AFTER the encoder spec too. ffmpeg's
    # `-threads` is per-codec-context: the pre-input copy (v0.5.6) caps
    # decoder threads, but software encoders (libx265 here) need the cap
    # on the encoder side to actually take effect. NVENC/QSV/VAAPI
    # ignore -threads (the GPU does the heavy lifting), so this is a
    # no-op redundancy on the hardware encode paths and a real cap on
    # libx265 — both desirable. Per the GitHub feature requester's
    # observation: "ffmpeg will only apply it for that processing
    # portion" so it has to be applied at every boundary you want
    # capped.
    if ffmpeg_threads and ffmpeg_threads > 0:
        cmd += ["-threads", str(ffmpeg_threads)]

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
            # SRT and ASS/SSA are natively supported by MKV — byte-copy
            # them. v0.4.9+: pre-fix this path used `-c:s srt` for SRT
            # sources, which forced a decode→encode roundtrip through
            # ffmpeg's strict UTF-8 SRT encoder. SRT files in the wild
            # are frequently Windows-1252 / ISO-8859-1, especially for
            # non-English releases — those failed with "Invalid UTF-8
            # in decoded subtitles text". Copying the bytes through
            # avoids the encoder entirely; most MKV players handle
            # mixed-encoding SRT fine via charset auto-detection.
            if codec in ("subrip", "srt", "ass", "ssa"):
                cmd += [f"-c:s:{out_sub_idx}", "copy"]
            elif codec in ("webvtt",):
                # WebVTT isn't a native MKV subtitle codec; keep the
                # convert-to-srt path. Same UTF-8 strictness applies but
                # WebVTT is already required to be UTF-8 by spec, so
                # the validator should pass for any well-formed source.
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
        # v0.5.19: catch-all `-c:s copy` for any unset external-sub
        # output streams. Each ext_subs entry maps with `-map 1:s` (all
        # streams in the input), but a VobSub `.idx` typically carries
        # multiple language streams in one file. The per-stream codec
        # spec above (`-c:s:N copy`) only sets ONE output index per
        # entry; the additional streams pulled in by `-map 1:s` had no
        # codec setting and defaulted to matroska's text default (ass).
        # That triggered "Subtitle encoding currently only possible
        # from text to text or bitmap to bitmap" when ffmpeg tried to
        # transcode dvdsub bitmap → ass text. The unindexed `-c:s
        # copy` sets the default for ALL output sub streams; the
        # per-stream specifiers above still win for the streams they
        # name (so the webvtt → srt conversion path is preserved).
        cmd += ["-c:s", "copy"]
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


# v0.5.7: HW decode codec support tables. Conservative — only codecs
# we have strong evidence work on current driver/SDK versions. When the
# table says False, we silently fall back to software decode with a
# worker log line; failing the job would be worse UX than a slower decode.
#
# NVDEC: per NVIDIA Video Codec SDK 12.x decode matrix.
# QSV: per Intel Media Driver decode capabilities (gen-dependent but
#      the listed codecs work on Gen9+, which is everything Shrinkerr
#      supports as an encoder host).
# VAAPI: per Mesa VA-API + Intel iHD intersection.
_NVDEC_SUPPORTED = frozenset({
    "h264", "hevc", "h265",
    "vp9", "av1", "av01",
    "mpeg2video", "mpeg2", "mpeg4", "vc1", "wmv3",
})
_QSV_DECODE_SUPPORTED = frozenset({
    "h264", "hevc", "h265",
    "vp9", "av1", "av01",
    "mpeg2video", "mjpeg",
})
_VAAPI_DECODE_SUPPORTED = frozenset({
    "h264", "hevc", "h265",
    "vp9", "av1", "av01",
    "mpeg2video", "vc1", "wmv3",
})


def hw_decode_supports(decoder: str, source_codec: str | None,
                       source_pix_fmt: str | None = None) -> bool:
    """True if `decoder` ('cuda'/'qsv'/'vaapi') can hardware-decode
    `source_codec` (lowercase ffprobe codec name). Returns False when
    source_codec is None/empty so probe failures fall back to software
    rather than crashing the cmd builder.

    v0.5.26: also gates on `source_pix_fmt`. **10-bit H.264 (Hi10p,
    yuv420p10le)** is universally unreliable across consumer hardware
    decoders — Pascal NVDEC and older lack 10-bit H.264 entirely
    (Turing GP104+ added it), QSV decode is 8-bit-only for H.264,
    and most VAAPI drivers don't support it either. When the source
    is Hi10p, ffmpeg silently falls back to software decode but our
    `scale_cuda=format=p010le` (or `scale_qsv` / `scale_vaapi`) filter
    still expects HW frames and the encoder bombs with
    "Impossible to convert between the formats supported by the filter
    'graph -1 input from stream 0:0' and the filter 'auto_scale_0'".
    Excluding the case at the gate makes the cmd builder emit pure
    software decode for these files. 10-bit HEVC is unaffected —
    Pascal+ NVDEC, Gen11+ QSV, and most VAAPI drivers handle it fine."""
    if not source_codec:
        return False
    c = source_codec.lower()
    pf = (source_pix_fmt or "").lower()
    # 10-bit H.264 exclusion (universal across the three HW decoders).
    if c == "h264" and ("p10" in pf or "10le" in pf or "p12" in pf or "12le" in pf):
        return False
    table = {
        "cuda": _NVDEC_SUPPORTED,
        "qsv": _QSV_DECODE_SUPPORTED,
        "vaapi": _VAAPI_DECODE_SUPPORTED,
    }
    return c in table.get(decoder, frozenset())


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
    """Rewrite source-codec tags in `filename` for the output encoder.

    The target label depends on `encoder` — see `_hevc_tag_for_encoder`.
    Keeps the old behaviour (always `x265`) when `encoder` is None for
    back-compat with callers that haven't been updated yet.

    Covers every source family Shrinkerr offers in Settings → Convert From:
    h264/x264/AVC, MPEG-2/MPEG2/MPEG, MPEG-4/XviD/DivX/DX50, VC-1/WMV,
    VP9. Without this, a DVD MPEG-2 file like
    `36 Fillette (1988) 576p AC3 2.0 MPEG.mkv` got converted to HEVC but
    kept the misleading `MPEG` in its filename. v0.5.5 broadened the patterns.
    """
    target = _hevc_tag_for_encoder(encoder) if encoder is not None else "x265"
    # H.264 / x264 / AVC. `[._-]?` allows scene-tag variants with a
    # literal separator between the letter and the digits — `H.264`
    # (Amazon Prime convention), `h-264`, `h_264`. Pre-v0.3.102 the
    # patterns required adjacency (`\bh264\b`), so a file like
    # `…H.264-NTb.mkv` got converted to HEVC but kept the misleading
    # H.264 in its filename.
    result = re.sub(r'\bx[._-]?264\b', target, filename, flags=re.IGNORECASE)
    result = re.sub(r'\bh[._-]?264\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bAVC\b', target, result)
    # MPEG-2 / MPEG2 / MPEG (DVD rips). Match the digits-bearing forms
    # first so `MPEG-2` doesn't get partially consumed by the bare `MPEG`
    # pattern. Bare `MPEG` is intentionally last and case-sensitive so
    # we don't munge unrelated words like "Stomping" or filenames that
    # happen to share the substring.
    result = re.sub(r'\bMPEG[._\-\s]?2\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bMPEG\b', target, result)
    # MPEG-4 Part 2 / XviD / DivX / DX50.
    result = re.sub(r'\bMPEG[._\-\s]?4\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bXviD\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bDivX\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bDX50\b', target, result, flags=re.IGNORECASE)
    # VC-1 / WMV (Windows Media). Wmv9 / VC1 / VC-1 etc.
    result = re.sub(r'\bVC[._\-\s]?1\b', target, result, flags=re.IGNORECASE)
    result = re.sub(r'\bWMV[0-9]?\b', target, result, flags=re.IGNORECASE)
    # VP9 (YouTube/WebM rips occasionally hit this path).
    result = re.sub(r'\bVP[._\-\s]?9\b', target, result, flags=re.IGNORECASE)
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


def _build_audio_conversion_summary(
    probe_audio_tracks: list,
    global_audio_codec: str,
    lossless_conversion: dict | None,
) -> list[str]:
    """Return display-name list of source audio codecs that were
    re-encoded by this conversion. Empty when nothing was re-encoded.
    Used by the Completed-tab job report to show e.g.
    "DTS-HD MA → EAC3 640kb". v0.4.7+.

    Two re-encode triggers:
      * Global `audio_codec` is not "copy" — every kept audio track
        gets re-encoded to that codec.
      * `lossless_conversion` is set — only LOSSLESS tracks (TrueHD /
        DTS-HD MA / FLAC / PCM / etc.) get re-encoded; lossy tracks
        copy through unchanged.

    Sources are deduped + sorted for stable display. Returns the source
    side; the target codec/bitrate is reported separately in
    encoding_stats so the frontend can render the arrow.
    """
    if not probe_audio_tracks:
        return []
    sources: set[str] = set()
    if global_audio_codec and global_audio_codec.lower() != "copy":
        # Every audio track will be re-encoded.
        for t in probe_audio_tracks:
            name = get_audio_display_name(t.get("codec", ""), t.get("profile", ""))
            if name:
                sources.add(name)
    elif lossless_conversion:
        # Only the lossless tracks get re-encoded; collect their pretty names.
        for t in probe_audio_tracks:
            codec = t.get("codec", "")
            profile = t.get("profile", "")
            if is_lossless_audio(codec, profile):
                name = get_audio_display_name(codec, profile)
                if name:
                    sources.add(name)
    return sorted(sources)


def rename_source_quality_in_filename(filename: str) -> str:
    """Normalize source-quality tags in `filename` after conversion.

    A full Blu-ray disc rip (BR-DISK) becomes a Bluray rip once it's
    been re-encoded; a full DVD (DVD-R / DVD5 / DVD9) becomes a
    DVDRip. The tag in the filename should reflect what the file
    actually is.

    Case-insensitive matching; output uses the canonical scene-style
    forms ("Bluray" and "DVDRip"). Leaves already-encoded source tags
    (Bluray / BDRip / DVDRip / WEB-DL / HDTV / WEBRip / etc.) alone.

    Examples (post-conversion filename normalization):
      "Movie.2020.1080p.BR-DISK.x264-GRP.mkv" → "Movie.2020.1080p.Bluray.x265-GRP.mkv"
      "Show.S01.DVD-R.AC3.mkv"                → "Show.S01.DVDRip.AC3.mkv"
      "Foo.BD50.x264.mkv"                     → "Foo.Bluray.x265.mkv"

    v0.5.18+.
    """
    result = filename
    # Blu-ray disc tier → "Bluray"
    #   BR-DISK / BRDISK / BR.DISK / BR_DISK
    result = re.sub(r'\bBR[\s._-]?DISK\b', 'Bluray', result, flags=re.IGNORECASE)
    #   BD25 / BD50 / BD100 (single/dual/triple-layer disc-size tags).
    #   Bare "BD" is intentionally NOT matched — it's ambiguous and
    #   appears in release group names ("BD-Crew" etc).
    result = re.sub(r'\bBD[\s._-]?(?:25|50|100)\b', 'Bluray', result, flags=re.IGNORECASE)
    # DVD disc tier → "DVDRip"
    #   DVD-R / DVDR / DVD.R / DVD_R. The trailing \b prevents matching
    #   inside "DVDRip" (R is followed by "i", a word char, so \b fails).
    #   It also leaves DVD-RW / DVD-RAM alone for the same reason.
    result = re.sub(r'\bDVD[\s._-]?R\b', 'DVDRip', result, flags=re.IGNORECASE)
    #   DVD5 / DVD9 (single/dual-layer DVD-size tags).
    result = re.sub(r'\bDVD[\s._-]?(?:5|9)\b', 'DVDRip', result, flags=re.IGNORECASE)
    # Clean up any double spaces left over.
    result = re.sub(r'  +', ' ', result)
    return result


def rename_audio_codec_in_filename(filename: str, new_audio_tag: str) -> str:
    """Replace audio codec tags in a filename with the actual primary audio codec."""
    # Build a combined pattern matching any known audio codec tag
    combined = "|".join(AUDIO_FILENAME_PATTERNS)
    # Only replace the first match (the primary audio codec in the filename)
    result = re.sub(combined, new_audio_tag, filename, count=1, flags=re.IGNORECASE)
    return result


async def _is_media_dir_root(candidate: Path) -> bool:
    """Return True if `candidate` is one of the user's configured
    media_dirs (path equality after normalizing trailing slashes).
    Used by build_disc_output_filename to decide whether an ISO at
    `candidate / xxx.iso` is 'loose' (use ISO stem for filename) vs
    'in a movie folder' (use parent folder name). v0.7.0+."""
    try:
        import aiosqlite
        from backend.database import DB_PATH
    except ImportError:
        return False
    norm = str(candidate).rstrip("/")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            for r in await cur.fetchall():
                if str(r["path"]).rstrip("/") == norm:
                    return True
    finally:
        await db.close()
    return False


async def build_disc_output_filename(
    disc_marker_path: str,
    disc_type: str,
    probe_data: dict,
    encoder: str | None = None,
) -> str:
    """Construct a scene-style output filename for a converted disc.

    Disc-folder conversions have no source filename to mutate (regular
    files go through `rename_source_to_target_codec` etc), so the name
    is BUILT from the parent folder name + probe-derived tokens
    (resolution / source-quality / audio codec / channels / encoder
    tag). Output lands in the parent folder of VIDEO_TS/ or BDMV/.

    Pattern (space-separated scene style):
      "<parent name> <resolution> <DVDRip|Bluray> [<audio codec> <channels>] <encoder>.mkv"

    Example:
      `/Movies/Fast-Walking (1982) [tt0083930]/VIDEO_TS/VIDEO_TS.IFO`
      + disc_type='dvd' + 480p MPEG-2 + AC3 2.0 + libx265
      → `/Movies/Fast-Walking (1982) [tt0083930]/Fast-Walking (1982) 480p DVDRip AC3 2.0 x265.mkv`

    Audio/channels tokens are omitted entirely when the probe yields no
    audio tracks (rare; preserves a useful fallback name).

    v0.6.8+: metadata-ID brackets/braces (e.g. `[tt0363589]`, `[imdb-tt..]`,
    `[tmdb-123]`, `{tvdb-123}`) are stripped from the FILE name — they
    belong on the FOLDER (per *arr convention) but not duplicated into
    the filename itself. The folder structure on disk is unchanged.

    v0.7.0+: ISO file inputs (`disc_marker_path` points at a `.iso`)
    produce output in the ISO's parent dir. Base name comes from the
    parent folder when the ISO sits inside a movie-named folder, or
    from the ISO stem when the ISO is loose at a `media_dirs` root.

    v0.6.0+.
    """
    import re as _re
    from backend.rename import _format_channels
    p = Path(disc_marker_path)
    # v0.7.0: ISO file input — output lives in the ISO's parent dir.
    # Base name comes from the parent folder name unless the ISO is
    # "loose" at a media_dir root, in which case use the ISO stem.
    if p.is_file() and p.suffix.lower() == ".iso":
        iso_parent = p.parent
        if await _is_media_dir_root(iso_parent):
            base_name = p.stem
        else:
            base_name = iso_parent.name
        output_dir = iso_parent
    else:
        # v0.6.0: folder-based disc — marker path is
        # .../<parent>/VIDEO_TS/VIDEO_TS.IFO or
        # .../<parent>/BDMV/index.bdmv. Strip two segments to get the
        # disc-root (parent) folder.
        output_dir = p.parent.parent
        base_name = output_dir.name
    # Strip metadata-ID tags ([tt1234567], [imdb-tt..], [tmdb-..], [tvdb-..],
    # {tmdb-..}, {tvdb-..}) and the whitespace immediately preceding them.
    # Folder name keeps the IDs (for *arr cataloguing); only the filename
    # drops them. v0.6.8+.
    base_name = _re.sub(
        r"\s*[\[\{](?:tt\d+|(?:imdb|tmdb|tvdb)[-:][a-zA-Z0-9]+)[\]\}]",
        "",
        base_name,
    ).strip()

    # Resolution token from probe video height
    h = int(probe_data.get("video_height") or 0)
    if h >= 2000:
        res = "2160p"
    elif h >= 1000:
        res = "1080p"
    elif h >= 700:
        res = "720p"
    elif h >= 560:
        res = "576p"  # PAL DVD typical
    else:
        res = "480p"  # NTSC DVD typical

    source_quality = "Bluray" if disc_type == "bdmv" else "DVDRip"

    # Primary audio track → scene-style codec + channels tokens.
    audio_tracks = probe_data.get("audio_tracks") or []
    audio_token = ""
    channels_token = ""
    if audio_tracks:
        a = audio_tracks[0]
        codec_raw = a.get("codec") or ""
        if codec_raw:
            audio_token = get_audio_display_name(codec_raw, a.get("profile") or "")
        ch = int(a.get("channels") or 0)
        if ch > 0:
            channels_token = _format_channels(ch)

    # Encoder tag — reuse existing helper.
    codec_tag = _hevc_tag_for_encoder(encoder)

    # Assemble: parent name + space-separated tokens + .mkv
    tokens = [base_name, res, source_quality]
    if audio_token:
        tokens.append(audio_token)
    if channels_token:
        tokens.append(channels_token)
    tokens.append(codec_tag)
    name = " ".join(tokens) + ".mkv"
    return str(output_dir / name)


def get_output_path(input_path: str, suffix: str = "", encoder: str | None = None) -> str:
    """Return the final output path: rename codec tag, add suffix, and change extension to .mkv.

    `encoder` is threaded through so libx265 output gets `x265` and
    NVENC output gets `h265` — the scene convention that distinguishes
    software-encoder tags from codec tags.
    """
    p = Path(input_path)
    new_stem = rename_source_to_target_codec(p.stem, encoder=encoder)
    # v0.5.18: normalize disc-tier source tags (BR-DISK→Bluray, DVD-R→DVDRip)
    # since the re-encoded file is no longer a disc rip.
    new_stem = rename_source_quality_in_filename(new_stem)
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
    """Rename external subtitle files that match the original filename stem.

    No-op when the parent dir is gone. v0.6.4: disc conversions delete the
    source subdirectory (VIDEO_TS/ or BDMV/) before this runs, so the
    original_path's parent is missing; that's expected, not an error.
    Discs don't have sidecar subs anyway — their subtitles are internal
    PGS/VobSub streams on the disc itself."""
    p = Path(original_path)
    original_stem = p.stem
    parent = p.parent

    if not parent.exists():
        return

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
        # Duration-unknown files (corrupt/in-progress mkv where ffprobe
        # returned no duration, AND total_frames unavailable): we can't
        # compute a meaningful progress ratio, but if the line has an
        # `fps=` field we still want the UI's fps readout to update
        # rather than stalling. Return progress=0 + the fps we parsed,
        # which keeps the worker's progress callback firing instead of
        # going silent for the rest of the encode. v0.4.1+.
        if fps_val is not None:
            return {"progress": 0.0, "fps": fps_val, "eta_seconds": None}
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

    # v0.6.0: original_size + free-disk-space check are deferred until after
    # the probe sets `disc_type`. For disc-folder inputs the marker file
    # (VIDEO_TS.IFO / index.bdmv) is only ~KBs, so using `p.stat().st_size`
    # here would (a) make the free-disk-space check vacuously pass and
    # (b) cause the post-encode `space_saved` comparison to wrongly discard
    # successful encodes as "larger than original". See the disc-aware
    # recomputation below, after `disc_type` is set.

    # v0.6.0: temp_path / final_path computation is deferred until after
    # the probe, so disc-folder inputs (whose marker path sits inside
    # VIDEO_TS/ or BDMV/) can use the disc-root for both output and temp.
    # The actual values get assigned below once disc_type is known.

    # Read live settings from DB — or use pre_settings if provided (worker mode, no local DB)
    if pre_settings is not None:
        live_settings = pre_settings
    else:
        live_settings = await get_live_encoding_settings()
    filename_suffix = live_settings.get("filename_suffix", "")
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
    # v0.5.7: hardware decode settings. Resolved per-job below once
    # the source codec is known (see HW-decode resolution block before
    # the _build_ffmpeg_cmd_impl call). Coerced to bool by
    # get_live_encoding_settings via _str_to_bool, matching qsv_lookahead.
    nvenc_hw_decode = bool(live_settings.get("nvenc_hw_decode", True))
    qsv_hw_decode = bool(live_settings.get("qsv_hw_decode", True))
    vaapi_hw_decode = bool(live_settings.get("vaapi_hw_decode", True))
    libx265_use_nvdec = bool(live_settings.get("libx265_use_nvdec", False))

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
    # v0.5.7: initialised here so it's always in scope for the HW-decode
    # resolution block below — even if the probe try-block fails (which
    # also defeats HW decode gating; we fall back to software decode).
    source_video_codec: str | None = None
    # v0.5.9: source pix_fmt also pre-initialised so the bit-depth `auto`
    # resolution block has a safe value when the probe fails (defaults
    # to 8-bit encode, the safer fallback).
    source_pix_fmt: str = ""
    # v0.6.0: disc-folder input flag. Set from probe_data["disc_type"]
    # (scanner returns 'dvd' for VIDEO_TS.IFO inputs, 'bdmv' for
    # BDMV/index.bdmv inputs); None for regular files. Drives the
    # protocol-prefixed encode input, the output-filename construction,
    # the temp-path location, and the HW-decode bypass below.
    disc_type: str | None = None
    probe_data: dict | None = None
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
            # v0.5.7: source codec for HW decode gating + log line. Already
            # populated by scanner.probe_file (codec_name of first video
            # stream). Captured here so it's in scope for the HW-decode
            # resolution block before _build_ffmpeg_cmd_impl.
            source_video_codec = probe_data.get("video_codec") or None
            # v0.5.9: source pix_fmt for `nvenc_bit_depth=auto` resolution.
            # Probed by scanner.probe_file; "yuv420p10le" / "yuv420p12le"
            # signal 10-bit source, everything else (yuv420p, nv12) is 8-bit.
            source_pix_fmt = probe_data.get("video_pix_fmt") or ""
            # v0.6.0: disc-folder marker. Set by scanner.probe_file when the
            # input_path points at a VIDEO_TS.IFO ('dvd') or BDMV/index.bdmv
            # ('bdmv'). Encode-input then becomes ffmpeg's dvd:/ or bluray:/
            # protocol with the disc-root folder; ffmpeg auto-selects the
            # longest title.
            disc_type = probe_data.get("disc_type") or None

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

    # v0.6.0: compute original_size + free-disk-space check now that
    # disc_type is known. For disc folders, the input_path points at a
    # ~KB marker file inside VIDEO_TS/ or BDMV/ — walk the disc root to
    # get the real total instead.
    try:
        if disc_type:
            from backend.scanner import _disc_total_size
            disc_root = Path(input_path).parent.parent
            original_size = _disc_total_size(disc_root, disc_type)
            if original_size <= 0:
                # Fall back to marker stat if disc-walk fails (defensive)
                original_size = p.stat().st_size
        else:
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

    # v0.6.0: compute output + temp paths now that disc_type is known.
    # Regular files: existing get_output_path / get_temp_path behaviour.
    # Disc inputs: filename built from disc-root folder name + probe tokens
    # (via build_disc_output_filename); temp lands in the disc-root folder
    # too (NOT inside VIDEO_TS/ or BDMV/, which get_temp_path would do).
    # Pass the effective encoder so the output filename picks the right
    # codec tag: `x265` for libx265, `h265` for NVENC (see
    # rename_source_to_target_codec for the rationale).
    if disc_type and probe_data:
        final_path = await build_disc_output_filename(
            input_path, disc_type, probe_data, encoder=encoder,
        )
        temp_path = str(Path(final_path).with_suffix(".converting.mkv"))
    else:
        final_path = get_output_path(input_path, suffix=filename_suffix, encoder=encoder)
        temp_path = get_temp_path(input_path)

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
    # v0.7.0: extra ffmpeg input args (e.g. `-f dvdvideo` for DVD ISO)
    # that must be emitted BEFORE `-i` in the encode cmd. Spliced in
    # below after _build_ffmpeg_cmd_impl returns. Empty for folder discs
    # and regular files.
    ffmpeg_input_args: list[str] = []
    # v0.6.0: disc-folder input — encode reads from ffmpeg's dvd:/ or
    # bluray:/ protocol with the disc-root folder (parent of VIDEO_TS/
    # or BDMV/). Prestrip never fires for disc inputs (no sub-removal
    # against a virtual title set), so the protocol path stays through
    # the encode without further swaps.
    # v0.7.0: ISO file input — ffmpeg accepts the ISO directly via
    # libdvdread (DVD, needs `-f dvdvideo`) or libbluray (BD, via the
    # `bluray:` protocol on the .iso path). No mount, no extraction.
    if disc_type:
        _disc_p = Path(input_path)
        if _disc_p.is_file() and _disc_p.suffix.lower() == ".iso":
            if disc_type == "dvd":
                encode_input_path = str(_disc_p)
                ffmpeg_input_args = ["-f", "dvdvideo"]
            else:  # bdmv
                encode_input_path = f"bluray:{_disc_p}"
            print(
                f"[CONVERT] Disc input detected ({disc_type}, iso); "
                f"input={encode_input_path}",
                flush=True,
            )
        else:
            disc_root = _disc_p.parent.parent
            if disc_type == "dvd":
                # v0.6.2: DVD encode reads through ffmpeg's `concat:` protocol
                # over the main-feature VOBs. The v0.6.0 `dvd:/` protocol was
                # fictional; see scanner._dvd_concat_input docstring.
                from backend.scanner import _dvd_concat_input
                encode_input_path = _dvd_concat_input(disc_root)
                if encode_input_path is None:
                    raise RuntimeError(
                        f"DVD encode failed: no main-feature VOBs in {disc_root}/VIDEO_TS/"
                    )
            else:  # bdmv
                encode_input_path = f"bluray:{disc_root}"
            print(f"[CONVERT] Disc input detected ({disc_type}, folder); using {disc_type}-concat over disc_root={disc_root.name}", flush=True)
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
        # v0.5.21: explicit default=False matches the UI's `?? false`
        # rendering. Pre-v0.5.21 missing-row fallback was True, so
        # external subs got merged even when the UI showed the toggle
        # off (which always did because saves were silently dropped).
        if _is_cleanup_enabled("merge_external_subs", default=False):
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

    # v0.5.6: thread cap from live settings (0 = ffmpeg auto).
    try:
        ffmpeg_threads = int(live_settings.get("ffmpeg_threads", 0) or 0)
    except (TypeError, ValueError):
        ffmpeg_threads = 0

    # v0.5.7: compute HW decode parameters for this specific job based
    # on encoder choice + user setting + per-codec support. Silent
    # fallback to software decode when codec unsupported — logged but
    # not surfaced as a job failure.
    _hw_use = False
    _hw_backend: str | None = None
    _hw_on_device = True
    _src_codec_lower = (source_video_codec or "").lower() if source_video_codec else None
    # v0.5.26: enrich the fallback log line with pix_fmt so users hitting
    # the 10-bit H.264 exclusion can tell why NVDEC was skipped.
    _hw_skip_reason = (
        f"codec '{_src_codec_lower}' pix_fmt '{source_pix_fmt or 'unknown'}'"
        if source_pix_fmt else f"codec '{_src_codec_lower}'"
    )
    if encoder == "nvenc" and nvenc_hw_decode:
        if hw_decode_supports("cuda", _src_codec_lower, source_pix_fmt):
            _hw_use = True
            _hw_backend = "cuda"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for {_hw_skip_reason} "
                  f"on NVDEC — software fallback for this job",
                  flush=True)
    elif encoder == "qsv" and qsv_hw_decode:
        if hw_decode_supports("qsv", _src_codec_lower, source_pix_fmt):
            _hw_use = True
            _hw_backend = "qsv"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{_src_codec_lower}' on QSV — software fallback for this job",
                  flush=True)
    elif encoder == "vaapi" and vaapi_hw_decode:
        if hw_decode_supports("vaapi", _src_codec_lower, source_pix_fmt):
            _hw_use = True
            _hw_backend = "vaapi"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{_src_codec_lower}' on VAAPI — software fallback for this job",
                  flush=True)
    elif encoder == "libx265" and libx265_use_nvdec:
        if hw_decode_supports("cuda", _src_codec_lower, source_pix_fmt):
            _hw_use = True
            _hw_backend = "cuda"
            _hw_on_device = False  # mixed mode — readback to CPU
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{_src_codec_lower}' on NVDEC (libx265 mixed mode) — software fallback",
                  flush=True)

    # v0.6.0: ffmpeg's dvd:/ and bluray:/ protocols always demux through
    # the native software decoder; -hwaccel is silently ignored. Force
    # the flags off so the filter-chain logic doesn't try to insert
    # scale_cuda / hwupload etc. that would expect HW frames the demuxer
    # never produced.
    if disc_type:
        _hw_use = False
        _hw_backend = None
        _hw_on_device = True  # filter chain skips hwupload; HW path is fully off via _hw_use=False
        print("[CONVERT] HW decode bypassed for disc input (ffmpeg protocol demux is software-only)", flush=True)

    hw_decode_active = _hw_use

    # v0.5.9: resolve effective NVENC bit depth per job. The setting is
    # one of "10bit" / "8bit" / "auto":
    #   - "10bit": always main10 / p010le (pre-v0.5.9 hardcoded behaviour)
    #   - "8bit":  always main / nv12 (Maxwell-compatible, smaller files
    #              on most sources, faster encode)
    #   - "auto":  probe source pix_fmt; encode 10-bit only when source
    #              is 10-bit (yuv420p10le / yuv420p12le / p010 surfaces),
    #              else 8-bit. Avoids unnecessary 8→10 bit upconvert.
    _bit_depth_setting = str(live_settings.get("nvenc_bit_depth", "10bit")).lower()
    if _bit_depth_setting == "8bit":
        nvenc_effective_bit_depth = "8bit"
    elif _bit_depth_setting == "auto":
        pf = (source_pix_fmt or "").lower()
        # Any pix_fmt with "10" or "12" in the bit-depth suffix counts as
        # high-bit-depth source: yuv420p10le, yuv422p10le, yuv444p10le,
        # yuv420p12le, p010le, p012le, etc.
        is_10bit_source = ("p10" in pf) or ("p12" in pf) or ("10le" in pf) or ("12le" in pf)
        nvenc_effective_bit_depth = "10bit" if is_10bit_source else "8bit"
        if encoder == "nvenc":
            print(
                f"[CONVERT] NVENC bit-depth auto: source pix_fmt='{pf or 'unknown'}' "
                f"→ encoding {nvenc_effective_bit_depth}",
                flush=True,
            )
    else:
        # Default and any unrecognised value fall through to 10bit.
        nvenc_effective_bit_depth = "10bit"

    # v0.7.14: NVDEC silently falls back to software decode for frames it
    # can't decode on-GPU (e.g. sources with "unknown" colour metadata).
    # That mid-stream CUDA→CPU frame switch shows up as "hwaccel changed"
    # and breaks the scale_cuda filter graph ("Error reinitializing
    # filters!", exit 218). No input flag prevents it. When we detect that
    # specific failure on the NVDEC-native CUDA path, retry once with
    # software decode — the software decoders handle every mid-stream
    # change consistently, and NVENC still does the encode.
    _used_nvdec_native = bool(_hw_use and _hw_on_device and _hw_backend == "cuda")
    _RECONFIG_SIGNATURES = (
        "Error reinitializing filters",
        "auto_scale_0",
        "hwaccel changed",
    )

    def _assemble_cmd(use_hw: bool) -> list:
        """Build the full ffmpeg argv for an encode attempt. `use_hw`
        toggles hardware decode — the software-decode retry passes False
        to bypass NVDEC while keeping the NVENC encoder."""
        c = _build_ffmpeg_cmd_impl(
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
            ffmpeg_threads=ffmpeg_threads,
            use_hw_decode=use_hw,
            hw_decode_backend=_hw_backend,
            # When falling back to software decode, frames are never on the
            # GPU, so the on-device flag must follow `use_hw`.
            hw_decode_keeps_on_device=(_hw_on_device and use_hw),
            source_codec=_src_codec_lower,
            nvenc_bit_depth=nvenc_effective_bit_depth,
            pre_input_args=ffmpeg_input_args or None,
        )
        # Append custom ffmpeg flags if configured (before the output path).
        cf = live_settings.get("custom_ffmpeg_flags", "")
        if cf.strip():
            import shlex
            c = c[:-1] + shlex.split(cf) + c[-1:]
        # During quiet hours, lower process priority.
        if nice:
            c = ["nice", "-n", "15", "ionice", "-c", "3"] + c
        return c

    # Outer-scope state the success path (below) reads back after the run.
    all_lines: list[str] = []
    full_command = ""

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

    async def _run_encode(run_cmd: list) -> dict:
        """Run one ffmpeg attempt with live progress streaming.

        Returns:
          {"outcome": "ok"}                       — encode succeeded
          {"outcome": "retry", "result": <dict>}  — failed with an NVDEC
              reconfig signature; caller MAY retry with software decode.
          {"outcome": "fail",  "result": <dict>}  — non-retryable failure
        Assigns the outer `all_lines` so the success path can attach the
        ffmpeg log to its result.
        """
        nonlocal all_lines
        try:
            proc = await asyncio.create_subprocess_exec(
                *run_cmd,
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
            local_all_lines: list[str] = []  # Full log for conversion history
            last_lines: list[str] = []  # Last N for error reporting
            # Sticky error capture (v0.4.8+). Lines matching ffmpeg error
            # patterns get retained here as they're emitted, so they survive
            # the rolling 20-line `last_lines` buffer. Without this, files
            # with large amounts of metadata in the stream listing (e.g. MKVs
            # with many subtitle/audio streams each carrying _STATISTICS_*
            # tags) push the actual error line off the end before we capture
            # it for `error_log`. Cap at 50 lines to bound DB write size.
            error_lines: list[str] = []
            _ERROR_PATTERNS = (
                "[error]", "Error ", "error:", "ERROR ", "ERROR:",
                "failed", "Failed", "FAILED",
                "Could not", "could not",
                "Invalid ", "invalid ",
                "No such ", "Cannot ", "Unable to ",
                "Unknown encoder", "Unknown decoder", "Unknown format",
                "Conversion failed",
            )
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
                            local_all_lines.append(line)
                        last_lines.append(line)
                        if len(last_lines) > 20:
                            last_lines.pop(0)
                        # Sticky error capture — see _ERROR_PATTERNS above.
                        if any(p in line for p in _ERROR_PATTERNS):
                            error_lines.append(line)
                            if len(error_lines) > 50:
                                error_lines.pop(0)
                    if progress_callback and line:
                        parsed = parse_ffmpeg_progress(
                            line, duration,
                            start_time=encode_start_time,
                            total_frames=progress_total_frames,
                        )
                        if parsed:
                            await progress_callback(**parsed)

            await asyncio.wait_for(proc.wait(), timeout=live_settings.get("ffmpeg_timeout", 21600))
            all_lines = local_all_lines

            if proc.returncode != 0:
                # Clean up temp file if it exists
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except OSError:
                    pass
                # Extract meaningful error from ffmpeg output. v0.4.8+:
                # prefer the sticky `error_lines` we accumulated during the
                # encode (lines matching error patterns), since those survive
                # MKVs with heavy stream metadata that would otherwise push
                # the real error off the rolling 20-line `last_lines` buffer.
                # Fall back to the last 10 non-progress lines from the rolling
                # buffer if no pattern matches found.
                if error_lines:
                    error_detail = "\n".join(error_lines[-15:])
                else:
                    non_progress = [l for l in last_lines if not l.startswith("frame=") and not l.startswith("size=")]
                    error_detail = "\n".join(non_progress[-10:]) if non_progress else ""
                error_msg = f"ffmpeg exited with code {proc.returncode}"
                if error_detail:
                    error_msg += f"\n\n{error_detail}"
                # v0.4.9+: also persist the full command and stderr log on
                # failure so the Completed-tab failed-job expand can show
                # the exact invocation. Pre-fix the failure path returned
                # only `error`, leaving the new ffmpeg_command / ffmpeg_log
                # collapsible sections empty for any failed job.
                fail_dict = {
                    "success": False,
                    "output_path": None,
                    "space_saved": 0,
                    "error": error_msg,
                    "ffmpeg_command": full_command,
                    "ffmpeg_log": "\n".join(local_all_lines[-500:]),
                }
                # v0.7.14: classify the failure. The NVDEC mid-stream
                # reconfig crash is retryable with software decode.
                haystack = "\n".join(local_all_lines[-80:])
                if any(sig in haystack for sig in _RECONFIG_SIGNATURES):
                    return {"outcome": "retry", "result": fail_dict}
                return {"outcome": "fail", "result": fail_dict}

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            return {"outcome": "fail", "result": {
                "success": False,
                "output_path": None,
                "space_saved": 0,
                "error": "ffmpeg timed out",
                "ffmpeg_command": full_command,
                "ffmpeg_log": "\n".join(all_lines[-500:]),
            }}
        except Exception as exc:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
            if prestrip_path:
                try: Path(prestrip_path).unlink(missing_ok=True)
                except OSError: pass
            return {"outcome": "fail", "result": {"success": False, "output_path": None, "space_saved": 0, "error": str(exc)}}

        return {"outcome": "ok"}

    # Run the encode, with one software-decode retry on the NVDEC
    # mid-stream reconfig failure (v0.7.14). The fast NVDEC path is tried
    # first; only the specific reconfiguration crash on the NVDEC-native
    # CUDA path triggers the software-decode fallback.
    _software_retry = False
    while True:
        cmd = _assemble_cmd(_hw_use and not _software_retry)
        full_command = " ".join(cmd)
        if _software_retry:
            print(f"[CONVERT] Retrying with software decode: {' '.join(cmd[:6])} ...", flush=True)
        else:
            print(f"[CONVERT] ffmpeg cmd: {' '.join(cmd[:6])} ...", flush=True)
        _enc = await _run_encode(cmd)
        if _enc["outcome"] == "ok":
            break
        if (
            _enc["outcome"] == "retry"
            and _used_nvdec_native
            and not _software_retry
        ):
            print(
                "[CONVERT] NVDEC native decode failed mid-stream "
                "(hwaccel reconfig); falling back to software decode + "
                f"{encoder} encode",
                flush=True,
            )
            _software_retry = True
            continue
        # Non-retryable, or retry already attempted — return the failure.
        return _enc["result"]

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
        # v0.7.11: propagate the diagnosis back to the source row so the
        # UI flags it as corrupt and the user doesn't try to convert it
        # again. ffprobe-time corruption detection only sees container
        # headers; ffmpeg-decode corruption (corrupt data mid-stream)
        # never surfaces until conversion is attempted. Without this
        # mark, the row still shows "Convert to x265 (est. save ...)"
        # as if healthy. Best-effort: a DB failure here doesn't block
        # the return-to-caller-with-original-preserved path.
        try:
            import aiosqlite as _aiosqlite
            import json as _json
            from backend.database import DB_PATH as _DB_PATH
            _db = await _aiosqlite.connect(_DB_PATH)
            try:
                await _db.execute(
                    "UPDATE scan_results SET "
                    "  health_status = 'corrupt', "
                    "  probe_status = 'corrupt', "
                    "  health_errors_json = ? "
                    "WHERE file_path = ?",
                    (
                        _json.dumps({
                            "source": "conversion_size_check",
                            "error": "Output suspiciously small — likely source stream corruption ffprobe didn't catch",
                            "original_size": original_size,
                            "output_size": output_size,
                        }),
                        input_path,
                    ),
                )
                await _db.commit()
                print(
                    f"[CONVERT] Marked source as corrupt in scan_results: {input_path}",
                    flush=True,
                )
            finally:
                await _db.close()
        except Exception as _exc:
            print(f"[CONVERT] Failed to mark source corrupt (non-fatal): {_exc}", flush=True)
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
    # v0.5.7: VMAF skipped when HW decode is active for this job.
    # VMAF needs software-decoded source frames; running a second
    # software-decode pass purely for VMAF reference would double
    # source-file I/O. Skip silently here; the existing decision log
    # below still prints, and this extra line makes the situation
    # explicit in the worker log.
    if vmaf_enabled and hw_decode_active:
        print(
            f"[CONVERT] VMAF skipped — hardware decode is active for this job "
            f"(backend={_hw_backend}, on_device={_hw_on_device})",
            flush=True,
        )
        vmaf_enabled = False
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
        if disc_type and Path(input_path).is_file() and Path(input_path).suffix.lower() == ".iso":
            # v0.7.0: ISO source — single file ops (unlink / trash / move).
            # Same three modes (backup / trash / delete) as folder discs
            # but operating on the .iso file directly.
            iso_source = Path(input_path)
            if backup_days and backup_days > 0:
                custom_backup = live_settings.get("backup_folder", "")
                if custom_backup:
                    backup_dir = Path(custom_backup) / iso_source.parent.name
                    backup_dir.mkdir(parents=True, exist_ok=True)
                else:
                    legacy = iso_source.parent / ".squeezarr_backup"
                    backup_dir = legacy if legacy.exists() else (iso_source.parent / ".shrinkerr_backup")
                    backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / iso_source.name
                if backup_path.is_symlink():
                    raise OSError(
                        f"Refusing to move into backup path — destination is a symlink: {backup_path}"
                    )
                shutil.move(str(iso_source), str(backup_path))
                result_backup_path = str(backup_path)
                print(f"[CONVERT] ISO backed up to: {backup_path}", flush=True)
            elif use_trash:
                try:
                    from send2trash import send2trash
                    send2trash(str(iso_source))
                    print(f"[CONVERT] ISO moved to trash: {iso_source.name}", flush=True)
                except Exception as trash_exc:
                    print(f"[CONVERT] Trash failed ({trash_exc}), falling back to permanent delete", flush=True)
                    iso_source.unlink()
            else:
                iso_source.unlink()
                print(f"[CONVERT] Removed ISO: {iso_source}", flush=True)
        elif disc_type:
            # v0.6.0: for disc inputs the "source" is the disc subdir
            # (VIDEO_TS/ or BDMV/), not the marker file inside it. Same
            # three modes (backup / trash / delete) but operating on the
            # whole folder.
            source_to_handle = Path(input_path).parent
            if backup_days and backup_days > 0:
                custom_backup = live_settings.get("backup_folder", "")
                if custom_backup:
                    backup_dir = Path(custom_backup)
                    backup_dir = backup_dir / p.parent.parent.name
                    backup_dir.mkdir(parents=True, exist_ok=True)
                else:
                    legacy = p.parent.parent / ".squeezarr_backup"
                    backup_dir = legacy if legacy.exists() else (p.parent.parent / ".shrinkerr_backup")
                    backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / source_to_handle.name
                if backup_path.is_symlink():
                    raise OSError(
                        f"Refusing to move into backup path — destination is a symlink: {backup_path}"
                    )
                shutil.move(str(source_to_handle), str(backup_path))
                result_backup_path = str(backup_path)
                print(f"[CONVERT] Disc subdir backed up to: {backup_path}", flush=True)
            elif use_trash:
                try:
                    from send2trash import send2trash
                    send2trash(str(source_to_handle))
                    print(f"[CONVERT] Disc subdir moved to trash: {source_to_handle.name}", flush=True)
                except Exception as trash_exc:
                    print(f"[CONVERT] Trash failed ({trash_exc}), falling back to permanent delete", flush=True)
                    shutil.rmtree(source_to_handle)
            else:
                shutil.rmtree(source_to_handle)
                print(f"[CONVERT] Removed disc subdir: {source_to_handle}", flush=True)
        elif backup_days and backup_days > 0:
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
            # v0.5.21: explicit default=False to match UI default and
            # avoid silent file deletion on missing-row installs.
            _should_delete_ext_subs = _ice("delete_external_subs_after_merge", default=False)
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
            # Audio conversion details for the Completed-tab job report
            # (v0.4.7+). Frontend renders e.g. "DTS-HD MA → EAC3 640kb"
            # when these fields are present. Empty list when no audio was
            # re-encoded (audio_codec="copy" and no lossless trigger).
            "audio_converted_from": _build_audio_conversion_summary(
                probe_audio_tracks=probe_audio_tracks,
                global_audio_codec=audio_codec,
                lossless_conversion=lossless_conversion,
            ),
            "lossless_target_codec": (lossless_conversion or {}).get("codec"),
            "lossless_target_bitrate": (lossless_conversion or {}).get("bitrate"),
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
