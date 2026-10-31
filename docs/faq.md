# FAQ

Quick answers. If the question is "how does X work in detail", one of the
other docs covers it.

## General

**Why x265 / HEVC?**
Shrinkerr targets HEVC because it's the best compression codec supported
natively by every mainstream media server (Plex, Jellyfin, Emby) and
every current device. AV1 is more efficient but client support is still
patchy — broken playback on older TVs / streaming devices is a worse
outcome than a 25% larger file. HEVC is the default; an encoding rule's
**Output codec** can choose AV1 for the files it matches
([Encoding guide § AV1 output](encoding-guide.md#av1-output)).

**Is my data safe? Will Shrinkerr destroy my library?**
Several safety nets make destroying your library hard:
- The encoded output is verified non-empty before the original is
  touched.
- An output shorter than the original (a stalled network share, say), or
  one that lost its HDR, is thrown away and the original kept.
- VMAF rejection (Settings → Video) discards encodes that fell below a
  quality threshold — on at 88 by default for new installs since v0.10.0.
- Originals are kept for N days (7 by default for new installs since
  v0.10.0), or moved to the trash, or deleted — Settings → Automation →
  Originals & Backups.
- Files that grew after encoding are detected — original kept, and the
  file is marked **Came out larger** so it isn't queued again on its own.
- **Review before replace** (Settings → Automation → Originals & Backups)
  holds converted files in Queue → Review until you approve them.

Every check, in order: [What happens to your files](what-happens-to-your-files.md).

That said, back up your library before a big batch if you haven't done
so in a while. We've spent a lot of effort making Shrinkerr conservative
but "defense in depth" beats "trust one tool".

**Why does Shrinkerr ship its own post-processing scripts for NZBGet /
SABnzbd?**
Because Shrinkerr needs to know when a download completes to optionally
block Sonarr/Radarr's import until after conversion (so the smaller
file is what gets imported). A plain "watch folder" scan can't do
that — it only reacts after the file has already moved.

Torrent setups have no such script: add Shrinkerr to Sonarr / Radarr as a
Connect → Webhook instead, and each file is queued once it's imported. The
steps are in Settings → Integrations.

**Does this work without Plex?**
Yes. Plex integration is optional and adds metadata-based rules,
watch-status targeting, and auto-refresh. The core encoding pipeline
works fine without it.

**How does Shrinkerr work with Sonarr and Radarr?**
- After a conversion, and after a rename from the Scanner, Shrinkerr asks
  Sonarr or Radarr to rescan that series or movie, so they pick up the
  new file — its name changes too (the codec tag, e.g. `x264` → `x265`,
  and the extension, always `.mkv`). Files still in a download client's
  post-processing are skipped: Sonarr / Radarr import those anyway.
- A file below its quality profile's cutoff will be replaced by an
  upgrade, so converting it is wasted work. The Scanner's **Below cutoff**
  filter shows those files (read after every full scan, or with **Read
  cutoff & monitoring** in Settings → Integrations).
- The Connect webhook (above) queues each file they import.
- **Request replacement** (Scanner → \*arr actions, or a file's details)
  asks them to blocklist the release, delete the file and search for
  another — for a corrupt file, say.

## Encoding

**Should I use NVENC or libx265?**
See the [Encoding guide](encoding-guide.md). TL;DR: if you have an NVIDIA
GPU, use NVENC and be 10× faster. libx265 gets ~25% smaller files at
the same quality but CPU encoding at any serious quality preset is
slow.

**What's "transparent" quality?**
An encode indistinguishable from the source even in A/B comparison.
Roughly CQ/CRF 18–20, VMAF 95+. Not necessary for most use cases —
CRF 22–24 is perceptually identical for most viewing (couch-distance
playback on a 65" TV).

**My encoded files are bigger than the original. Why?**
Your source is probably already low-bitrate. Shrinkerr's CQ/CRF is a
quality target, not a size target — if your source is at 2 Mbps x264
and you ask for "transparent" HEVC, HEVC may need more bits to preserve
the quality. Turn on the "min bitrate" conversion filter (Settings →
Automation → Conversion Filters) to skip low-bitrate sources.

**How long does a batch take?**
Very rough 1080p ballparks:
- NVENC on a modern NVIDIA card: 50–150 fps → a 45-min episode in 4–10 min
- libx265 fast on a modern CPU: 15–30 fps → 25–50 min
- libx265 medium on M1: ~10 fps → 80 min

Take the queue's ETA as reality; Shrinkerr reports live fps.

**Can I re-encode files that are already x265?**
Yes. Settings → Video → Convert From → check **H.265 / HEVC / x265**.
Useful for migrating between HEVC profiles (e.g. re-encoding x265 8-bit
to x265 Main10 for smaller files). Not useful as a default — you'll waste
compute on files that are already fine.

**Does Shrinkerr keep HDR and Dolby Vision?**
Yes, where it can. HDR10 and HLG files are encoded 10-bit and stay HDR; an
output that lost its HDR (or came out 8-bit) is thrown away and the
original kept. Dolby Vision profiles 7 and 8 keep their Dolby Vision:
dovi_tool moves it into the new file (profile 7 becomes 8.1). Left alone:
Dolby Vision profile 5, and Dolby Vision files that would be resized,
encoded to AV1 or with Quick Sync / VAAPI, or are Blu-ray / DVD discs.
Quick Sync and VAAPI don't convert HDR files at all. Dolby Vision files are
HEVC, so they're only converted when HEVC is ticked in Convert From. See
[Encoding guide § HDR and Dolby Vision](encoding-guide.md#hdr-and-dolby-vision).

## Workers

**Does a remote worker have to be Linux?**
No. Any machine that can run Docker with x86_64 or ARM64 support works.
Windows + WSL2, macOS, Linux, Raspberry Pi all work.

**Do workers need to see the library at the same path as the server?**
No — path mappings handle that. See
[Remote workers § Path mappings](remote-workers.md).

**Can I run the server and a worker on the same machine?**
There's no reason to — the server IS a worker (the "local" node). You'd
just be consuming more RAM. If you want to split a machine's work into
GPU + CPU lanes, use `job_affinity` on the local node.

**What happens if a worker goes offline mid-encode?**
After about five minutes without a check-in, the server marks the worker
offline and puts its job back in the queue for another node; the original
is untouched. If the worker comes back before another node has taken the
job, it gets the job back.

## Storage / library

**Does Shrinkerr scan remote / network shares?**
Yes — anything Docker can mount. SMB, NFS, AFP. Some filesystems are
slow at directory walks (AFP, SMB1), so first-time scans can take a
while.

**Does Shrinkerr write outside the media directories?**
Only to `/app/data` (SQLite DB, cached posters, before / after frames), to
the backup folder if configured, and to the trash if originals go there
(the system trash: on Linux, a `.Trash-<uid>` folder at the top of the
drive or share). VMAF and image-subtitle OCR do their temporary work in
the container's `/tmp`. See
[What happens to your files § Where Shrinkerr writes](what-happens-to-your-files.md#where-shrinkerr-writes).

**I seed my downloads. Does converting them save space?**
No. A file that's still hardlinked — a torrent client seeding it, with
the imported copy usually a hardlink to the same data — keeps its space
as long as the other link exists, so writing a new file uses more space,
not less. Scans record each file's hardlinks: the Scanner's **Hardlinked
(seeding)** filter shows them, and **Skip files that are still hardlinked
(e.g. seeding)** (Settings → Automation → Conversion Filters) leaves them
out of the queue. It's on for new installs; installs from before 0.10
turn it on there. Add to Queue says how many it left out, and **Convert
ignored files too, and override rules** queues them anyway.

**What's the difference between Remove, Ignore and Trash files in the
Scanner?**
- **Remove** takes the row out of the Scanner's list. The file isn't
  touched, and a later scan may list it again (one that has nothing to
  convert or clean up isn't listed again).
- **Ignore** keeps the file in the Scanner (the **Ignored** filter shows
  it) and it's never converted — by Add to Queue, auto-queue or policies —
  but its audio and subtitle clean-up still runs, as with an "ignore" rule.
  Tick **Override rules** in Add to Queue to convert it anyway.
  **Unignore** undoes it.
- **Trash files** moves the file to the system trash (on Linux, a
  `.Trash-<uid>` folder at the top of the drive or share) and drops it
  from the Scanner and the queue. Shrinkerr can't restore it — your
  system's trash can — and Sonarr / Radarr aren't told, so they'll find it
  missing.

**Will Shrinkerr re-scan files it's already seen?**
A full scan reuses what it read from a file last time when the file's size
and modification time haven't changed (its tracks are still sorted with your
current language settings). If either changes, the file is read again.
Rescanning a single folder from the Scanner always re-reads every file in it.

**Some of my tracks show as "unknown" language. Can Shrinkerr fix that?**
Yes. Text subtitles are identified automatically during a scan. For `und`
audio and image (PGS/VobSub) subtitle tracks, use the **Detect languages**
button on a file — or filter to **Unknown language** and run detect on the
batch. Detected languages are written back into the file (in place for mkv).
See [Language detection](language-detection.md).

**Does language detection re-encode my files?**
No. It only rewrites metadata — instantly and in place for mkv
(`mkvpropedit`), or a stream-copy remux for other containers. Audio and
video are never re-encoded.

## Troubleshooting (brief; see [Troubleshooting](troubleshooting.md) for depth)

**The UI is stuck on a loading spinner.**
Most likely a version issue. Pull latest, restart. If persistent,
`docker compose logs shrinkerr | tail -100` — look for a backend
traceback.

**A worker shows 1 fps.**
Either CPU fallback running a slow libx265 preset, or x86 emulation on
ARM. See [Troubleshooting § Remote worker is stuck or slow](troubleshooting.md).

**VMAF isn't running for some files.**
Pre-v0.3.10: filenames with apostrophes broke the filter arg. Fix: update.
Post-v0.3.10: check `docker logs | grep VMAF` for a specific failure
reason.

**Can I see the raw ffmpeg command?**
Yes. Job detail view → "ffmpeg command" section shows exactly what was
run, including all flags.

## Meta

**How is Shrinkerr licensed?**
Apache 2.0. Use it, fork it, run it anywhere. No warranty.

**Where do updates / releases come from?**
Every `v*` tag on GitHub triggers a build of the four image variants.
Release notes for each version are in the GitHub Releases page (pulled
from `CHANGELOG.md`) and under **Recent releases** in Settings → System →
Updates inside the app.

Every push to `develop` builds `:develop` test images. The update notice
follows your channel: a release hears about newer releases, a `:develop`
image about newer develop builds. Settings → System → Updates shows the
channel; untick **Check GitHub for new versions (every 30 minutes)** to
stop the check.

**How do I report a bug?**
Open **System → Doctor**, click **Download diagnostics** (version, setup,
recent log and failed jobs, with passwords, API keys and tokens taken out)
and attach the file to a GitHub issue — the bug report form asks for it.
Otherwise include:
- Version (bottom of sidebar or `docker compose exec shrinkerr cat /app/VERSION`)
- Image tag you're running
- Relevant log excerpt (`docker compose logs shrinkerr --tail 200`)
- Reproduction steps
