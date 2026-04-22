# Changelog

All notable changes to Shrinkerr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet. Bullets accumulate here as changes land, then get promoted
to a versioned release heading when we cut the next tag._

## [0.3.5] — 2026-04-22

### Fixed
- **README hero video now renders for everyone**, not just the repo owner. The previous `user-attachments` URL was uploaded via a draft issue-comment that was never actually posted, so the asset stayed gated to the uploader's GitHub session — `curl` from an unauthenticated client returned 404, and the `<video>` element showed a blank 0:00 player to every logged-out visitor. The README now points at `https://github.com/I-IAL9000/shrinkerr/releases/latest/download/hero.webm`, which is a public release-asset URL that always resolves to the most recent release. The release workflow automatically attaches `hero.webm` to every tagged release going forward, so the `/latest/` redirect keeps working without manual intervention.

## [0.3.4] — 2026-04-22

Quality-of-life follow-up to 0.3.3. Now that VMAF is producing correct
scores, there's no need for the conservative whole-file compare — this
release reverts to the fast 30-second sample and fixes the progress-
bar hang that was pinning the UI at 100% for several minutes during
analysis.

### Changed
- **VMAF analysis is ~50× faster on TV episodes.** Reverted the 0.3.3 whole-file compare (which was belt-and-suspenders insurance while we hunted the real cause of the wrong scores) back to a 30-second sample at 33% into the file, applied identically to both inputs via input-level `-ss` (accurate seek, no filter-level `trim`). The fps + colour-range normalisation from 0.3.3 is what actually fixed the scores, and it works just as well on a 30-second window as on the whole file. A 25-minute Croods episode now finishes VMAF in ~6 seconds instead of ~6 minutes.

### Fixed
- **VMAF progress bar no longer hangs at 100%.** Two bugs combined to freeze the progress indicator for the entire VMAF run:
  1. The code was seeding `progress=100` at the start of the VMAF phase as a "we're analysing" signal, then trying to reduce it as frames came in — except
  2. `-loglevel error` was suppressing ffmpeg's `frame=N fps=X …` progress output, so the frame-count parser never ran and progress stayed stuck at the seed value.
  Now seeds `progress=0`, adds `-stats` to force per-second progress output regardless of loglevel, and computes percentage against the real source fps from the probe (previously hardcoded to 24fps, which under-counted frames on 29.97/30fps content). The cross-check pass (when VMAF < 80) now also has its own progress phase labelled "Quality cross-check" instead of continuing to show a stale "VMAF analysis" at 99%.
- **ETA during VMAF.** The analysis fps and elapsed time are now shown via the progress callback, so the UI can surface something like "VMAF analysis — 90fps — 12s remaining" instead of a bare percentage.

## [0.3.3] — 2026-04-22

Follow-up release to chase down the last real-world VMAF failure mode:
animated content (The Croods et al.) was still scoring under 50 on
encodes that visually inspected as flawless. Adds proper stream
normalization in the VMAF filter graph, a whole-file compare for TV-
sized content, and a diagnostic cross-check so future suspicious scores
can be verified against independent metrics without guesswork. Also a
small UI wording fix for the VMAF-rejected case.

### Changed
- **VMAF filter graph now normalises frame rate and colour range on both streams.** Source fps is probed up front and used as the target for an `fps=fps=N` clause on both the reference and distorted streams, which guarantees matching frame counts regardless of VFR/CFR mix. Both streams also run through `scale=in_range=auto:out_range=tv` so a source-vs-encode range-tag mismatch can't silently stretch luma values (the classic "looks fine, scores 49" signature on some WEBDL captures). The side-by-side probe summary (`VMAF inputs — ref: 1920x1080 23.976fps yuv420p range=tv | dist: ...`) is logged before every VMAF run so any future mismatch is visible at a glance.
- **VMAF subprocess timeout now scales with analysed window** (`max(5 min, 3× duration)`). A full-file compare on a 45-minute episode can't time out any more.

