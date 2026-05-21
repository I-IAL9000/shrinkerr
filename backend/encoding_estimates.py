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
