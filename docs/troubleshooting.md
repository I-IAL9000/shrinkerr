# Troubleshooting

Symptoms and fixes. If you're stuck, include the relevant docker log
excerpts when opening an issue.

## Contents
- [Dashboard stuck on loading spinner](#dashboard-stuck-on-loading-spinner)
- [Setup wizard doesn't show on fresh install](#setup-wizard-doesnt-show-on-fresh-install)
- [Scanner shows "Loading files…" forever](#scanner-shows-loading-files-forever)
- [NVENC not advertised / unavailable](#nvenc-not-advertised--unavailable)
- [Remote worker is stuck or slow](#remote-worker-is-stuck-or-slow)
- [VMAF doesn't run for some files](#vmaf-doesnt-run-for-some-files)
- [Jobs fail with "file not found"](#jobs-fail-with-file-not-found)
- [Plex library refresh does nothing](#plex-library-refresh-does-nothing)
- [Encoded file is larger than original](#encoded-file-is-larger-than-original)
- [Queue is "running" but nothing happens](#queue-is-running-but-nothing-happens)
- [Lost / broken database](#lost--broken-database)
- [Reading the logs](#reading-the-logs)

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

Nodes → Local shows `capabilities: [libx265]` only, `gpu: None`, or a
red `nvenc_unavailable_reason`.

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
- Driver version doesn't match the image requirement. `:nvenc` needs
  ≥525.60.13; `:edge-nvenc` needs ≥570. Upgrade the driver or swap
  image.
- Another process is saturating the NVENC engine. Consumer NVIDIA cards
  have a concurrent-NVENC-session limit that unlocks on newer driver
  versions via the open-kernel-module driver.

## Remote worker is stuck or slow

### 1 fps on a Mac worker

See [Remote workers § "Mac worker is slow"](remote-workers.md). Two
common causes, both fixed in v0.3.19:
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

### Worker registered but never picks up jobs

Nodes page shows the worker online but `current_job_id` stays null.

1. **Affinity** — Node Settings → Job affinity. If set to `nvenc_only`
   and all queued jobs are libx265, or vice versa, the worker
   legitimately has nothing to pick up.
2. **Translate encoder** off — same issue but wider.
3. **Paused** — explicit pause flag on the node.
4. **Schedule** — node's per-hour schedule might be restricting
   right now.
5. **Max jobs** — at 0 (yes, some people do this by accident).

If all those look right, check worker logs:
```bash
docker logs shrinkerr-worker 2>&1 | grep -iE "request-job" | tail -5
```
Requests should be returning `"job":null` (no matching job) rather than
errors.

### Worker says "file not found"

Path mapping mismatch. See [Remote workers § Path mappings](remote-workers.md).

## VMAF doesn't run for some files

Before v0.3.10, apostrophes (and other special characters) in filenames
could break VMAF's `-filter_complex` arg and the error was silent. Fixed.
If you're on ≥ v0.3.10 and VMAF still isn't running for some files:

1. **VMAF disabled** — Settings → Video → Smart Encoding → VMAF
   analysis. Check it's on.
2. **Worker-mode job before v0.3.19** — remote workers hardcoded VMAF
   off. Fixed.
3. **libvmaf not available** — official images all have it. Custom builds
   or `:latest` without `ffmpeg` rebuild might not. Worker log will show
   `[CONVERT] VMAF skipped — libvmaf not available`.
4. **ffmpeg failed during VMAF** — logs now include
   `[CONVERT] VMAF failed (ffmpeg rc=N: ...)` with the tail of stderr.
   Often a filter-chain mismatch on odd source formats.

Without any of those log lines, the VMAF block wasn't even entered —
means the setting looked "false" at encode time. Check the current
value:
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
   → Plex → Path mapping (e.g. `/media → /home/plex/media`). The
   *library refresh* call is path-specific; if Plex doesn't see the
   exact path, it does nothing.
3. Plex library has "Scan library automatically" enabled (Plex side).
   Shrinkerr's call tells Plex *to* scan; if Plex is configured to
   ignore manual scan requests, nothing happens.

Test manually from Shrinkerr: Settings → Integrations → Plex → "Test
refresh". Failure here surfaces the actual HTTP error Plex returned.

## Encoded file is larger than original

Shrinkerr detects this and keeps the original. Job marked "skipped
(larger after conversion)", file auto-added to ignore list so
subsequent scans skip it.

Causes:
- Original was already encoded at very low bitrate (pre-compressed
  re-upload).
- CRF/CQ set too low (high quality) for the source. Try bumping 2–3
  steps.
- Source has audio tracks Shrinkerr is copying but would compress well.
  Enable audio conversion to smaller codec (EAC3 640k → 256k, say).

Unignore a file from the ignored list (Settings → Ignore list) if you
want to retry.

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
docker compose exec shrinkerr sqlite3 /app/data/shrinkerr.db 'PRAGMA integrity_check'
```
Returns `ok` when the DB is fine.

**Corrupt DB:**
- Export settings first if the UI still works (Settings → Backups).
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
- `[QUEUE]` — queue lifecycle, worker slot decisions
- `[WORKER]` — job execution by the local or remote worker loop
- `[CONVERT]` — ffmpeg invocation, VMAF, result summarization
- `[SCAN]` — scanner progress
- `[NODES]` — node registration, heartbeat, capability detection
- `[PLEX]` / `[JELLYFIN]` — media server integration calls
- `[RULES]` — rule resolver decisions (verbose; only useful when
  debugging unexpected rule behavior)

Default docker-compose log driver is `json-file` with a 10MB × 3 files
ring. Long runs can push older entries out; add a driver config if you
want more history:

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
