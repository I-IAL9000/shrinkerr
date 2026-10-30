# Rules and automation

How to make Shrinkerr encode the right files, at the right time, with the
right settings, without having to touch the UI for each one.

## Contents
- [Encoding rules](#encoding-rules)
  - [Conditions](#conditions)
  - [Actions and overrides](#actions-and-overrides)
  - [Order and precedence](#order-and-precedence)
  - [Sharing rules and the rule library](#sharing-rules-and-the-rule-library)
- [Watch folders](#watch-folders)
- [NZBGet / SABnzbd](#nzbget--sabnzbd)
- [Webhook endpoints](#webhook-endpoints)
- [Sonarr / Radarr](#sonarr--radarr)
  - [Queue each import (Connect → Webhook)](#queue-each-import-connect--webhook)
  - [Replacing corrupt files](#replacing-corrupt-files)
- [Bazarr and Maintainerr](#bazarr-and-maintainerr)
- [Plex / Jellyfin / Emby integration](#plex--jellyfin--emby-integration)
- [Scheduling](#scheduling)
  - [Policies](#policies)
  - [Freeing space by a date](#freeing-space-by-a-date)
- [Batch rename](#batch-rename)
- [Post-conversion scripts](#post-conversion-scripts)

## Encoding rules

Settings → Automation → Encoding Rules. Each rule is a set of conditions that
select files, plus an action with optional encoder overrides.

### Conditions

- **Directory** — absolute path; matches files under it (prefix match)
- **Source** — Remux / WEB-DL / WEBRip / Blu-ray / HDTV / DVD, from the file name
- **Video codec** — h264 / hevc / av1 / …
- **Audio codec** — contains / doesn't contain
- **Resolution** — exact: 4K / 1080p / 720p / SD
- **File size** — GB threshold (greater than / less than)
- **Date added** — newer / older than (e.g. `7d`)
- **Media type** — TV / movie
- **Title** — file name contains / doesn't contain
- **Release group**
- **Content type** — anime / animation / film grain / remux / other, the same
  file- and folder-name detection as Settings → Video → Content type
  detection (works whether or not that setting is on). With that setting
  on, **Rule…** next to each type there starts a new rule with this
  condition filled in.
- **Plex label / collection / genre / library** — requires Plex connected
- **Jellyfin / Emby tag**, **Emby watched** — requires the corresponding integration
- **Sonarr / Radarr tag** — the tag on the file's series / movie; requires the corresponding integration
- **Watched** (Plex) / **Jellyfin watched**
- **Saved view** — the files a Scanner view lists (Scanner → Views)
- **Download category** — the NZBGet / SABnzbd category, for downloads queued by their scripts

Conditions in a rule combine with `match_mode` — `any` (OR) or `all`
(AND).

### Actions and overrides

- **Encode** — the default. Apply the rule's encoder override (any
  combination of `encoder`, `nvenc_preset`, `nvenc_cq`, `libx265_preset`,
  `libx265_crf`, `target_resolution`, output codec, `audio_codec`,
  `audio_bitrate`, `queue_priority`). **Output codec** HEVC (the default)
  or **AV1** — see [AV1 output](encoding-guide.md#av1-output). With QSV,
  VAAPI or VideoToolbox the rule's **CQ** (NVENC's scale) is translated to
  the encoder's own quality setting.

**Languages, originals and more** (in the rule form) — per-library
policies:

- **Audio languages to keep** and **Subtitles** (keep all / remove all /
  keep these languages) replace the global languages for the rule's files
  when they're queued. Tracks you kept or removed by hand stay as you set
  them; the Scanner keeps showing the global choice.
- **Originals** — keep for N days, move to trash or delete, instead of the
  global setting (Encode rules).
- **Minimum VMAF** — this rule's rejection threshold; 0 never rejects.
- **VMAF target** — Settings' target (the default), **Off** (the rule's own
  quality, no search) or a score of 80–99 for this rule's files; see
  [VMAF target quality](encoding-guide.md#vmaf-target-quality).
- **Skip if savings below (%)** — leave the video alone when the estimated
  saving is smaller; the track cleanup still runs.

These travel with each job, to remote workers too.
- **Skip** — Add to Queue (and auto-queue, the download-client scripts and
  the webhooks) leave the file alone; the estimate counts it as "skipped by
  rules". A skip rule on a directory, a Plex label / collection / genre /
  library or a Jellyfin / Emby tag also keeps its files out of the
  conversion filters.
- **Audio/sub only** — skip video encoding entirely; only do the
  audio/subtitle cleanup pass (if enabled).

### Order and precedence

Rules evaluate top-down. The first matching rule wins. Drag to reorder.

**Test a file** (under the rule list): enter a file's path to see which
rule applies to it, and for every rule which of its conditions matched.

Practical layout that most users arrive at:

1. **Skip rules at the top** — e.g. "directory `/media/Samples`, skip".
2. **Codec-or-quality-specific rules** — e.g. "resolution is 4K, use p7
   and CQ 24" (4K gets more room).
3. **Source-specific rules** — e.g. "directory starts with `/media/Anime`,
   use `libx265 slow / CRF 20`" (animation compresses differently).
4. **Catch-all at the bottom** — let global settings do the work.

### Sharing rules and the rule library

**Export** (above the rule list) saves your rules as a YAML file,
`shrinkerr-rules.yaml`, with the saved views they use. **Import…** reads
such a file:

- Its rules are added after yours, in the file's order.
- A rule with the same name as one of yours replaces it but keeps its
  place in the order. Importing the same file again changes nothing.
- A saved view the file brings is created, unless you already have a view
  of that name. Then your view is used.
- A rule that can't be imported (an unknown condition, a bad value) is
  listed with the reason. The rest are imported.

The file is plain text, so you can write or edit it by hand:

```yaml
shrinkerr_rules: 1
rules:
  - name: Anime in AV1
    action: encode            # encode | ignore (tracks only) | skip
    enabled: true
    match: any                # any | all
    conditions:
      - {type: content_type, operator: is, value: anime}
      - {type: saved_view, operator: is, value: Big x264}
    settings:                 # anything the rule form sets; leave out what it doesn't
      output_codec: av1
      vmaf_min_score: 93
      originals_policy: keep  # keep | trash | delete
      originals_days: 30
saved_views:
  - {name: Big x264, filter: x264}
```

`settings` takes the rule form's fields: `encoder`, `nvenc_preset`,
`nvenc_cq`, `libx265_crf`, `libx265_preset`, `output_codec`,
`target_resolution`, `audio_codec`, `audio_bitrate`, `queue_priority`,
`audio_keep_languages`, `sub_policy`, `sub_keep_languages`,
`originals_policy`, `originals_days`, `vmaf_min_score`,
`vmaf_target` (0 is off) and `min_savings_pct`.

**Rule library** has ready-made rules to start from:

- Remuxes: keep the originals
- Animation and anime in AV1
- 4K: higher quality
- WEB-DL: only when it's worth it
- Small files: tracks only
- Skip samples

They work with any encoder. Each one is added after your rules, and you
can edit it like any other rule. Adding it again resets it.

## Watch folders

The file watcher is always on: it polls your media directories every 5
minutes, and new files it finds appear in the Scanner with the `NEW`
badge. Pair this with **Auto-queue new files** (Settings → Automation →
Auto-Queue) to have Shrinkerr automatically queue the new files that
need converting or a track cleanup, as Add to Queue would queue them.
**Auto-queue priority** (Normal by default) is the priority they get; a
rule's Queue priority wins when it's higher. **Only files in:** limits it
to the new files a saved view lists (default: every new file); that view
can't be deleted while the auto-queue uses it.

To look at each conversion before it replaces the original, turn on
**Review conversions before they replace the original** (Settings →
Automation → Originals & Backups; off by default). See
[What happens to your files](what-happens-to-your-files.md#the-checks-before-anything-is-replaced).

Watch folders run against the same media dirs you configured in
Settings → Media → Media Directories (those with **Scan** on) — no
separate path list.

## NZBGet / SABnzbd

The post-processing scripts let NZBGet/SABnzbd notify Shrinkerr the
moment a download finishes, so a file can start encoding before
Sonarr/Radarr have finished importing it. The script POSTs to
`/api/jobs/add-by-path` with the file paths and the download's category;
Shrinkerr stores each file in the Scanner and queues it as Add to Queue
would, your encoding rules included.

### Prerequisites (read these first)

Most NZBGet/SABnzbd integration problems are not script bugs — they're
path-visibility problems between two containers. Get these three things
right and the rest is paperwork.

**1. Both containers must mount the same host directory at the same
internal path.**

The script tells Shrinkerr "I just finished downloading
`/Downloads/completed/TV/Foo.S01E01/foo.mkv`". Shrinkerr then has to
open that file. If the NZBGet container sees `/Downloads/...` but the
Shrinkerr container has nothing mounted at `/Downloads`, every file is
refused with `Outside media dirs` or `File not found`.

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

Settings → Media → Media Directories → "+ Add". For each NZBGet category you want to
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

Settings → Integrations → NZBGet / SABnzbd → Path mappings translates the path
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
2. Settings → Integrations → NZBGet / SABnzbd — set Tags / Categories / Priority,
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
2. Settings → Integrations → NZBGet / SABnzbd, click **Download SABnzbd Script**.
3. Place `shrinkerr.py` in SABnzbd's `scripts` folder.
4. SABnzbd → Config → Categories, set **shrinkerr.py** as the
   post-processing script for the categories you want auto-queued.
5. URL and API key are baked into the downloaded file — nothing else to
   configure on the SABnzbd side.

### Shrinkerr-side options

Settings → Integrations → NZBGet / SABnzbd (same options apply to SABnzbd):

- **Tags to match in Sonarr/Radarr** — only process downloads whose
  series / movie has at least one of these tags in Sonarr / Radarr (a
  download they don't know is skipped). Without Sonarr / Radarr set up in
  Shrinkerr, every download is processed.
- **Categories** — only process downloads in these NZBGet / SABnzbd
  categories (empty = all)
- **Priority** — what priority the auto-queued job gets (Normal / High /
  Highest)
- **Wait for conversion to complete** — whether NZBGet's script waits for the
  encode to finish before returning (makes Sonarr/Radarr's "import on
  completion" wait for your smaller file). Adds a wait to the NZBGet
  queue — on by default.
- **Check Sonarr tags** / **Check Radarr tags** — whether the script looks
  TV downloads up in Sonarr and movies in Radarr for those tags. On by
  default. It's opt-in: tag the shows and movies you want converted. With a
  check off (or that app not set up), every download of that kind is
  converted.

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
   directory (Type=Other, Scan=off) and run the script for that download
   again.

**`File not found: ...`** or **`Probe failed: ...`** in NZBGet/SABnzbd
logs, or a job that failed with **`Source file no longer exists`** or
**`Failed to probe file`**.

Shrinkerr accepted the path but couldn't find or read the file. Usually
one of:

- The file moved between when NZBGet finished and when Shrinkerr
  ran the job (Sonarr/Radarr imported it). Set "Wait for conversion to
  complete = on" or accept that *arr will move it before encoding —
  Shrinkerr's queue handles the move-during-queue case for files that
  are already in your media library, but not for files in the download
  landing zone.
- Stale path from a restored database: an old `jobs` row points
  to a file path that no longer exists. Clear failed jobs and rescan.
- Permissions: the Shrinkerr container's user can't read the file.
  Fix the volume mount mode or chown the file.

**Script logs show `SHRINKERR: NONE` and no API call was made.**

The script's category/tag filter rejected the job before calling
Shrinkerr. Check the script's stdout in NZBGet history — it lists what
it saw and why it stopped. Usual cause: the NZBGet category doesn't
match what you typed in Shrinkerr's Categories field, or the Tags
filter is set but the Sonarr tag isn't propagated.

## Webhook endpoints

Settings → Automation → Webhook Endpoints lists what other tools can call:

| Endpoint | What it does |
|---|---|
| `POST /api/webhooks/scan` | Scans the library, or the `paths` you send (they must be under a media directory) |
| `POST /api/webhooks/queue` | Queues files by path: `{"paths": [...]}`, optionally `priority`, `insert_next`, `force_reencode` |
| `POST /api/webhooks/pause`, `/resume` | Pauses or resumes the queue |
| `GET /api/webhooks/status` | Running, pending and completed jobs, space saved, and whether it's paused |

They always need your API key (Settings → System → Authentication), in an
`X-Api-Key` header or as the Basic-auth password (any username) — not in
the URL. A file sent to `/queue` is stored in the Scanner and queued as
Add to Queue would queue it. Like the download-client scripts, it starts
the queue when it's stopped but never undoes a pause you set; `/resume`
does that.

## Sonarr / Radarr

Settings → Integrations → Sonarr / Radarr. Enter URL + API key.

Once connected:
- **Rescan after conversion** — Shrinkerr tells Sonarr / Radarr to rescan
  the series / movie folder so they pick up the new file.
- **Request replacement / Search for upgrade** buttons on a file's details
  in the Scanner, and for a selection in the Scanner's \*arr actions menu.
- **Search missing episodes** (\*arr actions, Sonarr only) — asks Sonarr
  to search for the missing monitored episodes of each series in the
  selection.
- **Tag-based rules** in the rule editor — condition "Sonarr/Radarr Tag
  is X".
- **Below cutoff** and **Unmonitored** Scanner filters (the Sonarr/Radarr
  group) — files Sonarr / Radarr will replace anyway, so converting them
  is wasted work, and files they don't monitor. Read after every full
  scan, or now with **Read cutoff & monitoring** (Settings → Integrations
  → Sonarr / Radarr).

### Queue each import (Connect → Webhook)

For setups without the NZBGet / SABnzbd scripts — torrent clients, for
example — Sonarr and Radarr can tell Shrinkerr about every file they import:

1. In Sonarr or Radarr: **Settings → Connect → + → Webhook**, with the
   triggers **On Import** (and **On Upgrade** if you want upgrades too).
2. URL `http://<shrinkerr>:6680/api/webhooks/arr`, method **POST**.
3. **Password**: your Shrinkerr API key (Settings → System → Authentication);
   the username can be anything. Or add an `X-Api-Key` header with the key.

Each imported file is stored in the Scanner and queued as Add to Queue
would queue it — encoding rules, "skip hardlinked files" and the
media-folder check included. Sonarr / Radarr paths go through the path
mapping you set for each. Torrent imports are usually hardlinks while they
seed; with **Skip files that are still hardlinked** on (Settings →
Automation) they're left out until seeding stops, since converting them
would save nothing.

### Replacing corrupt files

The health check (Scanner → select → Health check) marks files whose
video can't be decoded cleanly as **Corrupt**. With the Corrupt filter on,
**Replace via Sonarr/Radarr…** lists every corrupt file with the episodes
or movie it belongs to and the release that would be blocklisted — or why
it can't be replaced (not in Sonarr / Radarr, or Radarr lists a different
file for that movie). Nothing changes until you tick files and press
**Replace**: Sonarr / Radarr then blocklist the release, delete the file
and search for a new download. A replaced file leaves the Scanner, so its
replacement starts with no health result.

## Bazarr and Maintainerr

**Bazarr** adds subtitles a few minutes after Sonarr / Radarr import a
file. **Settings → Automation → Wait after an import** holds newly
imported files — auto-queued, from the download-client scripts or the
Sonarr / Radarr webhook — for that many minutes before converting, so
those subtitles arrive first and are merged in (with **Merge external
subtitles** on — see [Audio and subtitles](audio-and-subtitles.md)). The
Queue shows "waits until …"; remote workers wait too. Files you add
yourself aren't held.

Give Shrinkerr **Bazarr's URL and API key** (Settings → Integrations →
Bazarr) and, after each file it rewrites — sidecars merged in, image
subtitles read into SRT, the file renamed — it asks Bazarr to re-sync that
series or movie from Sonarr / Radarr and scan its subtitles, a minute
later (once Sonarr / Radarr have rescanned). Otherwise Bazarr still lists
the sidecars it downloaded and, with them merged and deleted, downloads
them again. Needs Sonarr / Radarr set up in Shrinkerr too.

**Maintainerr** collects titles it is about to remove ("Leaving soon").
Enter its URL in **Settings → Integrations → Maintainerr** and Shrinkerr
reads its collections after every full scan (or **Read collections**): with
**Don't queue titles Maintainerr is about to remove** on, they're left out
of the queue and estimates — no point spending hours converting them — and
the Scanner's **Leaving soon** filter shows them. "Include ignored" in Add
to Queue still queues them.

## Plex / Jellyfin / Emby integration

**Plex** — Settings → Integrations → Plex. Use the built-in OAuth flow
(click "Connect to Plex") or paste a manual token + server URL.

What you get:
- **Rule conditions on Plex label / collection / genre / library**.
- **Library refresh after conversion** — points Plex at the file's folder,
  no full-section rescan.
- **Plex trash cleanup** — also empties Plex's trash after the refresh.
- **Watch-status rules** — skip or prioritize based on watched / unwatched.
- **Prioritize unwatched content** (off by default) — unwatched files are
  queued with at least High priority, so they convert first. Watch status
  is read after every full scan, or with **Sync from Plex** above the
  Encoding Rules.
- **Pause on stream** (Schedule page) — stop encoding if Plex is currently transcoding a
  stream (Shrinkerr on the same host would share the GPU). Configurable
  threshold, transcode-only / any-playback.

**Jellyfin** — Settings → Integrations → Jellyfin. URL, API key, user ID
(optional).
Similar feature set minus the label/collection rules (Jellyfin's
metadata model differs); library refresh and pause-on-stream both work.

**Emby** — Settings → Integrations → Emby. URL, API key, user ID
(optional, for watched status).
- **Refresh library after conversion** — on by default.
- **Emby tag** and **Emby watched** rule conditions — read from Emby after
  every full scan.
- **Pause on stream** (Schedule page) — as for Plex.

## Scheduling

**Global quiet hours** (Schedule page → Quiet Hours):
- Enabled on/off
- Start / end hour (24h). `22 → 8` means "quiet from 10pm to 8am".
- Parallel-jobs override during quiet hours — e.g. drop to 1 instead of
  2 at night so the box isn't audible from the bedroom.
- `nice` the ffmpeg processes during quiet hours (reduces CPU priority,
  and I/O priority on Linux, so the system is responsive even when
  encoding).

**Per-node schedule** (Nodes → [node] → Node settings → Schedule): click the
hours the node may process jobs. Combines with global quiet hours.

**Run hours / scheduled start** (Schedule page): pick the hours the queue
may run (it pauses automatically outside them), or schedule a one-off
queue start.

**Home Assistant** can start and pause the queue too, with a switch over
MQTT — see [Home Assistant (MQTT)](monitoring.md#home-assistant-mqtt).

### Policies

Settings → Automation → Policies runs a **saved view** (Scanner → Views →
Save; saved views are also listed in the sidebar under Scanner) on a
**schedule**, within a **budget**:

- **Action** — *Add to Queue* (as the Scanner's button would: rules, ignore
  lists and hardlink / Maintainerr skips apply) or a *quick health check*.
- **When** — every day, or one day a week, at a time (the server's local
  time).
- **Budget** — at most N files and / or N GB of files per run. Add to Queue
  takes the files with the biggest estimated savings first; a health check
  takes the ones never checked, then the longest ago. Files already waiting
  in the queue are left out, so each run picks up where the last stopped.
- **Start the queue** after adding (never over a pause you set).

*Run now* runs it at once; each policy shows its next and last run. A
saved view can't be deleted while a policy uses it.

### Freeing space by a date

Queue → Pending → **Free space…** works out which conversions free the
space you ask for fastest: the files that need converting (not ignored,
queued, hardlinked or leaving soon), by estimated savings per hour of
encoding, until the target is reached — optionally from one saved view.
It says how long that takes at your parallel jobs (from your past encode
speeds) and whether it finishes before the date you give; *Queue N files*
adds them ahead of what's waiting and starts the queue.

## Batch rename

Settings → Media → Renaming. Define a filename pattern using tokens:
`{Title}`, `{Year}`, `{TmdbId}`, `{ImdbId}`, `{Edition}`,
`{SeriesTitle}`, `{season:00}`, `{episode:00}`, `{EpisodeTitle}`,
`{TvdbId}`, `{Quality}`, `{Resolution}`, `{Source}`, `{VideoCodec}`,
`{VideoDynamicRange}`, `{AudioCodec}`, `{AudioChannels}`,
`{ReleaseGroup}`, `{Proper}` (`:00` zero-pads a number).

File patterns for movies and episodes, plus folder patterns used with
**Also rename folders**. Preview in real time as you edit.

- **Auto-rename after conversion** toggles in Settings → Media → Renaming. When
  on, every finished job also renames the file to match the pattern, so
  your library stays consistent as Shrinkerr swaps x264 → x265.
- **Bulk rename UI** — Scanner → select files → Rename. Shows a preview
  of each rename and lets you commit in one go.

## Post-conversion scripts

Settings → Automation → Post-Conversion Script. The absolute path of an
executable script (run directly, not through a shell) that runs after
every finished conversion or track cleanup. Failed or cancelled jobs
don't run it. A conversion whose new file was thrown away (larger than
the original, or below the VMAF minimum) does, with
`SHRINKERR_SPACE_SAVED` 0. Environment vars available:

| Var | Example |
|---|---|
| `SHRINKERR_EVENT` | job_completed |
| `SHRINKERR_FILE_PATH` | /media/TV/Foo/ep.mkv |
| `SHRINKERR_ORIGINAL_PATH` | /media/TV/Foo/ep.mkv |
| `SHRINKERR_ORIGINAL_SIZE` | 3500000000 |
| `SHRINKERR_SPACE_SAVED` | 2476000000 |
| `SHRINKERR_VMAF_SCORE` | 94.3 |
| `SHRINKERR_ENCODER` | nvenc |
| `SHRINKERR_PRESET` | p6 |
| `SHRINKERR_CQ` | 20 |
| `SHRINKERR_FPS` | 195.5 |
| `SHRINKERR_JOB_ID` | 12345 |
| `SHRINKERR_JOB_TYPE` | convert |

Timeout configurable via `post_conversion_script_timeout` (default
300s). Script failure is logged but doesn't fail the job — the encode
is already done.

Common uses: push to a Discord channel with the savings, trigger a
custom archival workflow, feed the score into a quality-trend database.
