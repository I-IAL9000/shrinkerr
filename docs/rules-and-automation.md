# Rules and automation

How to make Shrinkerr encode the right files, at the right time, with the
right settings, without having to touch the UI for each one.

## Contents
- [Encoding rules](#encoding-rules)
  - [Conditions](#conditions)
  - [Actions and overrides](#actions-and-overrides)
  - [Order and precedence](#order-and-precedence)
- [Watch folders](#watch-folders)
- [NZBGet / SABnzbd](#nzbget--sabnzbd)
- [Sonarr / Radarr](#sonarr--radarr)
- [Plex / Jellyfin integration](#plex--jellyfin-integration)
- [Scheduling](#scheduling)
- [Batch rename](#batch-rename)
- [Post-conversion scripts](#post-conversion-scripts)

## Encoding rules

Settings → Rules. Each rule is a set of conditions that select files,
plus an action with optional encoder overrides.

### Conditions

- **Directory** — absolute path with `is` / `starts with` / `contains` /
  `matches regex`
- **Source codec** — h264 / hevc / av1 / …
- **Resolution** — exact (1080p) or range (>=1080p)
- **File size** — MB threshold
- **Bitrate** — Mbps threshold
- **Audio languages present** — match on any/all of a language set
- **Native language** — matches TMDB's original-language field
- **Plex label / collection / genre / library** — requires Plex connected
- **Sonarr / Radarr tag** — requires the corresponding integration

Conditions in a rule combine with `match_mode` — `any` (OR) or `all`
(AND).

### Actions and overrides

- **Encode** — the default. Apply the rule's encoder override (any
  combination of `encoder`, `nvenc_preset`, `nvenc_cq`, `libx265_preset`,
  `libx265_crf`, `target_resolution`, `audio_codec`, `audio_bitrate`,
  `queue_priority`).
- **Skip** — file is tagged "skipped by rule" during scan/estimate and
  won't show up in conversion filters.
- **Audio/sub only** — skip video encoding entirely; only do the
  audio/subtitle cleanup pass (if enabled).

### Order and precedence

Rules evaluate top-down. The first matching rule wins. Drag to reorder.

Practical layout that most users arrive at:

1. **Skip rules at the top** — e.g. "directory contains `/samples/`, skip".
2. **Codec-or-quality-specific rules** — e.g. "resolution >= 4K, use p7
   and CQ 24" (4K gets more room).
3. **Source-specific rules** — e.g. "directory starts with `/media/Anime`,
   use `libx265 slow / CRF 20`" (animation compresses differently).
4. **Catch-all at the bottom** — let global settings do the work.

## Watch folders

Settings → Automation → "Watch new files". When on, new files landing in
your media directories are scanned within seconds (inotify/FSEvents) and
appear in the Scanner with the `NEW` badge. Pair this with **Auto-queue
new files** to have Shrinkerr automatically queue any scan result that
needs conversion.

Watch folders run against the same media dirs you configured in
Directories — no separate path list.

## NZBGet / SABnzbd

The post-processing scripts let NZBGet/SABnzbd notify Shrinkerr the
moment a download finishes, so a file can start encoding before
Sonarr/Radarr have finished importing it. The script POSTs to
`/api/webhooks/post-process` with the file path and metadata; Shrinkerr
matches it against your encoding rules and queues the job.

### Prerequisites (read these first)

Most NZBGet/SABnzbd integration problems are not script bugs — they're
path-visibility problems between two containers. Get these three things
right and the rest is paperwork.

**1. Both containers must mount the same host directory at the same
internal path.**

The script tells Shrinkerr "I just finished downloading
`/Downloads/completed/TV/Foo.S01E01/foo.mkv`". Shrinkerr then has to
open that file. If the NZBGet container sees `/Downloads/...` but the
Shrinkerr container has nothing mounted at `/Downloads`, every job fails
with `Outside media dirs` or `Failed to probe file`.

The simplest fix is to mount the same host path at the same internal
path in both compose files:

```yaml
# nzbget docker-compose.yml
services:
  nzbget:
    volumes:
      - /home/me/Downloads:/Downloads:rw

# shrinkerr docker-compose.yml
services:
  shrinkerr:
    volumes:
      - /home/me/Downloads:/Downloads:rw   # ← same line, same case
```

Case matters on Linux. `/Downloads` and `/downloads` are different
directories.

After editing volumes, run `docker compose down && docker compose up -d`
on the affected service. Plain `docker compose up -d` does not pick up
new volume bindings.

**2. Add NZBGet's category folders as media directories in Shrinkerr.**

Settings → Directories → "+ Add". For each NZBGet category you want to
auto-queue, add the folder NZBGet writes finished downloads to —
typically `${MainDir}/completed/<category>` (e.g.
`/Downloads/completed/TV`, `/Downloads/completed/Movies`).

For each download directory:

| Setting | Value | Why |
|---|---|---|
| Type | **Other** | Stops Shrinkerr from asking TMDB to identify each release-name folder. |
| Scan | **off** (uncheck) | The webhook is still allowed to register files inside this directory, but the periodic file-tree scanner skips it — so your Scanner page isn't littered with `Foo.S01E01.1080p.WEB-DL.x264-GRP/` temp folders that exist for thirty seconds at a time. |

The `Scan: off` flag (added in v0.3.49) is what makes "register the file
when NZBGet calls us" work without "also crawl this directory looking
for stuff to convert." Use it for any directory that is a download
landing zone, not a finished library.

**3. Path mappings: only when paths actually differ.**

Settings → Integrations → NZBGet → Path Mappings translates the path
the script reports into the path Shrinkerr should open. You only need
mappings if the two containers see the same file at different paths.

If both containers mount `/home/me/Downloads:/Downloads:rw` (the
recommended setup), leave path mappings empty.

You need a mapping when:

- NZBGet runs on the host (no container) and writes to
  `/home/me/Downloads/...`, but Shrinkerr is in a container that mounts
  it at `/Downloads`. Map `/home/me/Downloads → /Downloads`.
- Historic case-mismatch: NZBGet writes to `/Downloads` but an old
  Shrinkerr config mounted `/downloads`. Better fix is to align the
  volumes and clear the mapping.
- Your NZBGet category folder lives on a different volume from your
  finished media library and you want Shrinkerr to see it under a
  unified path.

### NZBGet script installation

1. Complete the prerequisites above (volume mounts + media dirs).
2. Settings → Integrations → NZBGet — set Tags / Categories / Priority,
   click **Save**, then **Download NZBGet Script**.
3. Place `Shrinkerr.py` in NZBGet's `ScriptDir` (Settings → Paths →
   ScriptDir; defaults to `/scripts` inside the official container).
4. NZBGet → Settings → Extension Scripts, enable `Shrinkerr` and Save.
5. Reload scripts (Settings page footer) or restart NZBGet.
6. The script's URL and API key are baked in at download time — no
   extra config inside NZBGet.
7. In Sonarr/Radarr, tag any series/movies you want auto-queued with
   the tag you configured in step 2.

### SABnzbd script installation

1. Complete the prerequisites above (volume mounts + media dirs).
2. Settings → Integrations → SABnzbd, click **Download SABnzbd Script**.
3. Place `shrinkerr.py` in SABnzbd's `scripts` folder.
4. SABnzbd → Config → Categories, set **shrinkerr.py** as the
   post-processing script for the categories you want auto-queued.
5. URL and API key are baked into the downloaded file — nothing else to
   configure on the SABnzbd side.

### Shrinkerr-side options

Settings → Integrations → NZBGet (same options exist for SABnzbd):

- **Tags** — only process downloads matching these NZBGet tags (empty =
  all)
- **Categories** — same for categories
- **Priority** — what priority the auto-queued job gets (Normal / High /
  Highest)
- **Wait for completion** — whether NZBGet's script waits for the
  encode to finish before returning (makes Sonarr/Radarr's "import on
  completion" wait for your smaller file). Adds a wait to the NZBGet
  queue — off by default.
- **Check Sonarr/Radarr tags** — if on, the script asks Sonarr / Radarr
  whether this file's series/movie has a `no-convert` tag and skips if
  so. Lets you opt individual shows out via the *arr tag system.

### Troubleshooting

**`Shrinkerr: Outside media dirs: /path/to/file.mkv`** in NZBGet/SABnzbd
logs.

The script reported a path that is not under any media directory you've
registered in Shrinkerr.

1. Read the exact path the script reported — it's in the log line.
2. Check whether the Shrinkerr container can see it:
   `docker exec shrinkerr ls "/Downloads/completed/TV"` (substitute the
   actual path).
3. If the file is **not** there, your two containers disagree about
   where that path points. Either align the volumes (preferred) or add
   a path mapping that translates NZBGet's view into Shrinkerr's view.
4. If the file **is** there, just add the parent folder as a media
   directory (Type=Other, Scan=off) and re-run the job.

**`Failed to probe file: ...`** in Shrinkerr logs.

Shrinkerr accepted the path but ffprobe couldn't open it. Usually one
of:

- The file moved between when NZBGet finished and when Shrinkerr
  processed the webhook (Sonarr/Radarr imported it). Set "Wait for
  completion = on" or accept that *arr will move it before encoding —
  Shrinkerr's queue handles the move-during-queue case for files that
  are already in your media library, but not for files in the download
  landing zone.
- Stale path from a restored database: an old `convert_jobs` row points
  to a file path that no longer exists. Clear failed jobs and rescan.
- Permissions: the Shrinkerr container's user can't read the file.
  Fix the volume mount mode or chown the file.

**Script logs show `SHRINKERR: NONE` and no API call was made.**

The script's category/tag filter rejected the job before calling
Shrinkerr. Check the script's stdout in NZBGet history — it lists what
it saw and why it stopped. Usual cause: the NZBGet category doesn't
match what you typed in Shrinkerr's Categories field, or the Tags
filter is set but the Sonarr tag isn't propagated.

## Sonarr / Radarr

Settings → Integrations → Sonarr / Radarr. Enter URL + API key.

Once connected:
- **Auto-post-conversion actions** in Settings → Integrations:
  - **Rename via *arr** — trigger the series/movie rename after
    conversion (so "h264" → "x265" in the filename reflects reality).
  - **Refresh monitoring** — notify *arr of the new file.
- **Replace / Upgrade search** buttons on any job's detail view.
- **Missing search** for content *arr shows as missing but you know is
  actually there (a common thing after a restore from backup).
- **Tag-based rules** in the rule editor — condition "Sonarr tag is
  X" / "Radarr tag is Y".

## Plex / Jellyfin integration

**Plex** — Settings → Integrations → Plex. Use the built-in OAuth flow
(click "Sign in with Plex") or paste a manual token + server URL.

What you get:
- **Rule conditions on Plex label / collection / genre / library**.
- **Library refresh after conversion** — points Plex at the exact file,
  no full-section rescan.
- **Plex trash cleanup** — also empties Plex's trash after the refresh.
- **Watch-status rules** — skip or prioritize based on watched / unwatched
  / on watchlist.
- **Pause on stream** — stop encoding if Plex is currently transcoding a
  stream (Shrinkerr on the same host would share the GPU). Configurable
  threshold, transcode-only / any-playback.

**Jellyfin** — Settings → Integrations → Jellyfin. URL, API key, user ID.
Similar feature set minus the label/collection rules (Jellyfin's
metadata model differs); library refresh and pause-on-stream both work.

## Scheduling

**Global quiet hours** (Settings → System → Quiet hours):
- Enabled on/off
- Start / end hour (24h). `22 → 8` means "quiet from 10pm to 8am".
- Parallel-jobs override during quiet hours — e.g. drop to 1 instead of
  2 at night so the box isn't audible from the bedroom.
- `nice` the ffmpeg processes during quiet hours (Linux only; reduces
  CPU priority so the system is responsive even when encoding).

**Per-node schedule** (Nodes → [node] → Settings → Schedule): click the
hours the node may process jobs. Combines with global quiet hours.

**Post-conversion action scheduler** (Settings → Automation): run a
specific action at a specific time daily, like a nightly
"re-scan Movies" or "Plex sync trash".

## Batch rename

Settings → Renaming. Define a filename pattern using tokens:
`{title}`, `{year}`, `{season:02}`, `{episode:02}`, `{episode_title}`,
`{resolution}`, `{source}`, `{codec}`, `{audio}`, `{tags}`,
`{release_group}`.

Two patterns — movies and TV. Preview in real time as you edit.

- **Auto-rename after conversion** toggles in Settings → Renaming. When
  on, every finished job also renames the file to match the pattern, so
  your library stays consistent as Shrinkerr swaps x264 → x265.
- **Bulk rename UI** — Scanner → select files → Rename. Shows a preview
  of each rename and lets you commit in one go.

## Post-conversion scripts

Settings → Advanced → Post-conversion script. A shell command or script
path that runs after every successful encode. Environment vars
available:

| Var | Example |
|---|---|
| `SHRINKERR_FILE_PATH` | /media/TV/Foo/ep.mkv |
| `SHRINKERR_ORIGINAL_PATH` | /media/TV/Foo/ep.mkv |
| `SHRINKERR_FILE_SIZE` | 1024000000 (bytes) |
| `SHRINKERR_ORIGINAL_SIZE` | 3500000000 |
| `SHRINKERR_SPACE_SAVED` | 2476000000 |
| `SHRINKERR_VMAF_SCORE` | 94.3 |
| `SHRINKERR_DURATION` | 1800 (s) |
| `SHRINKERR_ENCODER` | nvenc |
| `SHRINKERR_PRESET` | p6 |
| `SHRINKERR_CQ` | 20 |
| `SHRINKERR_JOB_ID` | 12345 |
| `SHRINKERR_JOB_TYPE` | convert |

Timeout configurable via `post_conversion_script_timeout` (default
300s). Script failure is logged but doesn't fail the job — the encode
is already done.

Common uses: push to a Discord channel with the savings, trigger a
custom archival workflow, feed the score into a quality-trend database.
