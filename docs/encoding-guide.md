# Encoding guide

This is the "what do I actually pick" guide. For system-level setup see
[Installation](installation.md); for distributed encoding see
[Remote workers](remote-workers.md).

## Contents
- [NVENC vs libx265](#nvenc-vs-libx265)
- [NVENC presets and CQ](#nvenc-presets-and-cq)
- [NVENC bit depth](#nvenc-bit-depth)
- [libx265 presets and CRF](#libx265-presets-and-crf)
- [Hardware decode (NVDEC / QSV / VAAPI)](#hardware-decode-nvdec--qsv--vaapi)
- [FFmpeg threads per job](#ffmpeg-threads-per-job)
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

## NVENC bit depth

Settings → Video → Encoding → NVENC card → "Bit Depth". Three options:

| Option | Profile / pix_fmt | Use case |
|---|---|---|
| **10-bit** (main10 / p010le) | `-profile:v main10 -pix_fmt p010le` | Default. Best quality (reduces banding). Requires Pascal-or-newer NVIDIA (GTX 10xx, Quadro P-series, RTX). |
| **8-bit** (main / nv12) | `-profile:v main -pix_fmt nv12` | Maxwell-compatible (GTX 9xx, GTX 750 Ti, Quadro M-series). Smaller files on most material, faster encode. |
| **Match source** | Probes pix_fmt per job | 10-bit only when source is already 10-bit, else 8-bit. Avoids the 8→10 bit upconvert when input doesn't benefit. |

10-bit HEVC NVENC requires Pascal silicon (GTX 1050+ / Quadro P-series).
Maxwell silicon (GTX 9xx / 750 Ti / Quadro M-series) supports NVENC HEVC
but only 8-bit — pick the 8-bit option to make NVENC usable on those
cards. "Match source" is the recommended setting for mixed libraries —
8-bit Blu-ray rips encode as 8-bit (smaller, faster), 10-bit anime / 4K
HDR encodes as 10-bit (preserves source precision).

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

## Hardware decode (NVDEC / QSV / VAAPI)

Pre-v0.5.7 Shrinkerr always decoded sources on the CPU (libavcodec) even
when encoding on the GPU. v0.5.7 added hardware decode pairings so frames
can stay on the device — typical CPU load on a 2-job NVENC setup drops
from 80–90% to under 10%.

Each encoder card in Settings → Video has its own toggle:

| Toggle (encoder card) | Default | Pipeline |
|---|---|---|
| **Use NVDEC for decode** (NVENC) | On | `-hwaccel cuda -hwaccel_output_format cuda` + `scale_cuda` filter. Frames stay on the GPU. |
| **Use QSV for decode** (QSV) | On | `-hwaccel qsv -hwaccel_output_format qsv` + `scale_qsv`. Frames stay on the iGPU. |
| **Use VAAPI for decode** (VAAPI) | On | `-hwaccel vaapi -hwaccel_output_format vaapi` + `scale_vaapi`. Frames stay on the DRM device. |
| **Use NVDEC for decode (mixed mode)** (libx265) | Off | `-hwaccel cuda` then `hwdownload,format=nv12`. Decode on GPU, encode on CPU. Niche: slow CPU + fast dGPU. |

Each toggle disables itself in the UI when its matching hardware isn't
detected (checked via `ffmpeg -hwaccels` + the encoder probe).

**Codec gating.** Each hardware decoder supports a subset of source
codecs. When a job's source codec isn't supported (e.g. MS-MPEG4v3 on
NVDEC, MJPEG on VAAPI), Shrinkerr silently falls back to software decode
for that specific job and logs the fallback in the worker output:

```
[CONVERT] HW decode unavailable for codec 'msmpeg4v3' on NVDEC
          — software fallback for this job
```

No job failure, no UI noise — exotic codecs just take the slower path.

**VMAF interaction.** None (v0.9.135+). VMAF is measured in its own
ffmpeg pass that software-decodes a 30-second window of the original and
the encoded file, so it runs the same whether the encode used hardware
decode or not. (v0.5.7–v0.9.134 skipped VMAF on hardware-decoded jobs.)

**Distributed mode.** Hardware decode settings propagate from the server
to remote workers automatically — see
[Remote workers](remote-workers.md#per-node-controls). Each worker
respects the server's settings at job-dispatch time.

## FFmpeg threads per job

Settings → Video → Encoding → "FFmpeg Threads Per Job". Caps the
`-threads N` flag on ffmpeg. Default `0` = ffmpeg auto (uses all
available cores).

The default works fine for a single encode at a time. With `Parallel
Jobs > 1` on an older CPU, two libx265 processes each spawn N threads
and fight each other for cores — measurable throughput loss:

| Parallel Jobs | Threads (recommended) |
|---|---|
| 1 | 0 (auto) |
| 2+ on libx265 | 1–2 per job |
| 2+ on NVENC/QSV/VAAPI | 1–2 per job (GPU does the heavy lifting; ffmpeg just needs threads for muxing / filters) |

ffmpeg's `-threads` flag is per-codec-context — Shrinkerr emits it both
pre-input (caps decoder threads) and post-encoder (caps libx265 encoder
threads). NVENC/QSV/VAAPI ignore the flag, so the post-encoder copy is
harmless redundancy on hardware paths and a real cap on libx265.

## VMAF quality validation

[VMAF](https://github.com/Netflix/vmaf) is Netflix's perceptual quality
metric. Shrinkerr can run it automatically after each encode and reject
the output if the score is too low.

**Enable:** Settings → Video → Smart Encoding → "VMAF analysis" (on by
default — scoring only).

**Minimum score (`vmaf_min_score`):** "Reject encodes below a minimum
VMAF score". On by default at 88 for installs since v0.10.0 (installs
from before keep what they had); 0 disables rejection (VMAF still runs
and is reported, just never rejects). Typical values:

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

The band comes from the width as well as the height, so a 1920×800 scope
film is 1080p and 3840×1600 is 4K. Files scanned before v0.10.0 have no
stored width until the next full scan; until then their height and any
resolution tag in the name (`1080p`, `2160p`, …) decide.

The value becomes the job's NVENC CQ (libx265 gets CRF two higher) when no
encoding rule and no Add to Queue setting sets the quality, and the queue
estimate shows the same value. QSV, VAAPI and VideoToolbox jobs keep their
own quality setting.

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

Settings → Video → Smart Encoding → "Content type detection". Recognises
the content from the file and folder names (anime release groups and tags,
`grain`, animation studios, `remux`) and picks a CQ for that content and
the file's resolution band. The recommended values:

| Content | 4K | 1080p | 720p | SD |
|---|---|---|---|---|
| Anime | 23 | 19 | 18 | 16 |
| Animation | 23 | 19 | 18 | 16 |
| Film grain | 21 | 18 | 17 | 16 |
| Remux | 21 | 18 | 17 | 16 |
| *Everything else (resolution-aware)* | 24 | 20 | 18 | 16 |

Why these get a little more quality than everyday content: artifacts show
most where nothing hides them. Live action's texture masks quantisation;
anime and CGI animation are flat colour, smooth gradients and sharp lines,
where the same CQ shows as banding and ringing — and they compress so well
that the extra quality costs few bytes. Film grain is the first thing an
encoder smooths away (it turns waxy and blotchy), so keeping it costs bits
and grainy files save less. Remuxes are pristine sources kept for their
quality; they still shrink a lot at these values.

Every cell can be changed in the table under the setting, each type can be
switched off (it's then treated like everything else), and "Reset to
recommended" restores the values above. Anything else uses resolution-aware
CQ (if on) or your global CQ. Like resolution-aware CQ it sets NVENC CQ /
libx265 CRF only when no rule or Add to Queue setting does, and the
estimate shows the same values.

For more than quality — a different encoder, preset, resolution or audio
for one type — use an encoding rule with the **Content type** condition
("Rule…" next to each type creates one). A rule's quality wins over the
table.

Before v0.10.0 this (and resolution-aware CQ) only changed the queue
estimate; jobs always used the global CQ. It is on for new installs and was
switched off once on installs that existed before, so their output doesn't
change without warning — turn it on if you want it.

## Custom ffmpeg flags

Settings → Automation → Advanced → "Custom ffmpeg flags". Appended after the
built-in flags. Use with care — Shrinkerr's flag stack already covers
pixel format, container options, mapping, and so on.

Because these flags reach ffmpeg's command line, changing them requires
password auth (Settings → System → Authentication) and being signed in with
the password rather than the API key. Shrinkerr refuses flags that add
inputs or outputs (`-i`, `-f`, `-y`, an extra file name) or point at files
(paths, `-progress`, `-vstats_file`, `-attach`, …).

Common additions:
- `-b:v 4M` — force a specific bitrate (overrides CQ/CRF constant-quality
  mode; you probably want CQ/CRF instead)
- `-x265-params "aq-mode=3"` — libx265-specific tuning
- `-vf "unsharp=5:5:0.5"` — a light sharpen pass

If ffmpeg rejects a flag, the job fails with the stderr in `error_log`
viewable via the job detail.
