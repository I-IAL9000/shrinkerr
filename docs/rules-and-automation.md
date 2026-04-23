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

**NZBGet:** install the post-processing script from Shrinkerr (Settings →
Integrations → NZBGet → Download script) into NZBGet's scripts folder.
Enable it on the categories you want auto-queued (TV, Movies,
etc.). On job completion NZBGet calls the script, which registers the
file with Shrinkerr via `/api/webhooks/post-process`.

**SABnzbd:** similar — Settings → Integrations → SABnzbd → Download
script. Drop it in SABnzbd's post-processing script folder, enable for
the relevant categories.

Settings on the Shrinkerr side (Settings → Integrations → NZBGet):
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
