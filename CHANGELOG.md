# Changelog

All notable changes to Shrinkerr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.28] — 2026-04-23

### Security

Phase 1 of a security hardening pass — closes the most severe findings from the internal audit. No breaking changes for existing installs; a few previously permissive defaults tighten up on fresh installs.

#### Fixed
- Auth middleware no longer fails **open** when the settings DB read raises — returns 503. A transient SQLite lock could previously disable auth for the whole process.
- Auth middleware now enforces when a non-empty `api_key` is configured, independent of the `auth_enabled` flag. The old behaviour gated only when password auth was toggled on, so setting a key without flipping the toggle left the app wide open.
- `api_key` is masked (`****xxxx`) in `/api/settings/encoding` GET, matching every other stored secret. Dedicated `GET /api/settings/api-key` returns the unmasked key on demand for the Settings → System page and the copy-to-clipboard button.
- Integration endpoints (`/api/webhooks/*`, `/api/nodes/*`, `/api/settings/backup/{download,restore}`, `/api/settings/nzbget-config`, `/api/settings/{nzbget,sabnzbd}-script`) always require an API key — even when `auth_enabled=false`. Previously a LAN-exposed install handed out RCE-adjacent primitives to anyone who could reach port 6680.
- `/api/settings/dirs` POST now validates the path: must be absolute, must be an existing directory, must not be the filesystem root or under `/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/dev`, `/app/data`. Stops an attacker bypassing every downstream containment check by adding `"/"` as a media directory.
- `backup_folder` setting validated the same way. Stops the conversion pipeline from being coerced into renaming originals into privileged directories.
- `/api/scan/delete-file` containment check rewritten with `Path.resolve()` + `os.path.commonpath` — the old `startswith` check was defeatable by `"/media/../etc/hostname"` (literally starts with `/media/`).
- `/api/webhooks/{scan,queue}` and `/api/jobs/add-by-path` now verify every supplied path resolves inside a configured media directory before running ffprobe/ffmpeg.

#### Added
- Fresh installs auto-generate a strong `api_key` + `session_secret` and enable password auth on first startup. The generated key is printed once, prominently, to the container logs.
- Existing installs with both `api_key=""` and `auth_enabled=false` now get a loud `[SECURITY]` warning banner on every startup so the operator knows they're running unauthenticated.

## [0.3.27] — 2026-04-23

### Changed
- Setup wizard: reordered to Add directories → Scan library → Customize setup (optional) → Start converting, now that the bundled TMDB key means scanning gives posters + native-language detection on the first run without any prior configuration.
- Setup wizard: renamed the "Add connections" step to "Customize your setup" — reframes it as optional polish (linking Plex / Jellyfin / Sonarr / Radarr, encoder tuning, your own TMDB key) rather than a required connections step.
- Installation doc updated to match the new step order and to explain bundled-vs-own-TMDB-key trade-offs.

## [0.3.26] — 2026-04-23

### Changed
- Settings → Metadata APIs reframed now that TMDB ships with a bundled key. When a bundled key is active the copy explains TMDB already works and the input is labeled optional; the connection indicator distinguishes "using bundled key" vs "using your key".

## [0.3.25] — 2026-04-23

### Added
- TMDB non-commercial API key now baked into the published images (`:latest` / `:nvenc` / `:edge` / `:edge-nvenc`). Fresh installs get poster artwork and native-language detection without the user having to register with TMDB first — user-saved keys in Settings still win. Key comes from the `TMDB_API_KEY` GitHub secret at build time via a `--build-arg` into both Dockerfiles; local self-builds without the secret behave as before.
- TMDB attribution in Settings → Support, per TMDB's non-commercial API terms of use.

## [0.3.24] — 2026-04-23

### Changed
- Setup wizard step order: Add directories → **Add connections** → Scan library → Start converting. TMDB connection now comes before scanning so posters and native-language detection are populated on the first scan instead of missing until the next full rescan.

### Added
- `SHRINKERR_TMDB_API_KEY` environment variable — acts as a bundled fallback TMDB key when no user key is configured. Lets image maintainers (or self-builders) ship a non-commercial key so fresh installs get posters / metadata lookups without the user having to register with TMDB first. User-saved key always wins. `tmdb_key_source` is now returned on the settings GET so the UI can distinguish user-supplied vs bundled vs absent.

## [0.3.23] — 2026-04-23

