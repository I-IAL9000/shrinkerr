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

New installs (v0.10.0 on) start safe: originals are kept 7 days, encodes
scoring below VMAF 88 are rejected, subtitles are left alone until you
choose languages, hardlinked (seeding) files are skipped, and content type
detection is on. Installs from before keep their settings; turn these on
in Settings if you want them. Sign-in is required too: a password, or your
reverse proxy's ([Security](security.md#authentication)).

1. **Run the Doctor.** System → Doctor (in the sidebar) checks the media
   folders (readable, writable, free space), the database, ffmpeg and
   VMAF, your default encoder, the connections and remote workers
   ([Troubleshooting](troubleshooting.md#start-with-the-doctor-page)).
2. **Back up your DB.** The only stateful thing in Shrinkerr is
   `./data/shrinkerr.db`. Settings → System → Backups can export a full backup
   (DB + settings + media-dir config); do this after you've configured
   everything and before the first big run.
3. **Choose what happens to originals.** Settings → Automation → Originals & Backups:
   kept for N days (the default for new installs since v0.10.0: 7), moved to the trash, or
   deleted (originals stay put if the output is larger; see VMAF rejection).
   - Kept originals sit in a `.shrinkerr_backup/` folder next to each file
     for that many days before deletion; Undo works meanwhile.
   - `backup_folder` can centralize all backups to one path (e.g.
     `/srv/media-backup`) instead of sprinkling `.shrinkerr_backup/`
     everywhere.
   - A rule can choose differently for its files
     ([Rules](rules-and-automation.md#actions-and-overrides)).
     [What happens to your files](what-happens-to-your-files.md) has every
     step.
4. **Run a 30-second test encode first.** Scanner → pick a file →
   Add to Queue → Test Encode (30s sample). It encodes exactly as the job
   would, and you get the VMAF score + file-size
   projection for your actual settings on a real clip, in a minute. If
   the score is below 90, your CQ/CRF is probably too aggressive.
5. **Small first batch.** 10–20 files, watch the results, tune. Don't
   queue 10k files and walk away on the first night. Add to Queue's
   **What will happen** shows the encoder that will run, what becomes of
   the originals and which audio and subtitle tracks go, before anything
   is queued; **Add & start** queues and starts in one go.

## Encoder + preset picks

The qualities below are the app's
[quality presets](encoding-guide.md#quality-presets); savings are typical
for H.264 sources.

### You have an NVIDIA GPU (Linux/Windows)

**For most libraries — NVENC `p6 / CQ 20`** (the defaults: Quality)
- Looks the same as the original, about 45% smaller
- Slightly larger files than libx265 but usually invisible in quality
- For more savings, Balanced (`CQ 23`, about 55%): excellent quality

**For quality-first — NVENC `p6 / CQ 18`** (Max quality)
- Extra detail at a modest saving (about 35%)
- Useful for 4K sources or very noisy / grainy content

**For backfill / throughput — NVENC `p3 / CQ 26`** (Max savings)
- Faster preset, much smaller files (about 70%); dark or grainy scenes
  can show it
- Fine for TV shows where you'll probably watch once and move on

### You don't have a GPU

**For most libraries — libx265 `medium / CRF 22`** (Quality, the setup
wizard's pick)
- `medium` is the quality-per-minute sweet spot (8–15 fps for 1080p)
- Looks the same as the original

**Quality-first — libx265 `slow / CRF 20`** (Max quality)
- 3–6 fps on 1080p, overnight encoding only
- Visually transparent on most sources

**Fast — libx265 `veryfast / CRF 25`** (Balanced)
- 40–70 fps on 1080p
- Good for "just clear the queue tonight"

### Intel / AMD GPU or a Mac

Pick a preset the same way; QSV, VAAPI and VideoToolbox get it as their
own setting ([their quality](encoding-guide.md#qsv-vaapi-and-videotoolbox-quality)).

### Tuning by content type

- **Anime, animation, film grain, remuxes**: content type detection
  (Settings → Video, on for new installs) gives each its own CQ per
  resolution, a little more quality than everyday content
  ([Content-type detection](encoding-guide.md#content-type-detection)).
  A slower preset helps animation too.
- **Old 480p / DVD rips**: `CQ 16` (libx265 `CRF 18`), what
  resolution-aware quality gives SD — small pixels make artifacts more
  visible.
- **High-grain film / noise**: a slower preset (grain retention
  improves). Detection only spots grain from the name, so a rule on the
  folder helps. A denoise filter (`-vf "hqdn3d"`) in the custom ffmpeg
  flags replaces Shrinkerr's own filters; see
  [Custom ffmpeg flags](encoding-guide.md#custom-ffmpeg-flags).

## VMAF thresholds

Most users land on `vmaf_min_score = 87` after a few weeks of real-world
feedback (new installs since v0.10.0 start at 88):
- 0 — report-only. Start here to learn what your settings produce.
- 80 — "no catastrophic failures". Rejects maybe 1% of encodes.
- 87 — "good quality cutoff". Rejects ~3%. **Recommended for daily use.**
- 93 — "excellent only". Rejects 10–15% on typical content. Use this for
  archival encodes with quality-first presets.

A rule can set its own minimum, e.g. 93 for remuxes you keep while the
rest of the library uses 87
([Rules](rules-and-automation.md#actions-and-overrides)).

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
- `backup_original_days = 0` (no kept copy, so no Undo)
- `trash_original_after_conversion = true` (originals go to the trash;
  `false` deletes them)
- `vmaf_min_score = 0`

Don't start in Reckless mode. The total space cost of Paranoid for a
month is ~50% of your library for 30 days. If you're doing 100 files a
night at 2GB each, that's ~200GB of scratch space a night, ~6TB over 30
days. Plan accordingly.

Tiers can differ per library: a rule can keep, trash or delete its files'
originals (the rule library's "Remuxes: keep the originals" keeps them 30
days).

**Review before replace** (Settings → Automation → Originals & Backups) is
a real dry run for your first batches: converted files wait in **Queue →
Review** — the original untouched, the new file in a hidden
`.shrinkerr-review` folder beside it — until you approve them (put in
place, ahead of the rest of the queue) or reject them (deleted; the file
isn't queued again). If Sonarr / Radarr replaces the original meanwhile,
approving discards the stale conversion instead. The held files take
space, so review them before they pile up.

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
  indexed by status + order and stays snappy.

## When to use rules

Rules are for patterns you'd otherwise apply manually over and over.
Good candidates:
- "My Anime folder needs different settings" → rule matching directory,
  `libx265 slow / CRF 20` override.
- "4K releases should stay higher quality" → rule matching resolution is
  4K, `CQ 18` override (the rule library's "4K: higher quality").
- "Skip any file smaller than 500MB" → rule matching file size less than
  0.5 (GB), action = skip.
- "Use x264 h265 re-encoding only on my tagged `archive-me` series in
  Sonarr" → rule matching Sonarr tag, action = encode.
- "My kids' library keeps only English audio and its originals for 30
  days" → rule matching directory, with **Languages, originals and more**
  set: audio languages, subtitles, originals, minimum VMAF and a minimum
  saving can all differ per rule
  ([Rules](rules-and-automation.md#actions-and-overrides)).

Bad candidates:
- "I want to change all my encodes to be slower." → that's a global
  setting, not a rule.
- "This one file has a problem." → just edit that file's settings in
  the queue. No rule needed.

Start from the **rule library** (ready-made rules), share rules as a YAML
file, and use **Test a file** to see which rule a path gets and why
([Sharing rules and the rule library](rules-and-automation.md#sharing-rules-and-the-rule-library),
[Order and precedence](rules-and-automation.md#order-and-precedence)).

## Auto-queue new files

Off by default — enable with care.

- Safe to turn on once your library is mostly converted. You'll catch
  new downloads within minutes.
- Dangerous to turn on during the initial backfill — a bad rule can
  fire on thousands of files before you notice. Do the backfill
  manually first.
- Pair with a **min-bitrate filter** (Settings → Automation → Conversion
  Filters → `min_bitrate_mbps`) so already-tiny files aren't re-encoded.
- **Skip files that are still hardlinked** (same card; on for new
  installs) leaves torrents that are still seeding alone: converting them
  frees nothing and uses more space.
- **Wait after an import** (same card) holds new imports a few minutes so
  Bazarr's subtitles arrive first and are merged in
  ([Bazarr](rules-and-automation.md#bazarr-and-maintainerr)).
- Auto-queue can be limited to one saved view (Settings → Automation →
  Auto-Queue → **Only files in:**).

For the backfill itself, **Policies** (Settings → Automation) run a saved
view on a schedule within a budget, e.g. at most 50 GB a night, biggest
savings first; Queue → Pending → **Free space…** picks the conversions
that free N GB fastest and says whether they finish by a date
([Policies](rules-and-automation.md#policies),
[Freeing space by a date](rules-and-automation.md#freeing-space-by-a-date)).