### Added
- **SSIM + PSNR cross-check on suspicious VMAF scores.** Whenever VMAF reports under 80 on the mean, a second ffmpeg pass computes SSIM and PSNR over a 30-second sample. All three metrics are derived from the same pixel data but with different algorithms — if VMAF says "poor" but SSIM ≥ 0.98 or PSNR ≥ 40 dB, the encode is actually fine and VMAF is producing a measurement artefact (common on animation and other flat-coloured content, which is outside VMAF's training distribution). The log emits an explicit verdict line in that case: `→ SSIM/PSNR say the encode is actually fine; VMAF score is a measurement artefact`. The VMAF-threshold rejection still fires on the raw VMAF score — the cross-check is purely diagnostic — so users debugging a rejection can open the job's ffmpeg output, see the numbers, and decide whether to lower the threshold for this kind of content.

### Fixed
- VMAF analysis: bimodal scores on visually-identical encodes (e.g. sibling TV episodes scoring 49.5 and 96.3 at the same CQ and preset) traced to the filter-graph `trim=start:duration` approach picking different first frames in the reference vs encoded streams when the source had any of: VFR timestamps, non-zero container `start_pts`, interlaced fields, or keyframe-offset boundaries. Once the first frame misaligned, every subsequent pairwise compare was time-shifted and the score cratered. The 0.3.2 fix normalised pixel format and resolution but didn't touch temporal alignment, which is what was actually breaking. New behaviour:
  - For content ≤ 45 minutes (all TV episodes and most documentaries): VMAF compares the **whole file** — no trim, no sampling, no PTS math. The most reliable configuration possible.
  - For longer content (movies): sample a 90-second window at 33% into the file using input-level `-ss` (accurate seek in modern ffmpeg) applied identically to both inputs plus `-t` on the output — both streams are seeked to the same PTS *before* the filter graph sees them, so no in-filter trim is needed.
- History tab no longer labels VMAF-rejected jobs as "Converted (no savings)". A job whose encode scored below the minimum VMAF threshold is now recorded as `Kept original — VMAF below threshold`, which matches reality (the converted file was discarded and the source left in place). Encodes discarded because the output was larger than the source now read `Kept original — encode was larger than source` for the same reason. The plain `Converted (no savings)` label is reserved for real conversions that happened to break even on disk usage.

## [0.3.2] — 2026-04-21

VMAF-focused release. Fixes three real-world VMAF bugs observed in
production (bimodal scores on sibling episodes, score/event lost on
rename, no in-app way to verify the score per file) and closes a
UX gap in the update-notification system so new releases surface on
running containers without a manual image pull.

### Changed
- File-detail **History** tab now always shows the VMAF score when the file has one in `scan_results`, synthesising an entry if the original VMAF file-event is missing (older conversions pre-dating the logging feature, or events logged against a pre-rename path). Makes it easy to spot-check individual files surfaced by the VMAF filters without opening the job's full encoding log.
- Update-available notification now surfaces on the running container within ~30 minutes of a new GitHub release, no manual `docker compose pull` required. Previously the server-side cache window was 6 hours AND there was no background refresher, so the "Update available" pill could take most of a day to appear — or never, if the container happened to check just before the release. Matches how Sonarr / Radarr / Plex advertise updates. The Settings → Updates "Check for updates" button now also bypasses the cache so a manual click always reflects live GitHub state.

### Fixed
- VMAF analysis: reported suspiciously low scores (e.g. 43 on visibly near-transparent encodes; back-to-back same-show episodes at 57.7 and 97.7) due to pixel-format or resolution mismatch between the reference and encoded streams inside the libvmaf filter graph. The filter now explicitly normalises both sides to 8-bit `yuv420p` and uses `scale2ref` so resolution drift can't silently break the comparison. Also caps on the shortest stream with `shortest=1` so a trailing-frame discrepancy no longer inflates error. Logs now include min / max / harmonic-mean alongside the mean score, and emit a "distribution looks bimodal" warning when a sub-80 mean coexists with a ≥90 max — a signature of measurement artefacts rather than genuine quality loss.
- VMAF score / event no longer lost on files whose name changes during conversion (e.g. `x264` → `x265` rename): the backend was writing the score + event against the pre-rename path, so the updated `scan_results` row and the file-history query — both keyed on the post-rename path — never saw them. Both writes now use the post-rename path.

## [0.3.1] — 2026-04-21

A small quality-of-life release: one visual polish item and three bug
fixes, including a real-world VMAF-threshold regression that slipped
through in 0.3.0.

### Changed
- "Update available" sidebar button now uses the designer-drawn gift icon from the Figma design-system file instead of a hand-rolled lucide-style placeholder.

### Fixed
- Scanner page: the "Advanced" search button disappeared when the filter panel was expanded. It now renders in both collapsed and expanded layouts so you can open the advanced-query panel regardless of filter state.
- Scanner page: clicking a poster card in the `Corrupt` filter view no longer shows "No files found" when the card reports a non-zero file count. Files flagged corrupt by the health check (rather than by an ffprobe failure) were missing from the file-list response due to a dropped field in the backend row-enrichment step.
- VMAF threshold rejection: the minimum-score setting in Settings → Video was being saved correctly but never read by the encoder at encode time, so encodes with scores far below the threshold (even 43 vs a threshold of 85) would be accepted. The threshold is now honoured on every encode.

## [0.3.0] — 2026-04-21

First public tagged release. Focus areas: multi-platform Docker images, GPU
detection that's actually honest about what the host can do, a real VMAF
threshold feature, and serious UI performance wins during encoding.

### Added
- **Multi-arch Docker images.** `shrinkerr:latest` and `:edge` now build for
  both `linux/amd64` and `linux/arm64` — run Shrinkerr on a Mac, Raspberry Pi,
  ARM cloud VM, Windows WSL2, or any x86 Linux host. Ships with ffmpeg n7.1
  (stable) or master (bleeding edge).
- **NVENC image variants** (`:nvenc`, `:edge-nvenc`). Separate x86_64-only
  lineage built on the CUDA base image for NVIDIA hosts that want hardware
  HEVC encoding. Documented driver requirements (525.60.13+ for `:nvenc`,
  570+ for `:edge-nvenc`) and the Monitor page surfaces your running driver
  so any mismatch is obvious.
- **VMAF minimum-score threshold.** Settings → Video → "Reject encodes below
  a minimum VMAF score". When enabled, any encode whose measured score is
  below the threshold is discarded and the original is kept. Rejected jobs
  get a distinct amber "VMAF rejected" badge on the Completed tab with the
  exact score and threshold in the expanded detail.
- **Monitor page encoding-capability strip.** Each node card now shows what
  the node can actually encode (NVENC ✓ / libx265 ✓) and, when NVENC is
  unavailable, a specific human-readable reason (missing GPU, old driver,
  ffmpeg build has no hevc_nvenc, etc.).
- **Driver version reporting.** Shows your NVIDIA driver version alongside
  the GPU name so you can correlate with the ffmpeg SDK's driver floor.
- **`runtime: nvidia` compose docs.** The portainer compose template now
  documents the Linux vs Windows/Docker-Desktop GPU-passthrough differences
  inline, with copy-pasteable instructions for both.
- **Build variants via `FFMPEG_BUILD` arg.** Power users can rebuild the
  image pinned to a specific BtbN release (`n7.1`, `n8.1`, `master`, …).
- **`scripts/build-images.sh`** — one command to build all four image tags
  locally, with optional `MULTIARCH=1` for buildx cross-compile builds.
- **`.github/workflows/build-images.yml`** — automatically publishes all
  four tags to GHCR on every push to `main` and on `v*` tag pushes.
- **Remote worker nodes** — run a second container on any host with
  `SHRINKERR_MODE=worker`, advertise its capabilities (nvenc / libx265),
  and jobs get dispatched to it with capability-aware routing.
- **Autoplaying hero video in the README** — five-screen UI tour that
  loops seamlessly.

### Changed
- **Rebrand from Squeezarr to Shrinkerr.** All user-facing strings, DB
  filename (`squeezarr.db` → `shrinkerr.db` with transparent startup
  migration), backup folder name (`.squeezarr_backup` → `.shrinkerr_backup`
  with fallback reads), environment variable prefix (`SQUEEZARR_*` →
  `SHRINKERR_*` with fallback parsing), session cookie, download filenames,
  and notification subject lines. Backward-compat shims everywhere so
  existing deployments upgrade with zero config changes.
- **Default ffmpeg pinned to BtbN n7.1** (NVENC SDK 12.2, driver 525.60.13+)
  instead of rolling `master`. Covers a much wider driver range out of
  the box. Override via `--build-arg FFMPEG_BUILD=master` for bleeding edge.
- **README rewrite** with a proper features matrix, platform compatibility
  table, workflow walkthroughs, environment variable reference, and
  troubleshooting guide.
- **Apache 2.0 license** added.

### Fixed
- **NVENC falsely detected on CPU-only hosts.** A bug in the detection code
  let `hevc_nvenc` pass the capability check on machines with no NVIDIA GPU
  at all (macOS in particular). Detection now requires `nvidia-smi` to
  succeed *before* even attempting the NVENC test encode, and trusts ffmpeg's
  return code rather than parsing stderr for failure strings.
- **Queue page at 116% CPU + 1.9 GB RAM in Chrome during active encoding.**
  Four compounding issues: a document-wide `MutationObserver`, un-throttled
  WebSocket progress broadcasts, missing `React.memo` on list rows, and
  never-paused polling. Fixed all four; typical idle Queue page is now in
  the single-digit CPU range.
- **Dashboard page at 60% CPU during encoding.** Similar pattern: every
  WebSocket progress tick forced the four Recharts SVG surfaces to redraw
  from scratch. Extracted the live-status card into a memoized child,
  memoized `chartData`, and added visibility-aware polling.
- **VMAF score missing on half of concurrent encodes.** Two jobs running in
  parallel on files that shared a 20-character filename prefix (same-series
  TV episodes, same-franchise movies) both wrote libvmaf output to the
  same `/tmp/shrinkerr_vmaf/*_vmaf.json` path, clobbering each other. Now
  suffixed with a UUID fragment — zero collisions possible.
- **"Output file missing or empty after conversion" false failures** on
  flaky NFS / SMB mounts. Added a retry loop with directory-scan fallback
  for late-landed temp files, and clear diagnostic output when the source
  is intact so a failed job doesn't trigger downstream NZBGet/Sonarr
  cascades that blocklist the release.
- **NZBGet post-processing script exiting with code 1 on unhandled Python
  exceptions.** Wrapped `main()` in a try/except that exits `POSTPROCESS_NONE`
  instead — prevents NZBGet from marking a healthy download as broken.
- **NZBGet script download endpoint 404.** Endpoint was pointing at a
  non-existent `nzbget-extension/Squeezarr/main.py` path with the wrong
  placeholder tokens. Now correctly serves the real `Shrinkerr.py` template
  with matching `__SHRINKERR_URL__` / `__SHRINKERR_API_KEY__` substitutions.
- **`runtime: nvidia` missing from production compose** caused NVENC
  passthrough to silently no-op on many Linux hosts even when the Container
  Toolkit was correctly installed. Compose templates now document both
  `runtime: nvidia` AND `deploy.resources.reservations.devices` as the
  belt-and-suspenders Linux pattern.
- **Scanner "Quick check" and "Thorough check" buttons collapsed into one
  "Health check" dropdown** — same two modes, less button clutter.

### Security
- No user-facing security fixes in this release.

---

[0.3.5]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.5
[0.3.4]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.4
[0.3.3]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.3
[0.3.2]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.2
[0.3.1]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.1
[0.3.0]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.0
