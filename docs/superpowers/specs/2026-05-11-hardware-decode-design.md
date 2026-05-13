# Hardware Decode (NVDEC / QSV / VAAPI) Design Spec

**Version target:** v0.5.7
**Status:** Design — awaiting implementation plan
**Brainstormed via Q&A:** scope, fallback, VMAF interaction, UI layout all locked

---

## Background

GitHub feature request: NVDEC / hardware decoding. Currently Shrinkerr does **GPU encode + CPU decode** on every path. On older CPUs paired with NVIDIA GPUs (the requester's exact setup: Xeon gen 4 + NVENC card), the CPU decode side becomes the bottleneck when `parallel_jobs > 1`: each ffmpeg process eats 4–8 cores just for `libavcodec` decoding while NVENC sits half-idle on the GPU.

The fix: emit `-hwaccel <type>` flags before `-i` so the decoder runs on hardware, matched per-encoder to keep frames on the device:

| Encoder | Decode pairing | PCIe transfer? | Default in v0.5.7 |
|---|---|---|---|
| **NVENC** | NVDEC (`-hwaccel cuda -hwaccel_output_format cuda`) | None — stays on GPU | **On** |
| **QSV** | QSV decode (`-hwaccel qsv -hwaccel_output_format qsv`) | None — stays on iGPU | **On** |
| **VAAPI** | VAAPI decode (`-hwaccel vaapi -hwaccel_output_format vaapi`) | None — stays on DRM device | **On** |
| **libx265** (CPU) | NVDEC + readback (`-hwaccel cuda` only, no `_output_format`) | Yes — GPU→CPU per frame | **Off** (opt-in) |

The first three pairs are net-positive in all realistic scenarios — frames never leave the device, so the only cost is potential codec gating (NVDEC doesn't support every source format). The fourth case (libx265 + NVDEC) is the requester's exact niche: an old CPU that decodes slowly enough to make the PCIe readback cost worth paying. Defaults to off because it's a tradeoff most users won't benefit from.

## Scope (locked via Q&A)

- **All four toggles** ship in v0.5.7 (not phased).
- **Silent CPU fallback** when source codec isn't supported by the chosen HW decoder. Log line in worker output; no UI noise.
- **Skip VMAF** for jobs that use HW decode (VMAF reads software-decoded frames; running both would require a second software decode pass).
- **UI: inline with each encoder section** — toggle sits in the same Settings card that has CQ/CRF/preset for that encoder.

## Design

### Settings model (4 new keys)

```python
# backend/routes/settings.py — _ENCODING_DEFAULTS additions
"nvenc_hw_decode": "true",      # default on
"qsv_hw_decode": "true",        # default on
"vaapi_hw_decode": "true",      # default on
"libx265_use_nvdec": "false",   # default off — cross-bus readback, opt-in
```

```python
# backend/models.py — EncodingSettingsUpdate additions
nvenc_hw_decode: Optional[bool] = None
qsv_hw_decode: Optional[bool] = None
vaapi_hw_decode: Optional[bool] = None
libx265_use_nvdec: Optional[bool] = None
```

GET response builder echoes them as bools; PUT handler stores `"true"`/`"false"` strings. Standard pattern.

### Capability detection (encoder_caps.py)

Add three boolean probes — surfaced both to gate the toggles in the UI and to log a clear warning when a toggle is on but the hardware isn't available:

```python
@dataclass
class EncoderCaps:
    ...existing...
    nvdec_available: bool       # NVDEC decoder probe
    qsv_decode_available: bool  # iGPU + Intel Media Driver
    vaapi_decode_available: bool # DRM render node + VA driver
```

Probe approach for each:
- **NVDEC**: `ffmpeg -hide_banner -hwaccels` lists `cuda`. If we already have `nvenc` available, NVDEC is effectively guaranteed (same GPU, same driver). Reuse the existing NVENC probe; expose a separate bool for clarity in UI.
- **QSV decode**: `ffmpeg -hide_banner -hwaccels` lists `qsv` AND we have an Intel render node. Same gate as `qsv_encode_available`.
- **VAAPI decode**: same gate as `vaapi_encode_available`.

In practice all three are tied to the existing encoder probes — if NVENC works, NVDEC works on the same GPU. The separate bools are defensive (a future driver split could change this).

### Codec gating (NEW helper)

Each HW decoder supports a subset of source codecs. Add `hw_decode_supports()` to converter.py — invoked once per job during `convert_file()` after the source codec is probed:

```python
# Conservative codec lists — what each HW decoder actually accepts.
# Based on NVIDIA Video Codec SDK 12.x / Intel Media Driver / VA-API current.
NVDEC_SUPPORTED = {"h264", "hevc", "h265", "vp9", "av1", "av01",
                   "mpeg2video", "mpeg2", "mpeg4", "vc1", "wmv3"}
QSV_DECODE_SUPPORTED = {"h264", "hevc", "h265", "vp9", "av1", "av01",
                        "mpeg2video", "mjpeg"}
VAAPI_DECODE_SUPPORTED = {"h264", "hevc", "h265", "vp9", "av1", "av01",
                          "mpeg2video", "vc1", "wmv3"}

def hw_decode_supports(decoder: str, source_codec: str) -> bool:
    """Return True if `decoder` can hardware-decode `source_codec`."""
    table = {
        "cuda": NVDEC_SUPPORTED,
        "qsv": QSV_DECODE_SUPPORTED,
        "vaapi": VAAPI_DECODE_SUPPORTED,
    }
    return source_codec.lower() in table.get(decoder, set())
```

If `hw_decode_supports()` returns False for the job's source codec, `_build_ffmpeg_cmd_impl()` silently omits the `-hwaccel` flags and logs `[CONVERT] HW decode unavailable for codec '<x>', falling back to software decode for this job`. No job failure, no UI surface.

### Filter chain rework

The current filter chain assumes software-decoded frames. With HW decode on and frames staying on the device, scaling needs to happen on the same device:

**Current state:**
- NVENC: `-vf scale={scale}` (software scale → upload happens internally in NVENC)
- QSV: `-vf scale={scale}` (software scale → upload happens internally in QSV)
- VAAPI: `-vf scale={scale},format=nv12,hwupload` (software scale → GPU upload via filter)
- libx265: `-vf scale={scale}` (software scale, all CPU)

**With HW decode on (v0.5.7):**
- NVENC + NVDEC: `-vf scale_cuda={scale}` (or omit if no scale — frames flow directly to encoder)
- QSV + QSV decode: `-vf scale_qsv={scale}` (or omit if no scale)
- VAAPI + VAAPI decode: `-vf scale_vaapi={scale},format=nv12` (or `-vf format=nv12` if no scale — frames already on GPU, no hwupload needed)
- libx265 + NVDEC: `-vf hwdownload,format=nv12,scale={scale}` if scaling, else `-vf hwdownload,format=nv12` — download to CPU, then software scale

**Edge case:** if `target_resolution = "copy"` AND HW decode is on AND encoder is NVENC/QSV/VAAPI, no `-vf` chain is needed at all — frames flow from decoder straight to encoder on the same device.

### NVENC pix_fmt interaction

Current NVENC config: `-profile:v main10 -pix_fmt p010le` (10-bit output, requested at encode time so libavcodec converts).

With NVDEC on, source pix_fmt determines what NVDEC produces (`nv12` for 8-bit sources, `p010le` for 10-bit). NVENC accepts both. The existing `-pix_fmt p010le` on the output side forces 10-bit output regardless — that stays. No change needed.

### VMAF interaction

VMAF requires software-decoded source frames to compare against the encoded output. With HW decode on, frames are GPU-resident.

**v0.5.7 behaviour (per Q&A):** if `vmaf_analysis_enabled` AND the job uses HW decode (either because the encoder pair is on, OR `libx265_use_nvdec` is on), skip VMAF for that job. Job report shows `VMAF skipped: hardware decode enabled` in the same slot the score would render. No score-based rejection happens (the VMAF threshold gate is bypassed).

**Implementation:** in `convert_file()`, compute `hw_decode_active = bool(...)` per job. When deciding whether to run VMAF, gate on `not hw_decode_active`. Surface the skip reason in the conversion log so the user understands.

**User-facing communication (REQUIRED — must be impossible to miss):**

1. **VMAF settings section** (`SettingsPage.tsx`, VMAF block) — add a yellow/warning info chip directly under the VMAF enable toggle that's visible whenever ANY HW decode toggle is on:

   > ⚠ VMAF runs on software-decoded source frames. Jobs that use hardware decode (NVENC+NVDEC, QSV+QSV, VAAPI+VAAPI, or libx265+NVDEC) will skip VMAF — the score won't be computed and the quality threshold won't be applied. To enforce VMAF on every job, disable the hardware decode toggles in the encoder section above.

   The chip dynamically counts which decode toggles are currently on ("3 of 4 encoders") so users see the immediate impact.

2. **Each HW decode toggle** (in the encoder cards) — when the toggle is being flipped to ON and VMAF is also currently enabled, append a sub-line to the toggle's help text:

   > ⚠ VMAF won't run on jobs that use this decoder. See the VMAF section.

3. **Job-report skip line** — already covered: jobs that skipped VMAF show `VMAF skipped: hardware decode enabled` in the expanded job view, in the same slot the score would otherwise render.

This three-surface approach guarantees a user can't enable HW decode + VMAF without knowing they're mutually exclusive at job time, and a user wondering why their VMAF threshold isn't catching low-quality encodes will find the explanation at every place they'd look.

**Future option (out of scope for v0.5.7):** run a second software-decode pass purely for VMAF reference frames, costing 1 extra source-file read per job. Captured as a TODO comment near the skip site.

### UI layout (inline with encoder cards)

The Encoding settings page has per-encoder configuration grouped by toggling the active encoder dropdown. Each encoder branch (NVENC, QSV, VAAPI, libx265) renders its CQ/preset/quality controls in a card. Add the HW decode toggle as the **first row** of each card, above CQ/preset:

```
┌─ NVENC ────────────────────────────────┐
│ ☑ Use NVDEC for decode (NEW)           │  ← v0.5.7
│   Faster on older CPUs. Falls back to  │
│   CPU decode for unsupported codecs.   │
│                                         │
│ CQ:        [20]                         │
│ Preset:    [p6 ▾]                       │
└─────────────────────────────────────────┘
```

Same shape for QSV (`Use QSV for decode`) and VAAPI (`Use VAAPI for decode`).

For libx265, the toggle is labeled `Use NVDEC for decode (mixed mode)` with help text:

> Decodes the source on NVIDIA GPU (NVDEC), then transfers frames to CPU for libx265 encoding. Net win on slow CPUs paired with a dGPU; on modern CPUs the PCIe transfer overhead usually exceeds the savings. Defaults off.

If the corresponding capability bool is false (`nvdec_available`, etc.), the toggle is **disabled** with a help line "NVDEC not detected on this host." Same pattern as other capability-gated controls in the codebase.

### Worker logging

Every job logs one line at the start of `_build_ffmpeg_cmd_impl()` describing the decode path chosen:

```
[CONVERT] Decode: NVDEC (cuda) → Encode: NVENC (hevc_nvenc) — frames stay on GPU
[CONVERT] Decode: software (libavcodec h264) → Encode: NVENC — HW decode disabled in settings
[CONVERT] Decode: software (codec mpeg4 unsupported by NVDEC) → Encode: NVENC — fallback
[CONVERT] Decode: NVDEC + readback → Encode: libx265 (CPU) — mixed mode opt-in
```

Provides debuggability when users wonder "is my GPU actually being used."

## Out of scope for v0.5.7

- **AMD/Intel cross-vendor HW decode** (e.g. QSV decode + NVENC encode). Not requested, edge case.
- **Software-decode VMAF pass** when HW decode is on. Tracked as TODO comment.
- **Per-job override** (job-level "force software decode" toggle). Setting-level granularity is sufficient.
- **Profile/level gating** (e.g. 10-bit H.264 source on NVDEC consumer cards). Fall back to software via the same `hw_decode_supports` path if needed; refine the table over time based on user reports.

## Risk surface

| Risk | Mitigation |
|---|---|
| Codec support tables wrong for some user's hardware | Conservative defaults (well-tested codecs only); fallback to software is silent and logged |
| Filter chain regression for existing software-decode users | Toggles default the way current behaviour works (or with the obvious improvement) — pre-v0.5.7 path is preserved when toggle is off |
| VMAF threshold gate bypassed silently | Skip is logged in the job report; users opting into HW decode are explicitly trading the quality gate for speed |
| libx265 + NVDEC slower than pure CPU on modern systems | Defaults off; help text explains the tradeoff |
| HW decode toggles persist after hardware removed | Capability bool gates the toggle in the UI; backend logs a fallback line if toggle is on but hardware unavailable |

## Acceptance criteria

1. **Each native HW pair toggle** (NVENC+NVDEC, QSV+QSV, VAAPI+VAAPI) adds the matching `-hwaccel` flag and filter chain when on, and is omitted entirely when off.
2. **Source codec probe** runs before cmd assembly; unsupported codecs fall back silently with a log line.
3. **VMAF is skipped** with a clear job-report message when any HW decode path is active for that job, AND the incompatibility is surfaced in the UI at three places (VMAF settings warning chip with dynamic toggle-count, encoder-card help text when both VMAF and HW decode are enabled, job-report skip message).
4. **libx265 + NVDEC mixed mode** works end-to-end on a NVENC-capable host; toggle is independent of `nvenc_hw_decode`.
5. **Capability probes** correctly disable each toggle in the UI when the hardware isn't present.
6. **No regression** for users who leave all toggles at default — pre-v0.5.7 behaviour preserved bit-for-bit (default-off for libx265+NVDEC; default-on for native pairs means slightly faster but otherwise equivalent encodes).
7. **Worker log** shows the decode/encode path for every job.
8. **Settings persist** across container restart; PUT handler validates and stores; GET response builder echoes.
