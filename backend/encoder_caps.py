"""Detect which hardware HEVC encoders this host can actually run.

Probes once at startup (cached for the process lifetime) by combining:

  * `ffmpeg -encoders` — does the binary even know about hevc_nvenc /
    hevc_qsv / hevc_vaapi? (BtbN GPL builds include all three; other
    builds may not.)
  * `/dev/dri/renderD*` — is a render node mounted at all? Without
    `/dev/dri` passthrough, QSV and VAAPI both fail at runtime even if
    ffmpeg knows about the encoders.
  * `nvidia-smi` (already used by `backend/nodes.py` for NVENC) — keeps
    the legacy detection path; this module just centralises results.

Encoders aren't actually run-tested here — that would require a tiny
test encode each boot, which is too slow. The result is "ffmpeg has the
encoder AND the kernel exposes the device", which catches the 95%
case. Failures during real encodes still surface in the job's error log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class EncoderCaps:
    """Snapshot of what HEVC encoders this host can drive.

    `available` is the user-facing list — what to surface in the encoder
    dropdown. `libx265` is always present because it's pure CPU and
    needs no hardware.
    """
    nvenc: bool
    qsv: bool
    vaapi: bool

    @property
    def available(self) -> list[str]:
        out: list[str] = ["libx265"]
        if self.nvenc:
            out.append("nvenc")
        if self.qsv:
            out.append("qsv")
        if self.vaapi:
            out.append("vaapi")
        return out


_cached: EncoderCaps | None = None


def _ffmpeg_encoders() -> set[str]:
    """Return the set of encoder names ffmpeg has compiled in. Empty set
    on any error — caller falls back to libx265."""
    if not shutil.which("ffmpeg"):
        return set()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()
    if proc.returncode != 0:
        return set()
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        # ffmpeg output: ` V..... hevc_nvenc           NVIDIA NVENC hevc encoder ...`
        parts = line.strip().split()
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def _has_render_node() -> bool:
    """True iff /dev/dri/renderD* exists and is openable. /dev/dri may
    be present but renderD nodes can be missing on some hosts."""
    try:
        for entry in os.listdir("/dev/dri"):
            if entry.startswith("renderD"):
                # Don't actually open — just confirm the path exists.
                # Open-for-read here would require the container user to
                # be in the `render` group, which we'd rather report as
                # a separate, actionable error during the real encode.
                return True
    except (FileNotFoundError, PermissionError):
        return False
    return False


def _nvidia_present() -> bool:
    """True iff `nvidia-smi` works. This is the same heuristic
    `backend/nodes.py` uses for NVENC; kept here so this module can
    surface the full picture without back-references."""
    if not shutil.which("nvidia-smi"):
        return False
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.returncode == 0 and bool((proc.stdout or "").strip())
    except Exception:
        return False


def detect_encoders(force: bool = False) -> EncoderCaps:
    """Detect hardware-encoder availability. Cached after first call;
    pass `force=True` to re-probe (used by a manual "redetect" button
    in Settings, not by hot paths)."""
    global _cached
    if _cached is not None and not force:
        return _cached

    encoders = _ffmpeg_encoders()
    has_dri = _has_render_node()
    has_nvidia = _nvidia_present()

    _cached = EncoderCaps(
        nvenc=("hevc_nvenc" in encoders) and has_nvidia,
        qsv=("hevc_qsv" in encoders) and has_dri,
        vaapi=("hevc_vaapi" in encoders) and has_dri,
    )
    return _cached
