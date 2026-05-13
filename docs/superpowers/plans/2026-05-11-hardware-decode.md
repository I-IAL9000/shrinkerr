# Hardware Decode (NVDEC / QSV / VAAPI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hardware decode support (NVDEC, QSV decode, VAAPI decode) paired with the matching encoder, plus an opt-in libx265+NVDEC mixed mode. Surface VMAF/HW-decode incompatibility unambiguously in the UI.

**Architecture:** Per-encoder boolean settings drive pre-input `-hwaccel` flags and the filter chain in `_build_ffmpeg_cmd_impl()`. Source codec is probed before cmd assembly; unsupported combinations silently fall back to software decode with a worker log line. VMAF is skipped (not run on a software pass) when any HW decode path is active for a job; the incompatibility is surfaced at three UI locations. Capability probes gate the toggles so HW-less hosts don't see misleading options.

**Tech Stack:** FastAPI + aiosqlite backend, React/TypeScript frontend, ffmpeg subprocess control via asyncio. Existing `encoder_caps.py` probe infrastructure extended.

---

## File Structure

**Files modified (no new files):**
- `backend/encoder_caps.py` — extend `EncoderCaps` dataclass + `detect_encoders()` to probe `-hwaccels` output
- `backend/models.py` — 4 new `Optional[bool]` fields on `EncodingSettingsUpdate`
- `backend/routes/settings.py` — defaults dict, PUT save, GET response builder
- `backend/routes/stats.py` — `/encoder-caps` response includes the 3 decode-available bools
- `backend/converter.py` — new codec-gating helper, `_build_ffmpeg_cmd_impl()` signature + body changes, `convert_file()` reads settings + gates VMAF
- `frontend/src/api.ts` — extend `EncoderCaps` TypeScript type
- `frontend/src/pages/SettingsPage.tsx` — 4 toggles (inline with encoder cards) + VMAF warning chip + per-toggle sub-warning

**No frontend Completed-tab changes needed** — VMAF skip surfaces via the existing conversion-log rendering once the worker emits a `VMAF skipped: hardware decode enabled` line.

---

## Task 1: Capability detection (encoder_caps)

**Files:**
- Modify: `backend/encoder_caps.py`
- Test: ad-hoc via `python -c "from backend.encoder_caps import detect_encoders; print(detect_encoders())"`

- [ ] **Step 1: Add `_ffmpeg_hwaccels()` probe**

In `backend/encoder_caps.py`, after `_ffmpeg_encoders()` (around line 86), add:

```python
def _ffmpeg_hwaccels() -> set[str]:
    """Return the set of hwaccel backends ffmpeg has compiled in.
    Empty set on any error. Output of `ffmpeg -hwaccels`:

        Hardware acceleration methods:
        cuda
        vaapi
        qsv
        ...
    """
    if not shutil.which("ffmpeg"):
        return set()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return set()
    if proc.returncode != 0:
        return set()
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        s = line.strip()
        # Skip header line "Hardware acceleration methods:"
        if not s or s.endswith(":"):
            continue
        names.add(s)
    return names
```

- [ ] **Step 2: Extend `EncoderCaps` dataclass**

Add three fields after `vaapi_render_node` (line 48):

```python
    # v0.5.7: hardware DECODE capability. Tied to the same ffmpeg
    # `-hwaccels` probe — if the backend is compiled in AND the
    # corresponding encoder is also available (NVDEC needs an NVIDIA
    # GPU which is the same gate as NVENC, etc.), we expose the decode
    # toggle in the UI.
    nvdec_available: bool = False
    qsv_decode_available: bool = False
    vaapi_decode_available: bool = False
```

- [ ] **Step 3: Wire probe into `detect_encoders()`**

Around line 178, after the existing encoder probing, add hwaccel probing and pass to the `EncoderCaps()` constructor:

