# Changelog

All notable changes to Shrinkerr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.68] — 2026-04-28

### Added
- Settings → Encoding now exposes Intel QSV and Intel/AMD VAAPI as encoder options (auto-detected; only shown when the host can run them). Per-encoder preset and quality controls follow the NVENC / libx265 pattern. Compose-passthrough hint and a "Re-detect" button included.

## [0.3.67] — 2026-04-28

### Added
- Foundation for Intel QSV and Intel/AMD VAAPI hardware encoding — VA-API runtime baked into both Docker images, encoder detection, ffmpeg command builder, settings defaults, completed-job report labels. Settings UI / rule overrides / multi-node advertisement land in v0.3.68 and v0.3.69.

## [0.3.66] — 2026-04-28

### Fixed
- "Update available" modal now shows the actual new release notes from GitHub instead of re-rendering your installed CHANGELOG.md (which only goes up to your installed version).
- Modal force-refreshes the version check on open so the "latest is vX.Y.Z" header reflects the freshest GitHub data, not a 30-min-stale cache value.
- "LATEST" badge now lights up on the entry whose version actually matches the upstream latest, instead of always tagging the topmost local entry.

## [0.3.65] — 2026-04-28

### Fixed
- Fresh-install Docker containers now land on the setup wizard instead of a login screen with no obvious key (auth starts disabled; loud `[SECURITY]` banner prompts you to enable it in Settings before exposing the port).

## [0.3.64] — 2026-04-27

### Added
- Scanner now updates live when the watcher discovers new files — the NEW filter (and the rest of the file tree) refreshes without you having to navigate away and back.

## [0.3.63] — 2026-04-27

### Fixed
- Add-to-queue toast no longer says "all already queued" when new items were actually added (`cursor.lastrowid` is `None` after `executemany` per Python sqlite3 — read `MAX(id)` after the insert instead).

### Performance
- First-time poster resolution for a batch of new items now runs 8 paths in parallel instead of one-at-a-time, cutting initial render of ~30 new items from 30+ s to a few seconds.

## [0.3.62] — 2026-04-27

### Fixed
- "Update available" button no longer disappears on the local-network URL when it shows on the remote URL — `/stats/version` is now `Cache-Control: no-store` on both server and client.

## [0.3.61] — 2026-04-27

### Fixed
- Bulk-added items now land in the queue in alphabetical order by file path so episodes group by show/season instead of arriving in random order.

## [0.3.60] — 2026-04-27