### Added
- Support section in Settings with links to the documentation, GitHub repo, and issue tracker. Surfaces the new `/docs/` tree from inside the app so users don't have to go hunting for the README.

## [0.3.22] — 2026-04-23

### Fixed
- Checkmark finally renders on every checkbox (was missing in the Settings page and in the File tree). Two scoped selectors (`.settings-page input[type="checkbox"]` and `.poster-accordion input[type="checkbox"]:checked`) were setting `background:` shorthand at higher specificity than the base `:checked` rule, silently clearing the SVG tick image. Removed the scoped background overrides — base rule now styles every checkbox uniformly.

## [0.3.21] — 2026-04-23

### Fixed
- Checked checkboxes in light mode now show their tick. The old `::after` rotated-border checkmark was brittle; replaced with an inline-SVG background image. Also fixed the light-mode `:checked` override using the `background` shorthand (which was clearing the SVG) in favour of `background-color`.

## [0.3.20] — 2026-04-23

### Changed
- Light mode overhauled. New palette tuned for WCAG AA contrast (muted text, accent and status colors darkened), buttons / filter pills / sort pills / job-type badges now render as light grey instead of stark white, codec badges use pale-tinted backgrounds with colored text instead of heavy solid blocks, and a safety rule catches components with hardcoded `color: white` inline so section labels no longer disappear. Logo wordmark swaps to a dark variant in light mode.

## [0.3.19] — 2026-04-23

### Added
- Symmetric "GPU fallback" preset + CQ fields in Settings → Video (libx265 section), matching the "CPU fallback" pair. Lets libx265-first users pin specific NVENC settings for when a GPU-capable worker picks up a libx265 job. Worker now also only forwards main NVENC defaults when `default_encoder` is nvenc — mirror of the libx265 side.

## [0.3.18] — 2026-04-23

### Added
- "CPU fallback" preset + CRF fields in Settings → Video (NVENC section). When set, they override the NVENC→libx265 translation for CPU workers — lets NVENC-first users pin a specific libx265 profile for CPU fallback without changing their primary encoder.

## [0.3.17] — 2026-04-23

### Fixed
- Server now only ships its `libx265_preset` / `libx265_crf` values to a remote worker when libx265 is the configured default encoder. NVENC-first servers were previously leaking the shipped hardcoded libx265 defaults (`medium / CRF 20`) to CPU workers, short-circuiting the NVENC→libx265 translation of the user's actual NVENC settings.

## [0.3.16] — 2026-04-23

### Fixed
- Queue page's "Starting…" placeholder cards no longer appear for paused / offline worker nodes. Capacity is now summed from nodes that can actually pick up work, instead of blindly using the global `parallel_jobs` setting.

## [0.3.15] — 2026-04-23

### Fixed
- Remote CPU worker translating an NVENC job with no per-job encoder settings now uses the server's global NVENC defaults (e.g. `p3 / CQ 27`) instead of the old hardcoded `p6 / CQ 20` fallback — the translated libx265 output matches the user's actual quality target.

## [0.3.14] — 2026-04-23

### Fixed
- "Add a remote worker" snippet on the Nodes page now references the published GHCR images (`:nvenc` for GPU, `:latest` for CPU) instead of the non-existent `shrinkerr:latest` local build tag, so the copy-paste command actually pulls something.

## [0.3.13] — 2026-04-23

### Changed
- Node settings → "NVENC ↔ libx265 comparison table" now reflects the quality-matched translation (CRF = CQ, presets capped at `slow`) so the UI matches the actual worker behaviour.

## [0.3.12] — 2026-04-23

### Changed
- NVENC→libx265 translation on CPU workers now targets similar perceptual quality instead of similar file size: CRF matches the NVENC CQ value 1:1, so libx265's extra per-bit efficiency shows up as a smaller file rather than a quality drop. A typical `p3 / CQ 27` job now runs as `libx265 veryfast / CRF 27`.

## [0.3.11] — 2026-04-23

### Fixed
- Remote CPU workers handed an NVENC job no longer pick catastrophic libx265 settings. The old translation mapped `nvenc p6 / CQ 20` to `libx265 slower / CRF 16` — a near-lossless preset 40× slower than `fast`. Workers now prefer the server's configured libx265 defaults, fall back to a conservative translation capped at `slow`, and use `CRF = CQ + 3` (matching libx265's higher per-bit efficiency, instead of the inverted sign the old code had).