```python
    hwaccels = _ffmpeg_hwaccels()
    # NVDEC = ffmpeg has cuda hwaccel compiled in AND we already have
    # NVENC (= NVIDIA GPU + working CUDA driver). Splitting them would
    # produce false positives on hosts with cuda support but no GPU.
    nvdec = ("cuda" in hwaccels) and bool(nvenc)
    qsv_decode = ("qsv" in hwaccels) and bool(qsv) and bool(intel_node)
    vaapi_decode = ("vaapi" in hwaccels) and bool(vaapi) and bool(va_node)

    _cached = EncoderCaps(
        nvenc=bool(nvenc),
        qsv=bool(qsv),
        vaapi=bool(vaapi),
        qsv_render_node=intel_node,
        vaapi_render_node=va_node,
        nvdec_available=nvdec,
        qsv_decode_available=qsv_decode,
        vaapi_decode_available=vaapi_decode,
    )
```

- [ ] **Step 4: Verify manually**

Run: `python3 -c "from backend.encoder_caps import detect_encoders; c = detect_encoders(); print(c)"`
Expected: prints all 8 fields. On dev box without GPU: all three decode_available are False.

- [ ] **Step 5: Commit**

```bash
git add backend/encoder_caps.py
git commit -m "feat(caps): probe ffmpeg -hwaccels for NVDEC/QSV/VAAPI decode availability"
```

---

## Task 2: Settings model + defaults + PUT/GET

**Files:**
- Modify: `backend/models.py`
- Modify: `backend/routes/settings.py`
- Modify: `backend/routes/stats.py`

- [ ] **Step 1: Add 4 fields to `EncodingSettingsUpdate`**

In `backend/models.py`, after `vaapi_compression_level` (around line 139):

```python
    # v0.5.7: hardware decode toggles. Native pairs (encoder + matching
    # decoder) default on — frames stay on the device, no PCIe transfer.
    # libx265+NVDEC defaults off because it requires GPU→CPU readback
    # per frame, only a win when the CPU is the bottleneck.
    nvenc_hw_decode: Optional[bool] = None
    qsv_hw_decode: Optional[bool] = None
    vaapi_hw_decode: Optional[bool] = None
    libx265_use_nvdec: Optional[bool] = None
```

- [ ] **Step 2: Add to `_ENCODING_DEFAULTS`**

In `backend/routes/settings.py`, after `vaapi_compression_level` (around line 40):

```python
    # v0.5.7: hardware decode. Native pairs default on; cross-bus opt-in.
    "nvenc_hw_decode": "true",
    "qsv_hw_decode": "true",
    "vaapi_hw_decode": "true",
    "libx265_use_nvdec": "false",
```

- [ ] **Step 3: GET response builder**

In `backend/routes/settings.py`, after the `vaapi_compression_level` line in the GET response dict (around line 422):

```python
        "nvenc_hw_decode": merged.get("nvenc_hw_decode", "true").lower() == "true",
        "qsv_hw_decode": merged.get("qsv_hw_decode", "true").lower() == "true",
        "vaapi_hw_decode": merged.get("vaapi_hw_decode", "true").lower() == "true",
        "libx265_use_nvdec": merged.get("libx265_use_nvdec", "false").lower() == "true",
```

- [ ] **Step 4: PUT save handler**

In `backend/routes/settings.py`, after the existing `vaapi_compression_level` PUT branch (look for `if update.vaapi_compression_level is not None:`):

```python
        if update.nvenc_hw_decode is not None:
            updates["nvenc_hw_decode"] = "true" if update.nvenc_hw_decode else "false"
        if update.qsv_hw_decode is not None:
            updates["qsv_hw_decode"] = "true" if update.qsv_hw_decode else "false"
        if update.vaapi_hw_decode is not None:
            updates["vaapi_hw_decode"] = "true" if update.vaapi_hw_decode else "false"
        if update.libx265_use_nvdec is not None:
            updates["libx265_use_nvdec"] = "true" if update.libx265_use_nvdec else "false"
```

- [ ] **Step 5: Surface decode-available bools via `/encoder-caps`**

In `backend/routes/stats.py`, extend the `get_encoder_caps` return dict (around line 644):

```python
        "nvenc": caps.nvenc,
        "qsv": caps.qsv,
        "vaapi": caps.vaapi,
        "libx265": True,
        # v0.5.7: HW decode availability (gates the UI toggles).
        "nvdec_available": caps.nvdec_available,
        "qsv_decode_available": caps.qsv_decode_available,
        "vaapi_decode_available": caps.vaapi_decode_available,
        "available": caps.available,
        "qsv_render_node": caps.qsv_render_node,
        "vaapi_render_node": caps.vaapi_render_node,
```

