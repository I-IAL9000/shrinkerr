"""Shared encoding-savings estimator. v0.6.7+.

Single source of truth for "if we re-encode this file to HEVC, how
many bytes do we save?" Used by both scan-time storage of
estimated_savings_bytes AND the queue-estimate modal so they agree.

Calibrated from real NVENC NV-HEVC conversion results — see
routes/jobs.py history for the original calibration data.
"""

from __future__ import annotations


def cq_to_savings_pct(cq: int) -> float:
    """CQ-based empirical savings curve for source→HEVC conversion.

    Calibrated from real-world NVENC results:
      CQ 23 → ~54% actual, CQ 27 → ~77% actual
    Higher CQ → smaller output → larger savings percentage.
    """
    if cq <= 15: return 0.25
    if cq <= 18: return 0.35
    if cq <= 20: return 0.45
    if cq <= 22: return 0.50
    if cq <= 23: return 0.55
    if cq <= 24: return 0.60
    if cq <= 25: return 0.65
    if cq <= 26: return 0.70
    if cq <= 27: return 0.75
    if cq <= 28: return 0.77
    return 0.80


# libx265's CRF runs about this much above NVENC's CQ for similar quality
# (content_detect.CRF_OFFSET).
_CRF_OFFSET = 2
# VideoToolbox's -q:v (higher = better) as the CQ with the same savings,
# from the measured Settings → Video guide rows (q50 ≈ 65-70%, q55-60 ≈
# 40-55%, q65 ≈ 20-25%).
_VT_Q_AS_CQ = ((45, 28), (50, 26), (55, 23), (60, 21), (65, 15))
QUALITY_KEYS = ("default_encoder", "nvenc_cq", "libx265_crf", "qsv_cq", "vaapi_qp", "videotoolbox_quality")


def effective_cq(values: dict) -> int:
    """The default encoder's quality setting on NVENC's CQ scale, for the
    savings curve (v0.10.0). Every estimate read the NVENC CQ, even when
    jobs encode with libx265, QSV, VAAPI or VideoToolbox. `values` are
    settings rows (strings); missing ones use the stored defaults. QSV's
    global_quality and VAAPI's QP sit on a similar 1-51 scale but aren't
    measured."""
    def num(key: str, default: int) -> int:
        try:
            return int(values.get(key) or default)
        except (TypeError, ValueError):
            return default
    encoder = (values.get("default_encoder") or "nvenc").lower()
    if encoder == "libx265":
        return num("libx265_crf", 20) - _CRF_OFFSET
    if encoder == "qsv":
        return num("qsv_cq", 22)
    if encoder == "vaapi":
        return num("vaapi_qp", 22)
    if encoder == "videotoolbox":
        q = num("videotoolbox_quality", 55)
        return next((cq for max_q, cq in _VT_Q_AS_CQ if q <= max_q), 12)
    return num("nvenc_cq", 20)


async def load_effective_cq(db) -> int:
    """effective_cq() from an open database connection."""
    placeholders = ",".join("?" * len(QUALITY_KEYS))
    async with db.execute(
        f"SELECT key, value FROM settings WHERE key IN ({placeholders})", QUALITY_KEYS
    ) as cur:
        return effective_cq({r[0]: r[1] for r in await cur.fetchall()})


def video_conv_savings_bytes(file_size: int, cq: int) -> int:
    """Bytes saved from re-encoding the video stream at `cq`. Does NOT
    include audio/sub track-removal savings (those are tracked
    separately)."""
    return int(file_size * cq_to_savings_pct(cq))


def total_estimated_savings_bytes(
    file_size: int,
    needs_conversion: bool,
    cq: int,
    audio_tracks_to_remove: list,  # list of objects with .bitrate attribute
    duration: float,
) -> int:
    """Sum of video-conversion savings + audio-track-removal savings."""
    savings = 0
    if needs_conversion:
        savings += video_conv_savings_bytes(file_size, cq)
    for track in audio_tracks_to_remove:
        bitrate = getattr(track, "bitrate", None)
        if bitrate and duration:
            savings += int(bitrate * duration / 8)
    return savings
