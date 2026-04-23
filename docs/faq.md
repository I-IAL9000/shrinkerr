# FAQ

Quick answers. If the question is "how does X work in detail", one of the
other docs covers it.

## General

**Why x265 / HEVC?**
Shrinkerr targets HEVC because it's the best compression codec supported
natively by every mainstream media server (Plex, Jellyfin, Emby) and
every current device. AV1 is more efficient but client support is still
patchy — broken playback on older TVs / streaming devices is a worse
outcome than a 25% larger file.

**Is my data safe? Will Shrinkerr destroy my library?**
Several safety nets make destroying your library hard:
- The encoded output is verified non-empty before the original is
  touched.
- Optionally enable VMAF rejection (Settings → Video) to discard
  encodes that fell below a quality threshold.
- Optionally keep originals for N days (`backup_original_days` setting).
- Files that grew after encoding are detected — original kept, file
  auto-ignored.

That said, back up your library before a big batch if you haven't done
so in a while. We've spent a lot of effort making Shrinkerr conservative
but "defense in depth" beats "trust one tool".

**Why does Shrinkerr ship its own post-processing scripts for NZBGet /
SABnzbd?**
Because Shrinkerr needs to know when a download completes to optionally
block Sonarr/Radarr's import until after conversion (so the smaller
file is what gets imported). A plain "watch folder" scan can't do
that — it only reacts after the file has already moved.

**Does this work without Plex?**
Yes. Plex integration is optional and adds metadata-based rules,
watch-status targeting, and auto-refresh. The core encoding pipeline
works fine without it.

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
Video → Conversion filters) to skip low-bitrate sources.

**How long does a batch take?**
Very rough 1080p ballparks:
- NVENC on a modern NVIDIA card: 50–150 fps → a 45-min episode in 4–10 min
- libx265 fast on a modern CPU: 15–30 fps → 25–50 min
- libx265 medium on M1: ~10 fps → 80 min

Take the queue's ETA as reality; Shrinkerr reports live fps.

**Can I re-encode files that are already x265?**
Yes. Settings → Video → Convert From → check `h265`. Useful for
migrating between HEVC profiles (e.g. re-encoding x265 8-bit to x265
Main10 for smaller files). Not useful as a default — you'll waste
compute on files that are already fine.

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
The primary marks the job failed, cleans the `.converting.mkv` leftover
on the next scan of that directory, and the original is untouched. If
the worker comes back, you can retry the job.

## Storage / library

**Does Shrinkerr scan remote / network shares?**
Yes — anything Docker can mount. SMB, NFS, AFP. Some filesystems are
slow at directory walks (AFP, SMB1), so first-time scans can take a
while.

**Does Shrinkerr write outside the media directories?**
Only to `/app/data` (SQLite DB, logs, cached posters) and to the backup
folder if configured. If `backup_original_days = 0` and no centralized
`backup_folder`, originals are replaced in place with no external
writes.

**Will Shrinkerr re-scan files it's already seen?**
It uses mtime + size to skip unchanged files. If a file's mtime changes,
it's re-scanned. Forced re-scans via the Scanner "Rescan" button always
re-check every file under the chosen path.

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
from `CHANGELOG.md`) and the Settings → Updates → Changelog modal
inside the app.

**How do I report a bug?**
Open an issue on GitHub. Include:
- Version (bottom of sidebar or `docker compose exec shrinkerr cat /app/VERSION`)
- Image tag you're running
- Relevant log excerpt (`docker compose logs shrinkerr --tail 200`)
- Reproduction steps