- [ ] **Step 6: Syntax check + manual GET test**

```bash
python3 -c "
import ast
for p in ['backend/models.py', 'backend/routes/settings.py', 'backend/routes/stats.py']:
    with open(p) as f: ast.parse(f.read())
    print(f'OK: {p}')
"
```

- [ ] **Step 7: Commit**

```bash
git add backend/models.py backend/routes/settings.py backend/routes/stats.py
git commit -m "feat(settings): add 4 HW decode toggles + surface caps via /encoder-caps"
```

---

## Task 3: Codec gating helper + worker log

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Add `hw_decode_supports()` helper**

In `backend/converter.py`, near `_hevc_tag_for_encoder` (around line 493), add:

```python
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


def hw_decode_supports(decoder: str, source_codec: str | None) -> bool:
    """True if `decoder` ('cuda'/'qsv'/'vaapi') can hardware-decode
    `source_codec` (lowercase ffprobe codec name). Returns False when
    source_codec is None/empty so probe failures fall back to software
    rather than crashing the cmd builder."""
    if not source_codec:
        return False
    c = source_codec.lower()
    table = {
        "cuda": _NVDEC_SUPPORTED,
        "qsv": _QSV_DECODE_SUPPORTED,
        "vaapi": _VAAPI_DECODE_SUPPORTED,
    }
    return c in table.get(decoder, frozenset())
```

- [ ] **Step 2: Self-test the helper**

```bash
python3 -c "
from backend.converter import hw_decode_supports
cases = [
    ('cuda', 'h264', True),
    ('cuda', 'mpeg2video', True),
    ('cuda', 'msmpeg4v3', False),
    ('cuda', None, False),
    ('qsv', 'vc1', False),
    ('qsv', 'mjpeg', True),
    ('vaapi', 'vp9', True),
    ('vaapi', 'mjpeg', False),
    ('unknown', 'h264', False),
]
for dec, codec, expected in cases:
    got = hw_decode_supports(dec, codec)
    assert got == expected, f'{dec}/{codec}: expected {expected}, got {got}'
print('OK')
"
```

- [ ] **Step 3: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): hw_decode_supports() codec gating helper"
```

---

## Task 4: Wire HW decode into `_build_ffmpeg_cmd_impl`

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Extend signature**

In `_build_ffmpeg_cmd_impl()` (around line 214), add these parameters after `ffmpeg_threads`:

```python
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
```

- [ ] **Step 2: Emit pre-input `-hwaccel` flags**

Locate the pre-input section (around line 257-282, the block starting with `cmd = ["ffmpeg", "-y"]`). Insert HW-decode flag emission after the threads block and BEFORE the existing VAAPI/QSV `-init_hw_device` block:

```python
    # v0.5.7: HW decode pre-input flags. Must come before the VAAPI/QSV
    # init_hw_device calls because those refer to the device that will
    # be set up by -hwaccel.
    if use_hw_decode and hw_decode_backend:
        cmd += ["-hwaccel", hw_decode_backend]
        if hw_decode_keeps_on_device:
            cmd += ["-hwaccel_output_format", hw_decode_backend]
        # else: libx265+NVDEC mixed mode — frames downloaded to CPU
```

- [ ] **Step 3: Filter chain rework**

Replace the filter chain block (around line 289-310). New logic:

```python
    # Resolution scaling + decode/encode filter chain wiring.
    # v0.5.7: when HW decode is on, scale on the device matching the
    # decoder backend; when off, retain pre-v0.5.7 software-scale path.
    scale = RESOLUTION_MAP.get(target_resolution)
    if use_hw_decode and hw_decode_keeps_on_device:
        # Native pair: frames stay on device. Scale with the matching
        # device-native scaler; no hwupload needed.
        if hw_decode_backend == "cuda":
            # NVENC + NVDEC. scale_cuda for resolution change, otherwise
            # frames flow straight through with no -vf at all.
            if scale:
                cmd += ["-vf", f"scale_cuda={scale}"]
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
```

- [ ] **Step 4: Decode-path log line**

Right after the filter chain block, before the encoder selection, add:

```python
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
```

- [ ] **Step 5: Verify with sanity tests**

```bash
python3 -c "
from backend.converter import _build_ffmpeg_cmd_impl

