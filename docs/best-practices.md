# Best practices

Opinionated recommendations based on common setups. Not rules — just
starting points that work for most people.

## Contents
- [Before your first batch](#before-your-first-batch)
- [Encoder + preset picks](#encoder--preset-picks)
- [VMAF thresholds](#vmaf-thresholds)
- [Backup strategy](#backup-strategy)
- [Multi-node sizing](#multi-node-sizing)
- [When to use rules](#when-to-use-rules)
- [Auto-queue new files](#auto-queue-new-files)

## Before your first batch

1. **Back up your DB.** The only stateful thing in Shrinkerr is
   `./data/shrinkerr.db`. Settings → Backups can export a full backup
   (DB + settings + media-dir config); do this after you've configured
   everything and before the first big run.
2. **Set a backup folder for originals.** Settings → Video → Post-conversion.
   - `trash_original_after_conversion = false` by default (originals
     stay put if the output is larger; see VMAF rejection).
   - `backup_original_days` > 0 keeps originals in a `.shrinkerr_backup/`
     folder next to each file for that many days before deletion. Set
     to 7–14 for a safety net.
   - `backup_folder` can centralize all backups to one path (e.g.
     `/srv/media-backup`) instead of sprinkling `.shrinkerr_backup/`
     everywhere.
3. **Run a 30-second test encode first.** Scanner → pick a file →
   Estimate modal → Test encode. You get the VMAF score + file-size
   projection for your actual settings on a real clip, in a minute. If
   the score is below 90, your CQ/CRF is probably too aggressive.
4. **Small first batch.** 10–20 files, watch the results, tune. Don't
   queue 10k files and walk away on the first night.

## Encoder + preset picks

### You have an NVIDIA GPU (Linux/Windows)

**For most libraries — NVENC `p3 / CQ 27`**
- Fast, compresses well, typical 55–65% savings on 1080p x264 web-dl
- Slightly larger files than libx265 but usually invisible in quality

**For quality-first — NVENC `p6 / CQ 22`**
- Larger files, better compression decisions, VMAF usually 93+
- Useful for 4K sources or very noisy / grainy content

**For backfill / throughput — NVENC `p1 / CQ 28`**
- Maximum throughput, larger files, some banding on flats
- Fine for TV shows where you'll probably watch once and move on

### You don't have a GPU

**Balanced — libx265 `fast / CRF 23`**
- Reasonable speed on modern CPUs (15–30 fps for 1080p)
- Competitive quality with NVENC `p3/CQ24` at smaller file size

**Quality-first — libx265 `slow / CRF 20`**
- 3–6 fps on 1080p, overnight encoding only
- Visually transparent on most sources

**Fast — libx265 `veryfast / CRF 25`**
- 40–70 fps, about the same as NVENC p3 in compression efficiency
- Good for "just clear the queue tonight"

### Tuning by content type

- **Animation / cartoons**: bump preset one notch slower (more time on
  motion search), CRF unchanged. Animation rewards preset but not CRF.
- **Old 480p / DVD rips**: `CRF 18` (or `CQ 18`) — small pixels make
  artifacts more visible.
- **High-grain film / noise**: slower preset (grain retention
  improves) or add `-vf "hqdn3d"` via custom ffmpeg flags for a light
  denoise.

## VMAF thresholds

Most users land on `vmaf_min_score = 87` after a few weeks of real-world
feedback:
- 0 — report-only. Start here to learn what your settings produce.
- 80 — "no catastrophic failures". Rejects maybe 1% of encodes.
- 87 — "good quality cutoff". Rejects ~3%. **Recommended for daily use.**
- 93 — "excellent only". Rejects 10–15% on typical content. Use this for
  archival encodes with quality-first presets.

What to do when something gets rejected:
1. Check the job's detail — the VMAF score is recorded with min / mean /
   harmonic-mean / max. If min is very low but mean is high, it's a
   scene-specific issue (usually a fast-motion or grainy scene).
2. Lower CRF/CQ by 2–3 and re-queue.
3. If it still fails, the source might be low-bitrate already; tag the
   file to skip and move on.

## Backup strategy

**Three tiers** depending on how much risk you'll tolerate:

**Paranoid (recommended for first 90 days)**
- `backup_original_days = 30`
- `backup_folder = /srv/media-backup`
- Back up `./data` weekly to a different disk
- `vmaf_min_score = 87`

**Normal**
- `backup_original_days = 7`
- No centralized backup folder (lives next to source)
- `./data` backups on major settings changes only
- `vmaf_min_score = 80`

**Reckless**
- `backup_original_days = 0` (delete originals after conversion)
- `trash_original_after_conversion = true`
- `vmaf_min_score = 0`

Don't start in Reckless mode. The total space cost of Paranoid for a
month is ~50% of your library for 30 days. If you're doing 100 files a
night at 2GB each, you need ~200GB of scratch space. Plan accordingly.

## Multi-node sizing

Rule of thumb: **one worker is worth it only if it adds enough
throughput to justify the complexity.**

- **GPU + GPU** — worthwhile if your second GPU is newer/faster OR if
  you need two different driver versions (NVENC codec ABI changes
  between major driver versions).
- **GPU + CPU** — usually worthwhile, but only if the CPU host is
  reading the library over a fast local connection. AFP/SMB over Wi-Fi
  is an anti-pattern.
- **CPU + CPU** — worth it only if total core count is high on one
  machine. Two slow boxes doing libx265 medium aren't faster than one
  fast box; libx265 scales well with cores.

Queue depth matters:
- **< 100 jobs in queue** — single node is fine. Worker setup overhead
  isn't paid back.
- **1k+ jobs** — second node pays off within a day or two.
- **10k+ jobs** — consider 3+ nodes. The UI handles it; the queue is
  indexed by priority + order and stays snappy.

## When to use rules

Rules are for patterns you'd otherwise apply manually over and over.
Good candidates:
- "My Anime folder needs different settings" → rule matching directory,
  `libx265 slow / CRF 20` override.
- "4K releases should stay higher quality" → rule matching resolution >=
  2160p, `CQ 24` override.
- "Skip any file smaller than 500MB" → rule matching file size < 500MB,
  action = skip.
- "Use x264 h265 re-encoding only on my tagged `archive-me` series in
  Sonarr" → rule matching Sonarr tag, action = encode.

Bad candidates:
- "I want to change all my encodes to be slower." → that's a global
  setting, not a rule.
- "This one file has a problem." → just edit that file's settings in
  the queue. No rule needed.

## Auto-queue new files

Off by default — enable with care.

- Safe to turn on once your library is mostly converted. You'll catch
  new downloads within minutes.
- Dangerous to turn on during the initial backfill — a bad rule can
  fire on thousands of files before you notice. Do the backfill
  manually first.
- Pair with a **min-bitrate filter** (Settings → Video → Conversion
  filters → `min_bitrate_mbps`) so already-tiny files aren't re-encoded.
