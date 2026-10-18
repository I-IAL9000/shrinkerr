# Encoding guide

This is the "what do I actually pick" guide. For system-level setup see
[Installation](installation.md); for distributed encoding see
[Remote workers](remote-workers.md).

## Contents
- [Quality presets](#quality-presets)
- [NVENC vs libx265](#nvenc-vs-libx265)
- [NVENC presets and CQ](#nvenc-presets-and-cq)
- [NVENC bit depth](#nvenc-bit-depth)
- [libx265 presets and CRF](#libx265-presets-and-crf)
- [QSV, VAAPI and VideoToolbox quality](#qsv-vaapi-and-videotoolbox-quality)
- [Hardware decode (NVDEC / QSV / VAAPI)](#hardware-decode-nvdec--qsv--vaapi)
- [FFmpeg threads per job](#ffmpeg-threads-per-job)
- [VMAF quality validation](#vmaf-quality-validation)
- [VMAF target quality](#vmaf-target-quality)
- [Before / after frames](#before--after-frames)
- [AV1 output](#av1-output)
- [HDR and Dolby Vision](#hdr-and-dolby-vision)
- [Resolution-aware CQ](#resolution-aware-cq)
- [Cross-encoder fallback settings](#cross-encoder-fallback-settings)
- [Source-codec filter](#source-codec-filter)
- [Content-type detection](#content-type-detection)
- [Savings estimates](#savings-estimates)
- [Custom ffmpeg flags](#custom-ffmpeg-flags)

## Quality presets

One set of five presets, from best quality to smallest files. Each is a
CQ on NVENC's scale, set as your encoder's own quality setting:

| Preset | NVENC CQ | libx265 CRF | QSV / VAAPI | VideoToolbox q:v | Typical savings |
|---|---|---|---|---|---|
| Max quality | 18 | 20 | 18 | 62 | ~35% |
| **Quality** | 20 | 22 | 20 | 60 | ~45% |
| Balanced | 23 | 25 | 23 | 55 | ~55% |
| Max savings | 26 | 28 | 26 | 50 | ~70% |
| Smallest | 29 | 31 | 29 | 40 | ~80% |

- The setup wizard offers the middle three, with Quality picked. NVENC's
  default CQ, 20, is Quality too.
- Queue → select jobs → **Video preset…** sets those jobs' quality only;
  the speed preset stays your Settings choice.
- Settings → Video → Conversion Guide lists them for your encoder. The
  savings are typical for H.264 sources until your own conversions teach
  the estimate ([Savings estimates](#savings-estimates)).

## NVENC vs libx265

| | NVENC (GPU) | libx265 (CPU) |
|---|---|---|
| Speed | 100–500 fps (1080p on a decent NVIDIA GPU) | 5–30 fps (1080p, modern CPU) |
| Compression efficiency | Worse — ~25% larger files at the same quality | Better — the reference HEVC encoder |
| GPU load | High | None |
| CPU load | Low | All cores |
| Power draw per encode | Higher peak, shorter total | Moderate, longer total |
| Quality tuning knob | Preset (p1–p7) + CQ (15–40) | Preset (ultrafast–veryslow) + CRF (15–28) |
| Recommended for | High-volume re-encoding, recent NVIDIA GPUs | Small libraries, archival-quality, no GPU |

Rule of thumb: if you have an NVIDIA GPU, use NVENC. The ~25% file-size
penalty vs libx265 is usually worth the 10× speed, and you can always
re-encode anything that matters with libx265 later. For a small library
(say <500 titles) where you'd rather spend the extra CPU time for smaller
files, libx265 wins.

New installs start on the best encoder the host has: NVENC, then
VideoToolbox, QSV, VAAPI, and libx265 last. If the saved encoder isn't on
this server, Settings → Video says which one runs instead, with that
encoder's own quality setting. [Hardware](hardware.md) lists what each
needs.

## NVENC presets and CQ

**Preset (p1–p7)**

NVENC's presets adjust quality vs. speed tradeoff. Unlike libx265, the
spread between them is small: p7 is only about 5× slower than p1 on the
same card, and the quality gap is small.

| Preset | Label | Typical use |
|---|---|---|
| p1 | Fastest | Real-time streaming, tests |
| p2 | Very Fast | |
| p3 | Fast | Good for batch re-encoding |
| p4 | Medium | |
| p5 | Slow | |
| p6 | Very Slow | The default. Best quality without going extreme |
| p7 | Slowest | Diminishing returns beyond here |

**CQ (Constant Quality, 15–40)**

Lower = higher quality, larger files.

| CQ | Character | Notes |
|---|---|---|
| 18–20 | Transparent | Indistinguishable from source, largest files |
| 21–24 | Good | Savings noticeable, quality usually unaffected |
| 25–27 | Space-saver | Fine for most source material, some banding on flats |
| 28–30 | Aggressive | Visible macroblocks, only for space-constrained backfills |

**Starting point for most libraries**: the defaults, `p6 / CQ 20` (the
Quality preset). For smaller files, raise CQ along the
[quality presets](#quality-presets): Balanced (23) or Max savings (26).
For faster encodes, p3–p5 lose little.

## NVENC bit depth

Settings → Video → Encoding Defaults → "Bit Depth" (shown when NVENC is
the default encoder). Three options:

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

**Sweet spots** (for 1080p x264 sources; CRFs from the
[quality presets](#quality-presets)):
- Most libraries: `medium / CRF 22` (Quality; `medium` is the default
  speed and the quality-per-minute sweet spot)
- Backfilling a big library in a reasonable timeframe: `fast / CRF 22`
- Quality-first on a small library: `slow / CRF 20` (Max quality)
- Archival masters: `slower / CRF 20`
- Budget / older hardware: `veryfast / CRF 25` (Balanced)

## QSV, VAAPI and VideoToolbox quality

Each has its own quality setting in Settings → Video → Encoding Defaults,
shown when it's the default encoder ([Hardware](hardware.md) says which
machines have them):

| Encoder | Quality | Default | Speed |
|---|---|---|---|
| Intel QSV | Quality (`global_quality`), 15–32, lower = better | 22 | Preset veryfast–veryslow (medium), optional look-ahead |
| VAAPI | Quantizer (QP), 15–32, lower = better | 22 | Compression level 0–7 (4) |
| VideoToolbox | Quality (`q:v`), 30–85, **higher** = better | 55 | None: fixed by the Mac |

Since v0.10.0 a job's own quality reaches them too: a queue preset, a
rule's CQ, content-type and resolution-aware CQ, and the VMAF target.
It's set on NVENC's CQ scale and translated: QSV and VAAPI take the same
number, VideoToolbox the nearest measured q:v (the
[presets table](#quality-presets) shows the pairs). The speed setting
always comes from Settings, and Add to Queue has no quality control for
these encoders.

QSV and VAAPI values aren't measured: a test encode on your own files is
the way to calibrate. Both encode 8-bit here, so they don't convert HDR
files ([HDR and Dolby Vision](#hdr-and-dolby-vision)).

## Hardware decode (NVDEC / QSV / VAAPI)

Pre-v0.5.7 Shrinkerr always decoded sources on the CPU (libavcodec) even
when encoding on the GPU. v0.5.7 added hardware decode pairings so frames
can stay on the device — typical CPU load on a 2-job NVENC setup drops
from 80–90% to under 10%.

Each encoder's options in Settings → Video → Encoding Defaults (shown
for the default encoder) have their own toggle:

| Toggle (encoder card) | Default | Pipeline |
|---|---|---|
| **Use NVDEC for decode** (NVENC) | On | `-hwaccel cuda -hwaccel_output_format cuda` + `scale_cuda` filter. Frames stay on the GPU. |
| **Use QSV for decode** (QSV) | On | `-hwaccel qsv -hwaccel_output_format qsv` + `scale_qsv`. Frames stay on the iGPU. |
| **Use VAAPI for decode** (VAAPI) | On | `-hwaccel vaapi -hwaccel_output_format vaapi` + `scale_vaapi`. Frames stay on the DRM device. |
| **Use VideoToolbox for decode** (VideoToolbox) | On | `-hwaccel videotoolbox`, H.264 and HEVC only. Frames come back to system memory, so the usual filters apply. |
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
[Remote workers](remote-workers.md#settings-propagated-to-workers). Each worker
respects the server's settings at job-dispatch time.

## FFmpeg threads per job

Settings → Video → Encoding Defaults → "FFmpeg Threads Per Job". Caps the
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

**Enable:** Settings → Video → Smart Encoding → "VMAF quality analysis" (on by
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

A rule can set its own minimum (rule form → Languages, originals and more
→ **Minimum VMAF**: blank uses this one, 0 never rejects); see
[Rules](rules-and-automation.md#actions-and-overrides).

To try a setting before queueing, Add to Queue → **Test Encode (30s
sample)** encodes 30 seconds from a third of the way in exactly as the job
would (encoder, preset, quality, bit depth, hardware decode, resolution and
the file's rule) and reports the VMAF score and the savings.

VMAF is one of several checks before an output replaces the original. An
output shorter than the source (a read that died on a network share) is
thrown away too, and a conversion whose ffmpeg shows no progress for 30
minutes (a stalled share) is stopped. The original stays either way. All
the checks: [What happens to your files](what-happens-to-your-files.md#the-checks-before-anything-is-replaced).

**Mechanics:**
- VMAF analyzes a 30-second sample from the middle of the file (at 33%
  duration). Faster than full-file analysis, correlates well in practice.
- On encoders that produce bimodal quality (rare — usually filter-chain
  mismatch issues), SSIM + PSNR cross-check kicks in automatically.
- Required: ffmpeg built with `libvmaf`. `:latest`, `:nvenc`, and the
  `:edge*` images all ship with it.

## VMAF target quality

**Settings → Video → "Target a VMAF score".** Instead of one quality
setting for every title, Shrinkerr finds each title's own: before the
encode it takes three 10-second samples (at 20 %, 50 % and 80 % of the
file), encodes them at a few quality settings and scores each with VMAF,
then encodes the whole file at the lowest quality whose samples still
average the target. Grainy films get more bits, clean animation fewer —
the way [ab-av1](https://github.com/alexheretic/ab-av1)'s CRF search
works.

- **Target:** 80–99, 93 by default: the score Shrinkerr counts as
  transparent, where the encode looks the same as the original. The setup
  wizard's **Automatic** quality turns this on at 93.
- **The search** runs on NVENC's CQ scale from 14 to 34 (mapped to each
  encoder's own setting, e.g. libx265's CRF is CQ + 2), starting from the
  job's quality, and needs about five rounds of samples. If even CQ 14
  misses the target, CQ 14 is used.
- **It replaces** the quality from presets, rules and content type for
  that file. The job's details show the quality chosen and the samples'
  score ("VMAF target: CQ 25: samples scored 93.4 (target 93)").
- **Cost:** a few minutes per file on a GPU encoder; more on libx265
  (fifteen-odd sample encodes at your preset).
- **Not searched:** files under two minutes, discs, and ffmpeg builds
  without libvmaf — they keep the job's quality. A failed sample also
  falls back to it.
- The post-encode check and **minimum score** still apply to the whole
  file.
- Remote workers run the search themselves.

## Before / after frames

Each conversion grabs the same three moments (at 20 %, 50 % and 80 % of
the film) from the original and the converted file, while both still
exist. Open a finished job in Queue → Completed and click **Compare before
/ after**: drag across the picture (or use the slider) to move the
divider, and switch between the moments.

- Frames are JPEGs up to 960 pixels wide, shown with square pixels (an
  anamorphic DVD looks as it plays). The last 100 jobs keep theirs, about
  0.5 MB each, in the data folder.
- Remote workers grab them too and send them with the result.
- Not for discs. Turn it off in Settings → Video (**Keep before / after
  frames**).

## AV1 output

HEVC is the default output. An **encoding rule** can choose **AV1**
instead (Settings → Automation → Rules → Output codec) for the files it matches — for
example, a rule for animation. The encode uses the AV1 encoder of the
rule's (or the default) encoder family:

| Encoder | AV1 with | Needs |
|---|---|---|
| libx265 (CPU) | SVT-AV1 (`libsvtav1`), 10-bit | any CPU; the images include it |
| NVENC | `av1_nvenc` | an RTX 40 series or later GPU |
| QSV | `av1_qsv` | Intel Arc or Core Ultra (Meteor Lake) or later |
| VAAPI, VideoToolbox | — | not supported: encoded HEVC |

Shrinkerr checks the hardware ones with a one-frame test encode, since
ffmpeg lists them on GPUs that can't run them. Where a family has no AV1
here, files are encoded HEVC — the rule editor warns about it, and the
job's details say which codec was used.

- **Quality:** set on the HEVC scale and translated. SVT-AV1's CRF is
  libx265's + 8 (that matched VMAF on test clips); NVENC's CQ is rescaled
  to AV1's 0–63 range; QSV keeps its value. The last two are estimates —
  [VMAF target quality](#vmaf-target-quality) finds the right one per
  title.
- **Presets:** libx265's preset maps to SVT-AV1's (`medium` → 7, `slow`
  → 6, `veryslow` → 4, `fast` → 8 …).
- **File names** get an `AV1` tag where the source codec was named
  (`x264` → `AV1`).
- Only files that need converting are encoded; existing HEVC and AV1 files
  aren't re-encoded unless you tick them in
  [Convert From](#source-codec-filter).

## HDR and Dolby Vision

**HDR10 and HLG** files keep their HDR: they're encoded 10-bit with the
colour signalling carried over, and an output that lost it (an encoder or
ffmpeg build that drops it) or came out 8-bit (NVENC's Bit Depth set to
8-bit) is thrown away and the original kept. Quick
Sync and VAAPI encode 8-bit here, so they don't convert HDR files at all.

**Dolby Vision** profiles 7 and 8 keep their Dolby Vision. Shrinkerr
encodes the HDR10 (or HLG) base layer as usual, and
[dovi_tool](https://github.com/quietvoid/dovi_tool) moves the Dolby Vision
metadata (the RPU) from the original into the new file. Profile 7 becomes
profile 8.1: its enhancement layer isn't kept, as most players ignore it.
Every frame's metadata goes back with its frame, and a job that ends up
with a different number of frames, or loses Dolby Vision on the way, keeps
the original instead.

A Dolby Vision file's video is kept as it is when the file is:

- profile 5: Dolby Vision without HDR10 under it, so a re-encode turns it
  purple and green (a file with Dolby Vision and HDR10 is profile 7 or 8);
- resized (a rule's or the queue's target resolution) or encoded to AV1;
- encoded with Quick Sync or VAAPI;
- Blu-ray / DVD discs;
- on an install without dovi_tool and mkvmerge (both images include them;
  on a native install, install them yourself).

The job still does its other work on the original (removing tracks, Fix
for direct play: SRT copies, a stereo track), completes with **Video kept**
and the reason, and the file is ignored so it isn't queued again.

**Settings → Video → Dolby Vision** chooses what happens to profiles 7 and 8:

- **Keep it** (the default): as above.
- **Keep it when possible, otherwise HDR10**: when it can't be kept,
  Dolby Vision is dropped and the HDR10 (or HLG) under it is converted —
  resizing a 4K remux to 1080p gives a 1080p HDR10 file.
- **Convert to HDR10**: always dropped; the conversion is faster and needs
  no room for three times the video.

Profile 5 has no HDR10 under its Dolby Vision and is never converted.

Dolby Vision files are HEVC, so they're only converted when HEVC is among
the codecs you convert from (Settings → Video). Keeping Dolby Vision needs
room for about three times the converted video next to the output while the
job runs.

## Resolution-aware CQ

Settings → Video → Smart Encoding → "Resolution-aware quality". When on, each
resolution band gets its own CQ value:

| Band | Typical value |
|---|---|
| 4K | 24 (4K hides artifacts more) |
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
estimate shows the same value. QSV, VAAPI and VideoToolbox jobs get it
translated to their own setting (QSV and VAAPI take the same number,
VideoToolbox the nearest measured q:v).

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
`h264`/`mpeg2`/`mpeg4`/`vc1`/`msmpeg4v3`/`vp9`/`vp8`/`mpeg1`/`flv`/`h263`/`mjpeg`/`hevc`/`av1`.

Defaults to h264, mpeg2, mpeg4, vc1 — i.e. re-encode old / inefficient
codecs, leave already-modern files alone. Uncheck what you don't want
Shrinkerr to touch.

Checking `hevc` is the "re-encode everything, including existing HEVC"
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
CQ (if on) or your global CQ. Like resolution-aware CQ it sets the job's
quality only when no rule or Add to Queue setting does, and the
estimate shows the same values.

For more than quality — a different encoder, preset, resolution or audio
for one type — use an encoding rule with the **Content type** condition
("Rule…" next to each type creates one). A rule's quality wins over the
table.

Before v0.10.0 this (and resolution-aware CQ) only changed the queue
estimate; jobs always used the global CQ. It is on for new installs and was
switched off once on installs that existed before, so their output doesn't
change without warning — turn it on if you want it.

## Savings estimates

The Scanner, Add to Queue, policies, the "skip if savings below" rule
setting and the quality presets all use the same estimate. It starts from
typical results: a percentage of the file's size for the quality you
chose (CQ 20 saves about 45 %).

What a conversion actually saves depends a lot on the file's bitrate: a
30 Mb/s remux shrinks far more than a 4 Mb/s WEB-DL. So Shrinkerr learns
from your own finished conversions. It groups files by bits per pixel and
source codec and adjusts the estimate for each group. A group with few
conversions of its own leans on the broader one, so a handful of unusual
files can't skew it.

- It learns from conversions that kept the audio as it was, encoded HEVC
  and used a fixed quality (not a VMAF target), so the size change is the
  video's. An output that came out larger counts as no savings.
- The estimates update within about ten minutes of conversions finishing,
  and within a minute of a change to the default quality.
- Add to Queue and Settings → Video → Conversion Guide say how many
  conversions it learned from.
- A rule's **Skip if savings below (%)** leaves a file's video alone when
  this estimate is smaller; its track cleanup still runs.

## Custom ffmpeg flags

Settings → Automation → Advanced → "Custom ffmpeg flags". Appended after the
built-in flags. Use with care — Shrinkerr's flag stack already covers
pixel format, container options, mapping, and so on.

Because these flags reach ffmpeg's command line, changing them requires
sign-in to be set up (Settings → System → Authentication) and doing it in
the web app rather than with the API key. Shrinkerr refuses flags that add
inputs or outputs (`-i`, `-f`, `-y`, an extra file name) or point at files
(paths, `-progress`, `-vstats_file`, `-attach`, …).

Common additions:
- `-b:v 4M` — force a specific bitrate (overrides CQ/CRF constant-quality
  mode; you probably want CQ/CRF instead)
- `-x265-params "aq-mode=3:rd=4:psy-rd=2.0:no-sao=1"` — libx265 tuning.
  It replaces Shrinkerr's own `-x265-params aq-mode=3:rd=4:psy-rd=2.0`, so
  repeat what you want to keep. Other encoders ignore it (ffmpeg logs a
  warning).
- `-vf "unsharp=5:5:0.5"` — a light sharpen pass. A `-vf` replaces
  Shrinkerr's own filter chain: scaling to the target resolution, the
  hardware-decode filters (`scale_cuda`, `hwdownload`…) and VAAPI's
  `hwupload`. Use it only with no target resolution, hardware decode off
  and an encoder other than VAAPI, or the job fails or isn't resized.

If ffmpeg rejects a flag, the job fails with the stderr in `error_log`
viewable via the job detail.