# Software decode + libx265 (baseline, pre-v0.5.7 behaviour)
cmd = _build_ffmpeg_cmd_impl('/in.mkv', '/out.mkv', encoder='libx265')
assert '-hwaccel' not in cmd, 'unexpected hwaccel in default'
print('OK: software default')

# NVENC + NVDEC native pair
cmd = _build_ffmpeg_cmd_impl('/in.mkv', '/out.mkv', encoder='nvenc',
                              use_hw_decode=True, hw_decode_backend='cuda',
                              hw_decode_keeps_on_device=True,
                              source_codec='h264')
i = cmd.index('-hwaccel')
assert cmd[i+1] == 'cuda'
assert cmd[i+2] == '-hwaccel_output_format'
assert cmd[i+3] == 'cuda'
assert i < cmd.index('-i')
print('OK: NVENC+NVDEC native pair')

# libx265 + NVDEC mixed (no _output_format)
cmd = _build_ffmpeg_cmd_impl('/in.mkv', '/out.mkv', encoder='libx265',
                              use_hw_decode=True, hw_decode_backend='cuda',
                              hw_decode_keeps_on_device=False,
                              source_codec='h264')
i = cmd.index('-hwaccel')
assert cmd[i+1] == 'cuda'
# Must NOT include -hwaccel_output_format
assert '-hwaccel_output_format' not in cmd, 'mixed mode should not set output_format'
# Filter chain must include hwdownload
vf_i = cmd.index('-vf')
assert 'hwdownload' in cmd[vf_i+1]
print('OK: libx265+NVDEC mixed mode')

# NVENC + NVDEC with scale
cmd = _build_ffmpeg_cmd_impl('/in.mkv', '/out.mkv', encoder='nvenc',
                              target_resolution='720p',
                              use_hw_decode=True, hw_decode_backend='cuda',
                              hw_decode_keeps_on_device=True,
                              source_codec='hevc')
vf_i = cmd.index('-vf')
assert 'scale_cuda' in cmd[vf_i+1], f'expected scale_cuda in {cmd[vf_i+1]}'
print('OK: NVENC+NVDEC with scale_cuda')
"
```

- [ ] **Step 6: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): emit -hwaccel + device-native scaler when HW decode is on"
```

---

## Task 5: Wire HW decode settings into `convert_file()` + VMAF gate

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Read settings + decide HW decode path**

In `convert_file()`, find where existing encoder-related live_settings are read (around line 1348-1366). After those lines, add:

```python
    # v0.5.7: hardware decode resolution. Source codec is already
    # probed below as part of the audio/video probe; we use it to
    # gate HW decode silently when the codec isn't supported.
    nvenc_hw_decode = bool(live_settings.get("nvenc_hw_decode", True))
    qsv_hw_decode = bool(live_settings.get("qsv_hw_decode", True))
    vaapi_hw_decode = bool(live_settings.get("vaapi_hw_decode", True))
    libx265_use_nvdec = bool(live_settings.get("libx265_use_nvdec", False))
```

- [ ] **Step 2: Resolve HW decode after source codec is known**

After the probe runs and `probe_video_codec` (or equivalent — find the variable that holds the source codec; check around line 1380-1400). Add:

