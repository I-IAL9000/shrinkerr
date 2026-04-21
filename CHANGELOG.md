# Changelog

All notable changes to Shrinkerr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- "Update available" sidebar button now uses the designer-drawn gift icon from the Figma design-system file instead of a hand-rolled lucide-style placeholder.

### Fixed
- Scanner page: the "Advanced" search button disappeared when the filter panel was expanded. It now renders in both collapsed and expanded layouts so you can open the advanced-query panel regardless of filter state.
- Scanner page: clicking a poster card in the `Corrupt` filter view no longer shows "No files found" when the card reports a non-zero file count. Files flagged corrupt by the health check (rather than by an ffprobe failure) were missing from the file-list response due to a dropped field in the backend row-enrichment step.

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

[0.3.0]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.0