## [0.3.10] — 2026-04-23

### Fixed
- VMAF analysis now handles filenames with apostrophes, spaces or brackets (e.g. "Grey's Anatomy - S01E01"). The derived log path was previously inlined raw into ffmpeg's `-filter_complex` and the apostrophe opened an unbalanced quoted region, so libvmaf exited non-zero and the score was silently dropped.

### Changed
- Remote worker nodes now honour the server's `vmaf_analysis_enabled` / `vmaf_min_score` settings (previously hardcoded off).
- VMAF failures are now surfaced on the Activity page with the ffmpeg error, not just the successful scores.
- Converter always logs its VMAF decision (`enabled=… raw setting=…`) and the exception traceback on failure, so "VMAF didn't run" is diagnosable from docker logs without rerunning.

## [0.3.9] — 2026-04-22

### Changed
- Default encoder is now auto-picked from detected hardware on first launch — CPU-only boxes land on libx265 instead of NVENC.
- Always-keep audio/subtitle languages and NZBGet/SABnzbd tags & categories no longer come pre-populated.
- Settings → Updates card now uses the logomark instead of the shrunk full logo.
- Scanner empty state no longer shows an endless "Loading files..." spinner on fresh installs.
- Setup wizard: larger logomark, reworded intro, square icon tiles, and Plex step broadened to "Add connections" (TMDB / Plex / Jellyfin / Sonarr / Radarr) deep-linking to Settings → Connections.
- Queue page's video-preset dropdown now follows the selected default encoder (libx265 preset / CRF vs NVENC preset / CQ).

### Fixed
- Dashboard no longer hangs on the loading spinner on fresh installs, so the setup wizard renders as intended.
- Loose files directly in a media root now each get their own poster card and are individually selectable, instead of being collapsed under the root folder.
- Estimate modal's "Auto" encoder now respects the saved default encoder — previously it always routed CPU/libx265 installs through the NVENC UI.
- libx265 preset override from the estimate modal is now actually applied to queued jobs (the field was being dropped).

## [0.3.8] — 2026-04-22

### Fixed
- Settings page no longer spawns a page-wide scrollbar on short viewports.

## [0.3.7] — 2026-04-22

### Changed
- README hero is now an animated WebP instead of a WebM video, so it autoplays on GitHub.

## [0.3.6] — 2026-04-22

### Fixed
- README hero video now renders for unauthenticated visitors (the previous URL was session-gated).

## [0.3.5] — 2026-04-22

### Fixed
- Attempted fix for the README hero video that didn't land (see 0.3.6).

## [0.3.4] — 2026-04-22

### Changed
- VMAF analysis is much faster on TV episodes — reverted to a 30-second sample now that 0.3.3's normalisation fixed the accuracy problem.

### Fixed
- VMAF progress bar no longer hangs at 100%; now shows fps + ETA during analysis.

## [0.3.3] — 2026-04-22

### Changed
- VMAF filter graph now normalises frame rate and colour range on both streams, so scores are accurate regardless of VFR/CFR mix or range-tag drift.

### Added
- SSIM + PSNR cross-check runs automatically on any VMAF score below 80, so you can tell a real quality regression from a VMAF measurement artefact (common on animation / flat-coloured content).

### Fixed
- VMAF no longer produces bimodal scores (e.g. sibling TV episodes scoring 49 and 96 at identical settings).
- History tab no longer labels VMAF-rejected jobs as "Converted (no savings)" — now reads "Kept original — VMAF below threshold".

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

[0.3.28]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.28
[0.3.27]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.27
[0.3.26]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.26
[0.3.25]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.25
[0.3.24]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.24
[0.3.23]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.23
[0.3.22]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.22
[0.3.21]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.21
[0.3.20]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.20
[0.3.19]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.19
[0.3.18]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.18
[0.3.17]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.17
[0.3.16]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.16
[0.3.15]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.15
[0.3.14]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.14
[0.3.13]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.13
[0.3.12]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.12
[0.3.11]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.11
[0.3.10]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.10
[0.3.9]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.9
[0.3.8]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.8
[0.3.7]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.7
[0.3.6]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.6
[0.3.5]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.5
[0.3.4]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.4
[0.3.3]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.3
[0.3.2]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.2
[0.3.1]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.1
[0.3.0]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.0