```python
    # v0.5.7: compute HW decode parameters for this specific job based
    # on encoder choice + capability + codec support. Silent fallback
    # to software decode when codec unsupported.
    from backend.converter import hw_decode_supports  # already in module
    _hw_use = False
    _hw_backend: str | None = None
    _hw_on_device = True
    source_video_codec = (probe_video_codec or "").lower() if "probe_video_codec" in dir() else None
    if encoder == "nvenc" and nvenc_hw_decode:
        if hw_decode_supports("cuda", source_video_codec):
            _hw_use = True
            _hw_backend = "cuda"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{source_video_codec}' on NVDEC — software fallback for this job", flush=True)
    elif encoder == "qsv" and qsv_hw_decode:
        if hw_decode_supports("qsv", source_video_codec):
            _hw_use = True
            _hw_backend = "qsv"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{source_video_codec}' on QSV — software fallback for this job", flush=True)
    elif encoder == "vaapi" and vaapi_hw_decode:
        if hw_decode_supports("vaapi", source_video_codec):
            _hw_use = True
            _hw_backend = "vaapi"
            _hw_on_device = True
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{source_video_codec}' on VAAPI — software fallback for this job", flush=True)
    elif encoder == "libx265" and libx265_use_nvdec:
        if hw_decode_supports("cuda", source_video_codec):
            _hw_use = True
            _hw_backend = "cuda"
            _hw_on_device = False  # mixed mode — readback to CPU
        else:
            print(f"[CONVERT] HW decode unavailable for codec "
                  f"'{source_video_codec}' on NVDEC (libx265 mixed mode) — software fallback for this job", flush=True)

    hw_decode_active = _hw_use
```

(Find the actual variable name for source codec in the probe — it might be `video_codec`, `probe.get("video_codec")`, or similar. Adjust the dict-access pattern in `source_video_codec = ...` line to match. Look near the audio-probe block around line 1380-1410.)

- [ ] **Step 3: Pass new params to `_build_ffmpeg_cmd_impl`**

In the existing call (around line 1585-1602), add:

```python
        ...existing params...,
        ffmpeg_threads=ffmpeg_threads,
        use_hw_decode=_hw_use,
        hw_decode_backend=_hw_backend,
        hw_decode_keeps_on_device=_hw_on_device,
        source_codec=source_video_codec,
```

- [ ] **Step 4: Gate VMAF on `not hw_decode_active`**

Find the VMAF dispatch block (around line 1984 — `if vmaf_enabled:`). Change to:

```python
    # v0.5.7: VMAF skipped when HW decode is active. VMAF needs
    # software-decoded source frames; running a second software-decode
    # pass purely for VMAF reference would double source-file I/O.
    # Skip silently here; the worker log already announces the decode
    # path, and the job report shows a clear skip message.
    if vmaf_enabled and hw_decode_active:
        print(
            f"[CONVERT] VMAF skipped — hardware decode is active for this job "
            f"(backend={_hw_backend}, on_device={_hw_on_device})",
            flush=True,
        )
        # Mark for the job report so the user sees WHY no score was computed.
        encoding_stats["vmaf_skipped_reason"] = "hardware_decode_enabled"
        vmaf_enabled = False  # suppresses the rest of the VMAF block

    if vmaf_enabled:
        ... existing block unchanged ...
```

(Find the actual `encoding_stats` dict — the existing code already writes VMAF scores to a stats dict; surface the skip reason through the same field. If the dict has a different name, adjust.)

- [ ] **Step 5: Syntax check + sanity test**

```bash
python3 -c "
import ast
with open('backend/converter.py') as f:
    ast.parse(f.read())
print('OK')
"
```

- [ ] **Step 6: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): wire HW decode settings into convert_file; skip VMAF when active"
```

---

## Task 6: Frontend — HW decode toggles in encoder cards

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/pages/SettingsPage.tsx`

- [ ] **Step 1: Extend `EncoderCaps` TypeScript type**

In `frontend/src/api.ts`, find the `EncoderCaps` interface (around line 600 — look for `getEncoderCaps`). Add the three new bools to wherever the type is declared.

If the type is declared inline or imported, locate it and add:

```typescript
export interface EncoderCaps {
  ...existing fields...
  nvdec_available: boolean;
  qsv_decode_available: boolean;
  vaapi_decode_available: boolean;
}
```

- [ ] **Step 2: Find the NVENC settings card**

In `SettingsPage.tsx`, locate the NVENC settings block (look for `encoding.default_encoder === "nvenc"`). Add a toggle row as the first item in that block:

```tsx
{/* v0.5.7: NVDEC hardware decode pairing */}
<div style={{ marginBottom: 16, padding: "10px 12px", backgroundColor: "var(--bg-secondary)",
              border: "1px solid var(--border)", borderRadius: 4 }}>
  <label style={{ display: "flex", alignItems: "center", gap: 10,
                  cursor: encoderCaps?.nvdec_available ? "pointer" : "not-allowed",
                  opacity: encoderCaps?.nvdec_available ? 1 : 0.5 }}>
    <input type="checkbox"
      checked={encoding?.nvenc_hw_decode ?? true}
      disabled={!encoderCaps?.nvdec_available}
      onChange={e => setEncoding({ ...encoding, nvenc_hw_decode: e.target.checked })}
      style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
    <span style={{ fontSize: 14, fontWeight: 500 }}>Use NVDEC for decode</span>
  </label>
  <div style={{ ...helpStyle, marginTop: 6 }}>
    Decodes the source on the GPU before NVENC encodes it. Frames stay on-device — no PCIe transfer. Falls back silently to software decode for unsupported source codecs (MS-MPEG4v3, exotic formats).
    {!encoderCaps?.nvdec_available && <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>NVDEC not detected on this host.</span>}
    {(encoding?.vmaf_analysis_enabled !== false) && (encoding?.nvenc_hw_decode ?? true) && (
      <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
        ⚠ VMAF won't run on jobs that use this decoder. See the VMAF section.
      </span>
    )}
  </div>
</div>
```

- [ ] **Step 3: Same pattern for QSV card**

Find the QSV settings block (`encoding.default_encoder === "qsv"`). Add the same toggle, substituting:
- `encoding?.qsv_hw_decode`
- `encoderCaps?.qsv_decode_available`
- Label "Use QSV for decode"
- Help text adjusted for QSV

- [ ] **Step 4: Same pattern for VAAPI card**

Find the VAAPI settings block. Same substitution.

- [ ] **Step 5: libx265 mixed-mode toggle**

In the libx265 card (the default branch when encoder is not nvenc/qsv/vaapi), add a similar toggle but with different copy:

```tsx
{/* v0.5.7: libx265 + NVDEC mixed mode (opt-in) */}
<div style={{ ...same outer styling }}>
  <label style={{ ...same with capability gate on encoderCaps?.nvdec_available }}>
    <input type="checkbox"
      checked={encoding?.libx265_use_nvdec ?? false}
      disabled={!encoderCaps?.nvdec_available}
      onChange={e => setEncoding({ ...encoding, libx265_use_nvdec: e.target.checked })} />
    <span style={{ fontSize: 14, fontWeight: 500 }}>Use NVDEC for decode (mixed mode)</span>
  </label>
  <div style={{ ...helpStyle, marginTop: 6 }}>
    Decodes the source on NVIDIA GPU (NVDEC), then transfers frames to CPU for libx265 encoding. Net win on slow CPUs paired with a dGPU; on modern CPUs the PCIe transfer overhead usually exceeds the savings. Defaults off.
    {(encoding?.vmaf_analysis_enabled !== false) && encoding?.libx265_use_nvdec && (
      <span style={{ color: "var(--warning)", display: "block", marginTop: 4 }}>
        ⚠ VMAF won't run on jobs that use this decoder. See the VMAF section.
      </span>
    )}
  </div>
</div>
```

- [ ] **Step 6: Visual check via running app or screenshot review**

(No automated test — frontend visual change.)

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/pages/SettingsPage.tsx
git commit -m "feat(settings): HW decode toggles inline with encoder cards"
```

---

## Task 7: VMAF warning chip in VMAF settings section

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx`

- [ ] **Step 1: Add warning chip below VMAF enable toggle**

Find the VMAF settings block (line 1198 area, around the `vmaf_analysis_enabled` checkbox). Compute the count of active HW decode toggles and render a warning when ≥ 1:

