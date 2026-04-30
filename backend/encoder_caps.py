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

    `qsv_render_node` and `vaapi_render_node` (v0.3.90+) are the
    `/dev/dri/renderD*` paths the cmd builder should pin the encoder
    to. On a single-GPU host these match `/dev/dri/renderD128`; on a
    multi-GPU host (e.g. NUC9 with both Intel iGPU and an NVIDIA
    Quadro), Intel is often `renderD129` and the hardcoded `D128`
    would point at the NVIDIA card → libva fails to init iHD on the
    wrong driver. None when no suitable node is found, in which case
    the corresponding `qsv` / `vaapi` flag is also False.
    """
    nvenc: bool
    qsv: bool
    vaapi: bool
    qsv_render_node: str | None = None
    vaapi_render_node: str | None = None

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


def _classify_render_node(name: str) -> str | None:
    """Return the kernel DRM driver bound to /dev/dri/<name> ('i915',
    'nvidia-drm', 'amdgpu', 'radeon', etc.) by reading sysfs. None if
    the sysfs entry isn't readable. v0.3.90+."""
    path = f"/sys/class/drm/{name}/device/uevent"
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("DRIVER="):
                    return line.strip().split("=", 1)[1] or None
    except OSError:
        return None
    return None


def _list_render_nodes() -> list[tuple[str, str | None]]:
    """List (path, driver) pairs for every /dev/dri/renderD* on the host.
    Sorted by node name so the lowest-numbered node appears first when
    drivers tie. v0.3.90+."""
    out: list[tuple[str, str | None]] = []
    try:
        for entry in sorted(os.listdir("/dev/dri")):
            if entry.startswith("renderD"):
                out.append((f"/dev/dri/{entry}", _classify_render_node(entry)))
    except (FileNotFoundError, PermissionError):
        pass
    return out


def _intel_render_node() -> str | None:
    """First i915-bound render node, or None. QSV requires Intel
    specifically — neither AMD nor NVIDIA hardware can run it."""
    for path, drv in _list_render_nodes():
        if drv == "i915":
            return path
    return None


def _vaapi_render_node() -> str | None:
    """First render node that supports VA-API. Preference order:
    Intel (i915) → AMD (amdgpu / radeon) → any non-NVIDIA fallback.
    NVIDIA explicitly excluded (nvidia-drm doesn't speak VA-API).
    """
    nodes = _list_render_nodes()
    for path, drv in nodes:
        if drv == "i915":
            return path
    for path, drv in nodes:
        if drv in ("amdgpu", "radeon"):
            return path
    # Catch-all for unusual drivers (mock, virt, etc.) that may still
    # work — only excludes nvidia-drm explicitly.
    for path, drv in nodes:
        if drv and drv not in ("nvidia", "nvidia-drm"):
            return path
    return None


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
    has_nvidia = _nvidia_present()
    intel_node = _intel_render_node()
    va_node = _vaapi_render_node()

    _cached = EncoderCaps(
        nvenc=("hevc_nvenc" in encoders) and has_nvidia,
        # QSV requires Intel hardware specifically — having ANY render
        # node isn't enough. On a NUC9-style multi-GPU host, the only
        # render node may be the NVIDIA card; QSV would fail at runtime.
        # Per-driver detection avoids surfacing the option in that case.
        qsv=("hevc_qsv" in encoders) and intel_node is not None,
        # VAAPI works on Intel + AMD. Excluded from NVIDIA-only hosts.
        vaapi=("hevc_vaapi" in encoders) and va_node is not None,
        qsv_render_node=intel_node,
        vaapi_render_node=va_node,
    )
    return _cached
