# Troubleshooting

Symptoms and fixes. If you're stuck, include the relevant docker log
excerpts when opening an issue.

## Contents
- [Start with the Doctor page](#start-with-the-doctor-page)
- [Sign-in screen after updating, or locked out](#sign-in-screen-after-updating-or-locked-out)
- [Scripts or widgets get 401 after updating](#scripts-or-widgets-get-401-after-updating)
- [Dashboard stuck on loading spinner](#dashboard-stuck-on-loading-spinner)
- [Setup wizard doesn't show on fresh install](#setup-wizard-doesnt-show-on-fresh-install)
- [Scanner shows "Loading files…" forever](#scanner-shows-loading-files-forever)
- [NVENC not advertised / unavailable](#nvenc-not-advertised--unavailable)
- [Remote worker is stuck or slow](#remote-worker-is-stuck-or-slow)
- [VMAF doesn't run for some files](#vmaf-doesnt-run-for-some-files)
- [Jobs fail with "file not found"](#jobs-fail-with-file-not-found)
- [Plex library refresh does nothing](#plex-library-refresh-does-nothing)
- [Encoded file is larger than original](#encoded-file-is-larger-than-original)
- [HDR or Dolby Vision files aren't converted](#hdr-or-dolby-vision-files-arent-converted)
- [A conversion didn't replace the original](#a-conversion-didnt-replace-the-original)
- [Files aren't queued, or don't start](#files-arent-queued-or-dont-start)
- [Queue is "running" but nothing happens](#queue-is-running-but-nothing-happens)
- [Lost / broken database](#lost--broken-database)
- [Reading the logs](#reading-the-logs)

## Start with the Doctor page

**Doctor** (in the sidebar, under System) checks what Shrinkerr depends
on and says what to fix:

- **Media folders** — found, readable, writable (a read-only mount or the
  wrong PUID/PGID shows here), answering within 15 seconds, and their free
  space.
- **Data folder** and **database** — writable, room to spare, integrity.
- **ffmpeg / ffprobe**, **VMAF**, and whether your **default encoder** is
  available on this machine.
- **Connections** — TMDB, Plex, Jellyfin, Emby, Sonarr, Radarr — and each
  **remote worker**'s last check-in.
- **Password** — whether signing in is required.

Reporting a problem? **Download diagnostics** saves a zip of the checks,
your settings and environment (passwords, API keys and tokens taken out),
the library's shape, the recent log and recent failed jobs; **Report a
problem** opens a GitHub issue to attach it to.

## Sign-in screen after updating, or locked out

Since 0.10 Shrinkerr asks everyone to sign in. An install that didn't have
password login shows **Set up sign-in** before anything else: pick
**Sign-in page** (a username and password) or **External (reverse
proxy)**. If an API key is set, setting up sign-in needs that key, so
nobody else can pick the password. Download clients, Sonarr / Radarr and
remote workers keep using the API key. See
[Security § Authentication](security.md#authentication).

Forgot the password, or locked out? The README's
[Troubleshooting](../README.md#troubleshooting) section ("I locked myself
out of the UI") resets sign-in from the command line without touching the
rest of the database.

## Scripts or widgets get 401 after updating

Since 0.10 the API key only works in the `X-Api-Key` header. A script or
dashboard widget that puts it in the URL (`?api_key=…`) gets
`401 Authentication required`: send the header instead. Prometheus can
send the key as a bearer token, and webhooks as the Basic-auth password
([Monitoring](monitoring.md)).

## Dashboard stuck on loading spinner

Most common cause on fresh installs before v0.3.10: a backend endpoint
crashing on an empty database. Pull a newer image:

```bash
docker compose pull
docker compose up -d
```

If you're on the latest and still stuck, check `docker compose logs
shrinkerr | grep -iE "error|traceback" | tail -50`. Likely culprit:
an endpoint returning 500. Note the URL from the stack trace and open
an issue with the traceback.

## Setup wizard doesn't show on fresh install

The wizard appears when **all** of these are true:
- `setup_dismissed` is false in settings
- either `has_dirs` is false or `scan_count` is 0

If you clicked "Skip setup" it won't come back. Force it with
`?setup` in the URL: `http://host:6680/?setup`. That unblocks the
wizard even after a dismissal.

## Scanner shows "Loading files…" forever

**On a fresh install with no scans yet** — fixed in v0.3.9. The scanner
was showing "Loading files" instead of "No files scanned yet. Run a scan
to get started." Pull a newer image.

**After a large scan on slow storage** — `/api/scan/tree` can take 10+
seconds on libraries with 50k+ files. Not a bug, just slow. The page
will resolve; if you want to verify the request is in flight, open the
browser devtools Network tab.

## NVENC not advertised / unavailable

Nodes shows no **NVENC (GPU)** badge on the Local node, or its Monitor
card says **NVENC off** with the reason.

**Check `nvidia-smi` inside the container:**
```bash
docker compose exec shrinkerr nvidia-smi
```

If it fails with "command not found" or cannot connect to driver:
- You're on `:latest` (CPU-only image). Switch to `:nvenc`.
- `--runtime=nvidia` / compose `deploy.resources.reservations.devices`
  is missing. See [Installation → NVIDIA GPU setup](installation.md).
- The NVIDIA Container Toolkit isn't installed on the host.

If `nvidia-smi` works but NVENC test fails (`ffmpeg exited N: ...`):
- Driver version doesn't match the image requirement. `:nvenc` and
  `:edge-nvenc` both need ≥570. Upgrade the driver.
- Too many encodes at once. GeForce cards limit how many NVENC encodes
  run at the same time, other apps on the GPU (a Plex transcode, say)
  included. Keep **Parallel Jobs** (Settings → Video, or the node's
  **Node settings** on the Nodes page) within it.

## Remote worker is stuck or slow

### 1 fps on a Mac worker

Two common causes, both fixed in v0.3.19:
- Running the amd64 image on Apple Silicon via Rosetta emulation (5–10×
  slowdown by itself). Remove `--platform linux/amd64` and pull `:latest`
  (multi-arch). `docker exec shrinkerr-worker uname -m` should return
  `aarch64`.
- Aggressive preset translation for NVENC → libx265. With an old
  worker image, `nvenc p6` got mapped to `libx265 slower/CRF 16`
  (unusable on CPU). Update to v0.3.12+ and the translation is capped
  at `slow` + CRF 1:1.

If still slow on a network-mounted worker: the library reads/writes go
back over the mount for every encoded chunk. AFP/SMB specifically are
very chatty. On AFP over TCP we've measured a ~5× throughput penalty vs
local disk. Options:
- Mount via a faster protocol (SMB3 + multichannel, NFS4)
- Move the worker to a host with local disk access
- Accept the speed — a worker at 30 fps is still useful on a long queue
- **Run the Mac native** (no Docker) so ffmpeg can reach VideoToolbox.
  See [Running Shrinkerr natively on macOS](native-install-mac.md).
  Real-world numbers: ~30–100 fps with `hevc_videotoolbox` vs. ~1 fps
  for `libx265` inside Docker on the same machine.

### Worker registered but never picks up jobs

Nodes page shows the worker **Online** but never **Working**.

1. **Affinity** — Nodes → Node settings → Job affinity. If set to `nvenc_only`
   and all queued jobs are libx265, or vice versa, the worker
   legitimately has nothing to pick up.
2. **Encoder translation** off — same issue but wider.
3. **Paused** — explicit pause flag on the node.
4. **Schedule** — node's per-hour schedule might be restricting
   right now.

If all those look right, check worker logs:
```bash
docker logs shrinkerr-worker 2>&1 | grep -iE "request-job" | tail -5
```
Requests should be returning `200 OK` (the server just has no matching
job) rather than errors.

### Worker not updated to 0.10

Run remote workers on the server's version. Workers older than 0.10 don't
know the per-job settings 0.10 added (a rule's originals policy, Fix for
direct play, before / after frames, Dolby Vision). **Nodes → Add a remote
worker** gives a command with the image matching this server's version.

### Worker says "file not found"

Path mapping mismatch. See [Remote workers § Path mappings](remote-workers.md).

## VMAF doesn't run for some files

Before v0.3.10, apostrophes (and other special characters) in filenames
could break VMAF's `-filter_complex` arg and the error was silent. Fixed.
If you're on ≥ v0.3.10 and VMAF still isn't running for some files:

1. **VMAF disabled** — Settings → Video → Smart Encoding → VMAF
   quality analysis. Check it's on.
2. **Worker-mode job before v0.3.19** — remote workers hardcoded VMAF
   off. Fixed.
3. **libvmaf not available** — official images all have it. Custom builds
   or `:latest` without `ffmpeg` rebuild might not. Worker log will show
   `[CONVERT] VMAF skipped — libvmaf not available`.
4. **ffmpeg failed during VMAF** — logs now include
   `[CONVERT] VMAF failed (ffmpeg rc=N: ...)` with the tail of stderr.
   Often a filter-chain mismatch on odd source formats.

If the log shows `[CONVERT] VMAF decision: enabled=False`, the setting
looked "false" at encode time. Check the current value:
```bash
docker compose exec shrinkerr python3 -c "
import sqlite3
c = sqlite3.connect('/app/data/shrinkerr.db')
for r in c.execute(\"SELECT key, value FROM settings WHERE key LIKE 'vmaf%'\"):
    print(r)
"
```

## Jobs fail with "file not found"

On the primary server: the file moved or was deleted between scan and
encode. Re-scan the directory and re-queue.

On a remote worker: path mapping missing (see above) OR the worker's
mount of the library went away. `docker exec shrinkerr-worker ls /media`
should list the library.

On the primary: `.converting.mkv` leftover from a previous crash
blocking a rename. Shrinkerr auto-cleans these on startup but occasionally
misses one. Delete manually:
```bash
find /srv/media -name "*.converting.mkv" -mtime +1 -delete
```
(The `-mtime +1` avoids nuking an active encode.)

## Plex library refresh does nothing

1. Plex integration is connected (Settings → Integrations → Plex shows
   a server name).
2. Path mapping matches how Plex sees the files. Settings → Integrations
   → Plex → Path Mapping (e.g. `/media=/home/plex/media`). The
   *library refresh* call is path-specific; if Plex doesn't see the
   exact path, it does nothing.
3. Plex library has "Scan library automatically" enabled (Plex side).
   Shrinkerr's call tells Plex *to* scan; if Plex is configured to
   ignore manual scan requests, nothing happens.

Test manually from Shrinkerr: Settings → Integrations → Plex → "Test"
(under "Advanced: enter URL & token manually").
Failure here surfaces the actual HTTP error Plex returned.

## Encoded file is larger than original

Shrinkerr detects this and keeps the original. The job's details show
"(N% larger — discarded)", file auto-added to ignore list so
subsequent scans skip it.

Causes:
- Original was already encoded at very low bitrate (pre-compressed
  re-upload).
- CRF/CQ set too low (high quality) for the source. Try bumping 2–3
  steps.
- Source has audio tracks Shrinkerr is copying but would compress well.
  Enable audio conversion to smaller codec (EAC3 640k → 256k, say).

Unignore a file from the ignored list (Scanner → **Ignored** filter) if
you want to retry.

## HDR or Dolby Vision files aren't converted

Shrinkerr won't replace an HDR or Dolby Vision file with one that lost it.
When it can't keep it, the job keeps the video as it is, still does its
other work on the original (removing tracks, Fix for direct play), and
completes with **Video kept**; the file is ignored so it isn't queued again.
The job's details say why:

- **"The video was kept as it is: Dolby Vision …"** — it names the reason:
  profile 5, the job resizes it (a rule's or the queue's target
  resolution), it encodes to AV1, it uses Quick Sync / VAAPI, the file is a
  disc, or dovi_tool / mkvmerge are missing (a native install). To convert
  it, change what the reason names, then un-ignore the file (Scanner →
  Ignored).
- **"The video was kept as it is: HDR video needs a 10-bit encoder"** —
  Quick Sync and VAAPI encode 8-bit here. Use NVENC, libx265 or
  VideoToolbox.
- **"… lost its HDR in the encode. Original kept."** — the output came out
  SDR or 8-bit. With NVENC, check Settings → Video → Bit Depth isn't
  8-bit.
- **"Couldn't keep Dolby Vision: … Original kept."** — dovi_tool couldn't
  carry the Dolby Vision over; the reason follows.

See [Encoding guide § HDR and Dolby Vision](encoding-guide.md#hdr-and-dolby-vision).

## A conversion didn't replace the original

- **Review before replace** is on (Settings → Automation → Originals &
  Backups): converted files wait in **Queue → Review** until you
  **Approve** them (they're put in place straight away) or
  **Reject** them (the new file is deleted, the original isn't queued
  again).
- **The new file failed a check**: larger than the original, below your
  minimum VMAF, shorter than the original, or HDR lost. The job's details
  say which. See
  [What happens to your files](what-happens-to-your-files.md#the-checks-before-anything-is-replaced).

## Files aren't queued, or don't start

Add to Queue says what it left out:

- **Still hardlinked (seeding)** — with **Skip files that are still
  hardlinked (e.g. seeding)** on (Settings → Automation → Conversion
  Filters; on for new installs), a file a torrent client still shares is
  left out: converting it frees nothing. The Scanner's **Hardlinked
  (seeding)** filter shows them.
- **In a Maintainerr collection (leaving soon)** — with **Don't queue
  titles Maintainerr is about to remove** on (Settings → Integrations →
  Maintainerr).
- **Ignored**, or skipped by a rule.

**Include ignored files and override rules** in Add to Queue queues them
anyway.

A pending job that says **waits until …** was just imported: **Wait after
an import** (Settings → Automation → Conversion Filters) holds it so Bazarr
can add subtitles first.

## Queue is "running" but nothing happens

The queue's running flag is just UI state — it means "dispatcher is
accepting new jobs". If no node can take a job (all paused, all
affinity-filtered out, all at max concurrent), the queue stays running
but idle.

Previously (pre-v0.3.16) the UI would show a phantom "Starting..."
card in this case. Fixed: capacity is now summed across available
nodes.

## Lost / broken database

Back up BEFORE trying anything. `cp ./data/shrinkerr.db
./data/shrinkerr.db.bak`.

**SQLite integrity check:**
```bash
docker compose exec shrinkerr python3 -c "import sqlite3; print(sqlite3.connect('/app/data/shrinkerr.db').execute('PRAGMA integrity_check').fetchone()[0])"
```
Returns `ok` when the DB is fine.

**Corrupt DB:**
- Export settings first if the UI still works (**Export** at the top of
  the Settings page).
- Stop the container. Back up `shrinkerr.db`.
- Move the DB out of the way: `mv shrinkerr.db shrinkerr.broken.db`.
- Restart — a fresh DB is created.
- Restore settings + re-scan.

Pending jobs are lost but your library is untouched. Shrinkerr never
destructively modifies your media without backups/verifications.

## Reading the logs

Docker compose logs are the main debugging surface:
```bash
docker compose logs shrinkerr --tail 200
docker compose logs shrinkerr --follow           # tail -f
docker compose logs shrinkerr --since 1h
```

Log prefixes:
- `[QUEUE]` — adding jobs to the queue, and why a file was skipped
- `[WORKER]` — job execution by the local or remote worker loop, worker
  slot decisions
- `[CONVERT]` — ffmpeg invocation, VMAF, result summarization
- `[SCANNER]` — scanner progress
- `[NODES]` — node registration, heartbeat, capability detection
- `[PLEX]` / `[JELLYFIN]` / `[EMBY]` — media server integration calls
- `[RULES]` — encoding rules being listed and created

Shrinkerr's compose files set no logging options, so Docker's default
`json-file` driver applies: it keeps everything unless your Docker daemon
is set to rotate logs. Add a driver config to cap the logs or keep a set
amount of history:

```yaml
services:
  shrinkerr:
    # ...
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
```