```tsx
{/* v0.5.7: HW decode / VMAF incompatibility surface */}
{(() => {
  const vmafOn = encoding.vmaf_analysis_enabled === true ||
                 encoding.vmaf_analysis_enabled === "true" ||
                 encoding.vmaf_analysis_enabled == null;
  if (!vmafOn) return null;
  const activeDecoders: string[] = [];
  if (encoding?.nvenc_hw_decode ?? true) activeDecoders.push("NVENC+NVDEC");
  if (encoding?.qsv_hw_decode ?? true) activeDecoders.push("QSV");
  if (encoding?.vaapi_hw_decode ?? true) activeDecoders.push("VAAPI");
  if (encoding?.libx265_use_nvdec) activeDecoders.push("libx265+NVDEC");
  if (activeDecoders.length === 0) return null;
  return (
    <div style={{
      marginTop: 12, padding: "10px 12px",
      backgroundColor: "rgba(255, 200, 80, 0.10)",
      border: "1px solid rgba(255, 200, 80, 0.45)",
      borderRadius: 4, fontSize: 13, color: "var(--text-primary)", lineHeight: 1.5,
    }}>
      <strong style={{ color: "var(--warning)" }}>⚠ VMAF will not run on hardware-decoded jobs.</strong>
      {" "}VMAF compares the encoded output to software-decoded source frames. With{" "}
      <strong>{activeDecoders.length}</strong> hardware decode toggle{activeDecoders.length > 1 ? "s" : ""}{" "}
      currently on ({activeDecoders.join(", ")}), jobs that use those encoders will skip VMAF — no score
      is computed and the quality threshold won't be applied. To enforce VMAF on every job,
      disable the hardware decode toggles in the encoder section above.
    </div>
  );
})()}
```

- [ ] **Step 2: Manual visual verification**

Toggle VMAF on, toggle one HW decode → expect chip with count "1". Toggle another → "2". Toggle all decoders off → chip disappears.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(settings): VMAF warning chip when HW decode toggles are active"
```

---

## Task 8: Final integration — VERSION, CHANGELOG, ship

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Smoke test the full encode path**

If a test box is available, manually queue a job with each encoder branch:
1. NVENC + NVDEC on, source = h264 → verify worker logs "Decode: CUDA (on-device) → Encode: nvenc"
2. NVENC + NVDEC on, source = msmpeg4v3 → verify worker logs "HW decode unavailable for codec 'msmpeg4v3' on NVDEC — software fallback"
3. libx265 + NVDEC mixed mode on → verify worker logs "Decode: CUDA + CPU readback"
4. VMAF on + any HW decode on → verify worker logs "VMAF skipped — hardware decode is active"

If no test box, mark this step done after the sanity tests in Task 4 + 5 pass.

- [ ] **Step 2: Bump VERSION**

```bash
echo "0.5.7" > VERSION
```

- [ ] **Step 3: Add CHANGELOG entry**

Prepend to `CHANGELOG.md` above the v0.5.6 entry:

```markdown
## [0.5.7] — 2026-05-11

### Added
- **Hardware decode support** (NVDEC / QSV / VAAPI) paired with each encoder. ... [full entry per pattern]
```

Write the entry in the existing CHANGELOG voice — root cause + symptom + fix + behaviour notes.

- [ ] **Step 4: Commit + tag + push**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: v0.5.7 — hardware decode (NVDEC / QSV / VAAPI)"
git tag v0.5.7
git push origin main
git push origin v0.5.7
```

---

## Acceptance checklist (from spec)

After all tasks complete, verify:

- [ ] NVENC+NVDEC, QSV+QSV decode, VAAPI+VAAPI decode toggles emit `-hwaccel <backend> -hwaccel_output_format <backend>` and use device-native scalers
- [ ] libx265+NVDEC toggle emits `-hwaccel cuda` only (no `_output_format`) and uses CPU scale
- [ ] Source codec gated via `hw_decode_supports()`; unsupported codecs fall back silently with worker log line
- [ ] VMAF skipped when HW decode is active for the job; reason surfaced in job report
- [ ] VMAF warning chip appears in Settings → VMAF when any HW decode toggle is on; dynamically counts active decoders
- [ ] Each HW decode toggle's help text shows "⚠ VMAF won't run" when VMAF is also enabled
- [ ] Capability bools (`nvdec_available`, `qsv_decode_available`, `vaapi_decode_available`) gate the toggles in the UI; "NVDEC not detected on this host" message renders when False
- [ ] Worker log shows decode/encode path for every job
- [ ] Existing software-decode users see no regression — defaults preserve pre-v0.5.7 cmd for software path
- [ ] Settings persist across container restart