### Fixed
- "No items added" toast appearing after the bulk add even when items were successfully queued (regression from v0.3.57's executemany refactor).
- Toast now says "All N items were already in the queue" when every selection was a dup, and "Added N (M already queued)" when some were and some weren't.

## [0.3.59] — 2026-04-27

### Documentation
- Trimmed recent changelog entries to one-liners and adopted that style going forward.

## [0.3.58] — 2026-04-27

### Documentation
- Updated the VMAF analysis settings copy to match reality.

## [0.3.57] — 2026-04-27

### Added
- Loading overlay while adding many items to the queue.

### Fixed
- Bulk queue add is now much faster.

## [0.3.56] — 2026-04-27

### Fixed
- TMDB matching now honors `[tvdb-N]`/`[tt…]` IDs even when the show has no poster on TMDB.
- Title-search fallback no longer runs when an explicit ID is present, preventing wrong-show guesses.

## [0.3.55] — 2026-04-27

### Removed
- Disabled the subtitle-prestrip pre-pass that caused a long wait before encoding multi-sub files.

### Added
- Completed-job report now shows the original-vs-encoded comparison for "ignored — encode was larger" outcomes.

## [0.3.54] — 2026-04-26

### Fixed
- Removed trailing `0` after the VMAF tier label in the completed-job report.

## [0.3.53] — 2026-04-26

### Fixed
- Hidden `Scan=off` media directories from the Scanner dropdown.

## [0.3.52] — 2026-04-25

### Documentation
- Documented the NZBGet/SABnzbd setup prerequisites in the Settings UI and `docs/rules-and-automation.md`.

## [0.3.51] — 2026-04-26

### Fixed
- API error toasts now show the backend's actual reason instead of `API error: 400`.

## [0.3.50] — 2026-04-26

### Fixed
- "+ Add" on Settings → Media directories now shows an error toast on failure instead of doing nothing.

## [0.3.49] — 2026-04-26

### Added
- Per-media-dir "Scan" toggle — webhook-eligible without the scanner crawling it (for NZBGet/SABnzbd landing zones).

## [0.3.48] — 2026-04-26

### Fixed
- **Renaming section's "Save" button moved to the left** to match every other section in Settings. Encoding, audio, lossless, etc. all use `alignSelf: "flex-start"` for their save buttons; only `RenamingSettings.tsx` had `justifyContent: "flex-end"`. Now consistent.

## [0.3.47] — 2026-04-26

### Fixed
- **Completed-job report showed `x265 (NVENC)` instead of `h265 (NVENC)`.** The codec label in the expanded job details was hardcoded as `x265` regardless of the encoder, even though v0.3.30's rename rule already distinguished between them: libx265 → `x265` (the specific encoder), NVENC → `h265` (the codec spec, encoder-agnostic). The same rule now applies to the report so the on-screen label matches the renamed output filename.

## [0.3.46] — 2026-04-26

### Fixed
- **Conversion failure on files with orphan VobSub `.sub` external subtitles** (`ffmpeg exited with code 254`, `[vobsub @ ...] Unable to open <name>.sub as MPEG subtitles` followed by `Error opening input file <name>.idx: No such file or directory`). VobSub external subs are a paired format — `.idx` (index/metadata) + `.sub` (bitmap data) — and ffmpeg's vobsub demuxer auto-resolves the partner from disk. If only one half of the pair exists, the demuxer fails and the whole encode aborts. `detect_external_subtitles` was including every `.sub` and `.idx` it found, so an orphan crashed the job. Now: `.sub` files are represented via their `.idx` partner (ffmpeg picks up the `.sub` from the same stem), and an orphan `.sub` *or* orphan `.idx` is skipped with a clear log line. Plain text-format `.sub` files (subviewer) are also skipped — rare in the wild compared to vobsub, and skipping one text track is much less harmful than failing the entire encode.

## [0.3.45] — 2026-04-25

### Fixed
- **Settings → Encoding → Parallel jobs now syncs to the local node's `max_jobs`.** Pre-v0.3.45 the two settings represented the same thing (capacity for the in-process worker queue) but lived in separate DB rows, with the per-node value silently winning. Changing the global slider from the Settings page didn't propagate, so users editing parallel jobs there saw their change ignored at runtime. Both sides now stay in sync: changing the global slider on Settings → Encoding updates the local node's `max_jobs`, and changing the local node's max from Nodes → Settings updates the global setting. Remote nodes are still configured per-node — they reflect per-host hardware.

## [0.3.37–0.3.44] — 2026-04-25

### Progress-reporting overhaul (eight iterations, several reverts)

Long debugging chain to fix progress bars stalling on certain WEBDLs (Brotherhood, Borgen, Breathless). Recorded as a single entry rather than eight because most of the intermediate releases were misdiagnoses that got rolled back when they didn't help.

**Net effect vs v0.3.36 — what shipped and stayed:**

- **Frame-counter fallback in `parse_ffmpeg_progress`** (added v0.3.43, finalized v0.3.44). The actual root cause: ffmpeg's `time=` field reflects the muxer's *committed-output* position, which on some files stalls behind the encoder for the entire encode (most commonly when `-c:a copy` passes through audio with non-monotonic PTS that the muxer can't commit). The parser was time-only and ignored `frame=`, so when the muxer's clock was stuck or `N/A`, progress was stuck even though the encoder was happily producing frames. Now parses both fields every progress line and uses whichever yields *higher* progress — `frame=` is always honest, `time=` can lag arbitrarily. Confirmed in the field: same Borgen file went from "stuck at 6.43% with fps climbing to 397" to smooth advancement.

- **Pre-strip pass for files with ≥6 subtitle removals** (v0.3.39). Two-pass workflow: a fast `-c copy` remux drops unwanted subs first, then the main encode runs on a clean 5-7 stream file. Originally diagnosed as the cause of the stall (it wasn't — the parser was) but kept because it's independently useful for the genuine "32+ subtitle streams in a WEBDL" case where the muxer's per-stream interleave queue does fill up.

- **`backend.scanner.probe_file`** now exposes `video_fps` so the converter can compute total expected frames as the divisor for the parser's frame-count fallback.

**Reverted along the way** — kept here for archaeology since several were active in published images:

- `-fflags +flush_packets` (added v0.3.37, removed v0.3.38) — forced per-packet flushes, ~20% throughput cost.
- `-max_muxing_queue_size 9999` (added v0.3.37, removed v0.3.42) — bigger muxer queue weakened ffmpeg's natural back-pressure between encoder and muxer.
- Fire-and-forget DB writes via `asyncio.create_task` in `progress_cb` (added v0.3.40, removed v0.3.41) — same back-pressure problem; removed the await that mediated concurrent NVENC sessions politely sharing the GPU.

**Lessons noted for next time:** when only *some* files stall, the cause is usually in parsing/reporting, not the encoder. A single-job-at-a-time test is the cleanest way to rule out concurrency before chasing GPU contention or I/O hypotheses. Files where `frame=` advances but `time=` parks behind it are surprisingly common in WEBDL → `-c:a copy` flows.

## [0.3.36] — 2026-04-25

### Fixed
- **Per-job progress bars stuck for ~60s at a time, then jumping in big increments — even after the v0.3.35 WebSocket fix.** Inverting the ETA formula on stuck jobs showed both jobs' last DB-persisted progress was always ~57 seconds old simultaneously, while ffmpeg was happily burning ~380% CPU on each. Diagnosis: every ffmpeg progress line triggered an `aiosqlite.connect → UPDATE → commit → close` cycle for that job's row in `jobs`. Under contention from any other transaction holding the WAL write lock (some 60-second periodic loop in the codebase), all four progress writers (2 jobs × 2 lines/sec) queued behind it, blocked the converter's progress callback, and back-pressured ffmpeg's stderr buffer until the lock released and the queued events flushed in a burst. Fix: decouple DB persistence from live-UI updates. WebSocket broadcast keeps firing on every progress line (already throttled to 500ms per job server-side); the DB write is now throttled to once every 3 seconds per job, with a guaranteed flush on terminal progress (≥99.99) so the persisted final value is never off. Same throttle applied to the remote-worker `report_progress` HTTP path (2-second interval) so busy nodes don't flood the server. Smoke-tested: 25 WS broadcasts vs 5 DB writes over 12 seconds, terminal flush guaranteed.

## [0.3.35] — 2026-04-25

### Fixed
- **Progress bars stuck for minutes, then jumping in big increments.** WebSocket `broadcast()` was awaiting `send_json` *serially* for each connected client. Any slow / half-dead client (background browser tab, mobile on weak signal, Tailscale tunnel with packet loss, stale connection from a tab that didn't close cleanly) blocked every other client for as long as that one connection took to time out at the TCP layer. While blocked, every job's progress callback queued behind it; when the slow connection finally drained, the queued events flushed at once, manifesting as a progress bar jumping from e.g. 2% to 26% after several minutes of standstill. Fix: send to all connections in parallel via `asyncio.gather`, with a 2-second per-connection timeout. A sluggish client gets dropped after 2 seconds and the rest of the broadcasts complete uninterrupted. Smoke-tested with one 10-second-stalled connection alongside two healthy ones — the broadcast now returns in 2.0s (was 10s) and the healthy clients receive every message.

## [0.3.34] — 2026-04-25

### Fixed
- **Watcher log spam from AppleDouble companion files.** On Mac-formatted volumes (HFS+/APFS shared via SMB/AFP), every `.mkv` has a sibling `._<name>.mkv` resource fork that carries the same extension but contains HFS metadata, not video. The watcher walk used to include them, ffprobe rightly failed on them, and `[WATCHER] Skipped: 0 ignored, 200 probe failed, 0 AV1` would fire every cycle. Watcher's directory walk now skips dotfiles (`name.startswith(".")`), matching `scanner.py`'s pre-existing filter. Files already cached in the in-memory `_probe_failures` set fall out naturally on the next cycle since they're no longer in `disk_files`.
- **`[WATCHER] Pre-filtered: …` log deduplicated.** A stable backlog (e.g., 600 always-failing files plus zero new content) used to repeat the exact same numbers in the log every 5 minutes — non-actionable noise. Now only emits when the `(ignored, probe_failures, to_process)` tuple changes since last cycle.

## [0.3.33] — 2026-04-25

### Fixed
- **Dropdown arrows missing on Activity / Logs / Schedule pages and a few modals.** Inline `style={{ background: "..." }}` shorthand on `<select>` elements was wiping the global `background-image: <chevron>` rule from `theme.css`, so the carat indicator never rendered. Switched the affected callsites to `backgroundColor:` instead so the global chevron survives. Same root cause as the v0.3.22 checkbox-checkmark fix.
- **Type=Other media directories no longer trigger TMDB lookups.** When you add a folder and pick "Other" from the type dropdown, the scanner / watcher / metadata-refresh / poster-resolution paths now skip TMDB matching for files inside it. Previously a folder of home videos labelled "Other" would still get matched against TMDB's catalogue, producing spurious posters and original-language tags. New helper `backend.media_paths.is_other_typed_dir(path)` is the single gate; case-insensitive on the label.

## [0.3.32] — 2026-04-25

VMAF measurement reliability + activity log readability.

### Fixed
- **VMAF "bimodal desync" measurement bug.** libvmaf occasionally desynced its frame-pair iteration partway through the 30-second analysis window — half the frames scored ~100, half scored ~0, the recorded mean landed somewhere in between (e.g. 39.5 or 61.6) and a visually-fine encode was reported as "Poor". Diagnosis: see `min=0.0 max=100.0` in the converter log. Fix: detect the bimodal signature (min < 20 ∧ max ≥ 90) and re-run VMAF at an alternate seek (66% / 33% of duration, ≥60s away from the primary). Take the higher of the two scores. If every pass came back bimodal, persist the score but flag the new `jobs.vmaf_uncertain` column so the UI can surface a ⚠ glyph next to the score and a tooltip explaining "measurement-suspect — encode is almost certainly visually fine."
- **eac3 (Dolby Digital Plus) decoder warnings** (`expacc N is out-of-range`, `error decoding the audio block`) added to the health-check benign allow-list. These trip on streaming-service rips (HBO Max / Apple TV+ / etc.) but the audio plays fine in any real player. Files that previously got flagged "Corrupt (quick)" purely on these messages are now classified "warnings" and stay healthy for queue/auto-ignore.

### Added
- **Re-measure suspect VMAF scores** button in Settings → Encoding → VMAF. Iterates completed jobs whose recorded score landed below the "Excellent" tier or got flagged uncertain, and re-runs VMAF (with the same bimodal-aware retry path used for fresh encodes). Skips jobs whose original pre-rename source no longer exists on disk (typical when "delete original after conversion" is on). Live progress streams over the WebSocket; existing 30+ bad scores can be cleaned up without re-encoding anything.
- **Activity log + History tab now colour-code outcomes** rather than always-green:
    - `Health check: corrupt` → red, `warnings` → amber, `healthy` → green.
    - `VMAF: <score>` → green / amber / red based on the canonical 3-tier table; uncertain measurements get amber regardless of the underlying score.
- **Canonical 3-tier VMAF table everywhere** (FilterBar, JobListItem, EstimateModal, FileDetail, DashboardPage donut, EventTimeline, SettingsPage threshold). Excellent (93+) → green, Good (87–93) → amber, Poor (<87) → red. Backend mirrors the same cuts in `backend/queue.py`, `backend/test_encode.py`, `backend/routes/stats.py`. The previous "Fair" tier (80–87) was inconsistent across components — folded into Poor. `frontend/src/utils/vmaf.ts` is the new single source of truth.

### Changed
- `convert_file`'s VMAF block extracted into `_run_libvmaf_pass()` and `remeasure_vmaf()` helpers in `backend/converter.py`. Cuts ~150 lines of inline ffmpeg-spawning duplication and lets the remeasure endpoint share the exact same filter pipeline + bimodal-detection logic as fresh encodes.
- Cross-check (SSIM/PSNR) now runs against the same window that produced the chosen VMAF score, not always the primary seek — so a rescued retry-window score is sanity-checked at *its* window, not the bimodal one.

## [0.3.31] — 2026-04-24

Follow-up release cleaning up rough edges from the v0.3.30 migration.

### Added
- **Path mappings editor in the Node Settings modal.** Admin-editable override that takes precedence over the worker's `PATH_MAPPINGS` env var. Translation stays server-side so no worker restart is needed — change a mapping, save, next job dispatch uses the new value. The worker's env-var mappings are still shown in the modal for reference, and clearing the override reverts to them.
- **`SHRINKERR_DISABLE_NODE_TOKENS=true` escape hatch** for heterogeneous upgrades. When set on the server, per-node token enforcement is bypassed entirely, letting a v0.3.31 server talk to pre-v0.3.30 workers that haven't been updated yet. Prints a loud `[SECURITY] WARNING` on every startup so it can't silently stay on past the migration.
- **Diagnostic logs in server output** when a worker sends a `/api/nodes/*` call with no `X-Node-Token` but the server has one on file (`[NODES] 401 for node 'X': server has a stored token but the request sent no X-Node-Token...`). Surfaces the pre-v0.3.30-worker-vs-v0.3.30-server mismatch from the server side, so admins don't have to tail worker logs to diagnose it.

### Changed
- **Settings → Metadata APIs** now shows the green "TMDB is already connected" banner when using either the bundled key or a user-configured key. Previously only the bundled case got the banner, so admins who'd saved their own key saw no visible change in v0.3.30. Banner subtext differs per source.
- **docs/remote-workers.md** — path mappings section rewritten to cover the new UI override, the `PATH_MAPPINGS` env var fallback, and their precedence. Authentication section adds an "Upgrading server before workers" subsection documenting the escape hatch.

## [0.3.30] — 2026-04-23

### Security

Closes the last deferred item from the v0.3.28/0.3.29 hardening pass.

#### Added
- **Per-node worker tokens.** Remote workers now carry a per-node auth secret on top of the shared `X-Api-Key`. On first heartbeat the server generates a token with `secrets.token_hex(24)`, persists it in the `worker_nodes` table, and returns it in the response body. The worker writes it to `/app/data/worker_token` (mode 0600) and sends it on every subsequent call as `X-Node-Token`. The server compares with `hmac.compare_digest` and returns 401 on mismatch. Even if the shared API key leaks, an attacker who registers a fresh node can't impersonate an existing one.
- **Admin rotation** (Nodes → [node] → Settings → **Rotate token**): invalidates the stored token immediately. The worker drops its cached copy on the next 401 and re-bootstraps on its next heartbeat — no worker restart needed.
- `docs/remote-workers.md` documents the bootstrap + rotation flow; `docs/security.md` moves per-node tokens out of "deferred" into shipped defences.

#### Fixed
- `GET /api/nodes` no longer returns the `token` column to the frontend. The read surface now exposes a `has_token` boolean + `token_issued_at` ISO timestamp instead — the token itself never leaves the server.

### Changed
- **Encoder-aware rename.** Files encoded with NVENC now rename to `*.h265.*` instead of `*.x265.*` — `x265` is a specific libx265 binary, `h265` (a.k.a. HEVC) is the codec. libx265 jobs keep the `x265` tag. The scan dedup logic considers both siblings so existing `x265`-named NVENC outputs are still recognised on rescan. `rename_x264_to_x265` is kept as a back-compat alias.
- **Settings → Video → Conversion Guide** now has full libx265 preset + CRF tables and a recommended-combinations table alongside the existing NVENC ones, so CPU-only users see matching guidance. Expanded the tips section with NVENC-vs-libx265 quality equivalence, preset scaling, and CRF semantics.
- **Settings → Metadata APIs** shows a green "TMDB is already connected" banner when the bundled key is active, so users on fresh installs see that posters / native-language detection work out of the box and the TMDB input is strictly optional polish.

## [0.3.29] — 2026-04-23

### Security

Phases 2 + 3 of the security hardening pass. New [`docs/security.md`](docs/security.md) documents the threat model and a hardening checklist.

#### Fixed
- **Passwords** now hashed with bcrypt (cost 12). Legacy SHA-256 hashes from older installs are transparently upgraded on first successful login.
- **Secret comparisons** (API key, password hash, session signature) all use `hmac.compare_digest` — no more timing oracles.
- **Session signing** fails closed when `session_secret` is empty; the old code fell back to a literal `"default-secret"` constant that made every un-configured install's sessions forgeable. Startup now auto-generates `session_secret` on any DB that's missing it, not only on fresh installs.
- **Login rate-limited** to 8 attempts/minute/IP.
- **`post_conversion_script` setting** refuses to save a non-empty value when `auth_enabled=false`. Changing this setting runs arbitrary commands after every encode — now it requires the password-auth gate, not just an API key.
- **SSRF protection** on user-configured outbound URLs (Plex, Sonarr, Radarr, Discord webhook, generic webhook). Link-local (`169.254.0.0/16` — covers AWS / Azure / GCP / Alibaba metadata endpoints), IPv6 link-local, and IPv6 site-local ranges are rejected at save time.
- **Session cookies** set `Secure` flag when the request arrived over HTTPS (detected via scheme + `X-Forwarded-Proto`).
- **Settings export** now strips every secret row (`api_key`, `session_secret`, `auth_password_hash`, integration API keys, SMTP password, path-tokened URLs). Previously a leaked export file handed over every stored credential.
- **`/api/settings/browse`** refuses to list the filesystem root or any system directory (`/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/dev`). The old picker could be used as an unauthenticated filesystem enumerator before v0.3.28's auth middleware fixes; now it's locked regardless of auth state.
- **LIKE search patterns** escape `%` and `_` metacharacters with `ESCAPE '\\'`. Stops user-supplied search strings from enumerating scan_results via wildcard patterns.
- **Symlink guards** on rename / backup write destinations. Refuses to follow a pre-existing symlink at the target path — closes the window where an attacker with write access to the backup folder could redirect the rename.
- **TMDB API key** passed as `params={...}` rather than interpolated into the URL string — httpx exception messages no longer carry the raw key.
- **python-multipart bumped** to 0.0.18 (CVE-2024-53981 DoS).
- **`.env` / `.env.*`** added to `.dockerignore` so local secrets never leak into image layers.

#### Added
- **Baseline security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy: strict-origin-when-cross-origin`.
- **`backend/ssrf_guard.py`** — reusable URL validator; can also block RFC 1918 private ranges when `block_private=True` for cloud deployments.
- **`docs/security.md`** — threat model, list of in-app defences, hardening checklist for production deployments.

#### Remaining (tracked for the next release)
- **Per-node worker tokens.** Shipped in [0.3.30].

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

[0.3.51]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.51
[0.3.50]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.50
[0.3.49]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.49
[0.3.48]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.48
[0.3.47]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.47
[0.3.46]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.46
[0.3.45]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.45
[0.3.37–0.3.44]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.44
[0.3.36]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.36
[0.3.35]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.35
[0.3.34]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.34
[0.3.33]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.33
[0.3.32]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.32
[0.3.31]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.31
[0.3.30]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.30
[0.3.29]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.29
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
