# Encoding guide

This is the "what do I actually pick" guide. For system-level setup see
[Installation](installation.md); for distributed encoding see
[Remote workers](remote-workers.md).

## Contents
- [NVENC vs libx265](#nvenc-vs-libx265)
- [NVENC presets and CQ](#nvenc-presets-and-cq)
- [libx265 presets and CRF](#libx265-presets-and-crf)
- [VMAF quality validation](#vmaf-quality-validation)
- [Resolution-aware CQ](#resolution-aware-cq)
- [Cross-encoder fallback settings](#cross-encoder-fallback-settings)
- [Source-codec filter](#source-codec-filter)
- [Content-type detection](#content-type-detection)
- [Custom ffmpeg flags](#custom-ffmpeg-flags)

## NVENC vs libx265

| | NVENC (GPU) | libx265 (CPU) |
|---|---|---|
| Speed | 100–500 fps (1080p on a decent NVIDIA GPU) | 5–30 fps (1080p, modern CPU) |
| Compression efficiency | Worse — ~25% larger files at the same quality | Better — the reference HEVC encoder |
| GPU load | High | None |
| CPU load | Low | All cores |
| Power draw per encode | Higher peak, shorter total | Moderate, longer total |
| Quality tuning knob | Preset (p1–p7) + CQ (15–30) | Preset (ultrafast–veryslow) + CRF (15–30) |
| Recommended for | High-volume re-encoding, recent NVIDIA GPUs | Small libraries, archival-quality, no GPU |

Rule of thumb: if you have an NVIDIA GPU, use NVENC. The ~25% file-size
penalty vs libx265 is usually worth the 10× speed, and you can always
re-encode anything that matters with libx265 later. For a small library
(say <500 titles) where you'd rather spend the extra CPU time for smaller
files, libx265 wins.

## NVENC presets and CQ

**Preset (p1–p7)**

NVENC's presets adjust quality vs. speed tradeoff. Unlike libx265, the
spread between them is small: p1 is only ~2× faster than p7 on current
cards, and the quality difference at the same CQ is under 5%.

| Preset | Label | Typical use |
|---|---|---|
| p1 | Fastest | Real-time streaming, tests |
| p2 | Very Fast | |
| p3 | Fast | Good default for batch re-encoding |
| p4 | Medium | |
| p5 | Slow | |
| p6 | Very Slow | Best quality without going extreme |
| p7 | Slowest | Diminishing returns beyond here |

**CQ (Constant Quality, 15–30)**

Lower = higher quality, larger files.

| CQ | Character | Notes |
|---|---|---|
| 18–20 | Transparent | Indistinguishable from source, largest files |
| 21–24 | Good | Savings noticeable, quality usually unaffected |
| 25–27 | Space-saver | Fine for most source material, some banding on flats |
| 28–30 | Aggressive | Visible macroblocks, only for space-constrained backfills |

**Starting point for most libraries**: `p3 / CQ 27`. Quick, competitive
with NVENC's efficiency sweet spot, and about 55–65% smaller files than
1080p x264 web-dl sources.

## libx265 presets and CRF

Unlike NVENC, libx265 preset cost is **exponential**. Bumping one notch
slower roughly doubles encode time at the same CRF:

| Preset | Speed on 1080p (M1 ballpark) | Relative cost |
|---|---|---|
| ultrafast | 80–120 fps | 1× |
| superfast | 60–90 fps | 1.3× |
| veryfast | 40–70 fps | 2× |
| faster | 25–45 fps | 3× |
| fast | 15–30 fps | 5× |
| medium | 8–15 fps | 10× |
| slow | 3–6 fps | 20× |
| slower | 1–3 fps | 40× |
| veryslow | <1 fps | 80×+ |

**CRF** behaves like NVENC's CQ — lower is higher quality, 15–28 is the
practical range.

**Sweet spots** (for 1080p x264 source targeting ~40% savings):
- Backfilling a big library in a reasonable timeframe: `fast / CRF 22`
- Quality-first on a small library: `slow / CRF 20`
- Archival masters: `veryslow / CRF 18`
- Budget / older hardware: `veryfast / CRF 24`

## VMAF quality validation

[VMAF](https://github.com/Netflix/vmaf) is Netflix's perceptual quality
metric. Shrinkerr can run it automatically after each encode and reject
the output if the score is too low.

**Enable:** Settings → Video → Smart Encoding → "VMAF analysis" on.

**Minimum score (`vmaf_min_score`):** 0 to disable rejection (VMAF still
runs and is reported, just never rejects). Typical values:

| Min score | Meaning | Reject rate on typical content |
|---|---|---|
| 0 | Report-only | 0% |
| 80 | Poor quality cutoff | ~1% |
| 87 | Good quality cutoff | ~3% |
| 93 | Excellent cutoff | ~10–15% |
| 95+ | Transparent-only | High — most real encodes land 90–96 |

When a job's VMAF falls below the threshold, Shrinkerr keeps the
original, marks the job with a VMAF rejection, and logs the reason.
Bumping CRF / CQ one or two steps lower (higher quality) and re-queuing
usually rescues it.

**Mechanics:**
- VMAF analyzes a 30-second sample from the middle of the file (at 33%
  duration). Faster than full-file analysis, correlates well in practice.
- On encoders that produce bimodal quality (rare — usually filter-chain
  mismatch issues), SSIM + PSNR cross-check kicks in automatically.
- Required: ffmpeg built with `libvmaf`. `:latest`, `:nvenc`, and the
  `:edge*` images all ship with it.

## Resolution-aware CQ

Settings → Video → Smart Encoding → "Resolution-aware CQ". When on, each
resolution band gets its own CQ value:

| Band | Typical value |
|---|---|
| 4K | 24 (grain retention matters more) |
| 1080p | 20 |
| 720p | 18 |
| SD | 16 |

Rationale: smaller pixels = artifacts more visible per-pixel, so push
quality higher on lower-resolution sources. Off by default; enable if
your library has a wide resolution mix.

## Cross-encoder fallback settings

Shrinkerr supports both encoders and can translate when a node doesn't
match the job's requested encoder. Two optional override pairs in
Settings → Video:

- **CPU fallback** (shown when default encoder is NVENC) — libx265 preset +
  CRF to use when a CPU worker picks up an NVENC job. Default: blank →
  auto-translate (see table in [Remote workers](remote-workers.md)).
- **GPU fallback** (shown when default encoder is libx265) — NVENC preset +
  CQ to use when a GPU worker picks up a libx265 job.

Pin these if you have mixed-capability workers and you want predictable
output across them.

## Source-codec filter

Settings → Video → "Convert From (source codecs)". Checkboxes for
`h264`/`mpeg2`/`mpeg4`/`vc1`/`msmpeg4v3`/`vp9`/`h265`/`av1`.

Defaults to h264, mpeg2, mpeg4, vc1 — i.e. re-encode old / inefficient
codecs, leave already-modern files alone. Uncheck what you don't want
Shrinkerr to touch.

Checking `h265` is the "re-encode everything, including existing HEVC"
mode — useful if you're migrating from one HEVC profile to another
(Main10 on NVENC, say). Most users leave it off.

## Content-type detection

Settings → Video → Smart Encoding → "Content type detection". Inspects
the first few frames of each file during scanning to detect animation /
cartoons and adjust the VMAF expectation: animated content compresses
very differently from live-action, and the same CRF can produce wildly
different perceptual quality. When on, the estimate UI shows
`type: animation` / `live-action` per file.

## Custom ffmpeg flags

Settings → Video → Advanced → "Custom ffmpeg flags". Appended after the
built-in flags. Use with care — Shrinkerr's flag stack already covers
pixel format, container options, mapping, and so on.

Common additions:
- `-b:v 4M` — force a specific bitrate (overrides CQ/CRF constant-quality
  mode; you probably want CQ/CRF instead)
- `-x265-params "aq-mode=3"` — libx265-specific tuning
- `-vf "unsharp=5:5:0.5"` — a light sharpen pass

If ffmpeg rejects a flag, the job fails with the stderr in `error_log`
viewable via the job detail.
