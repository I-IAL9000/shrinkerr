"""Demo mode (v0.10.0): SHRINKERR_DEMO=1 runs a read-only Shrinkerr with a
made-up library, for a public demo (docs/demo.md, docker-compose.demo.yml).

- Nobody signs in, and nothing can be changed: the API refuses requests
  that would change anything (demo.readOnly) — reads, and the few requests
  that only compute something (an estimate, a rule test, a search), still
  work.
- On a fresh data folder it fills itself with a generated library: films
  and episodes with made-up titles (no files behind them), their tracks,
  and a history of conversions, so the Dashboard, Scanner and Queue have
  something to show.
- The watcher and the queue don't run (update checks and the weekly
  database backup still do).
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone

ENABLED = os.environ.get("SHRINKERR_DEMO", "").strip().lower() in ("1", "true", "yes")

# POSTs that only compute something (or are needed to browse).
READ_ONLY_POSTS = frozenset({
    "/api/auth/ws-ticket", "/api/jobs/estimate", "/api/rules/test", "/api/scan/search",
    "/api/rename/preview-pattern", "/api/rename/tokens", "/api/policies/plan", "/api/settings/language-preview",
})


# Reads that hand out secrets: everyone counts as signed in here (v0.10.0:
# the API key was served to anyone).
SECRET_READS = ("/api/settings/api-key", "/api/settings/backup/download", "/api/settings/nzbget-config",
                "/api/settings/nzbget-script", "/api/settings/sabnzbd-script")


def blocked(method: str, path: str) -> bool:
    """A request the demo refuses: one that would change something, or read
    a secret."""
    if any(path.startswith(p) for p in SECRET_READS):
        return True
    return (path.startswith("/api/") and method.upper() not in ("GET", "HEAD", "OPTIONS")
            and path.rstrip("/") not in READ_ONLY_POSTS)


_ADJ = ("Silent", "Crimson", "Hidden", "Last", "Broken", "Golden", "Distant", "Frozen", "Burning", "Hollow",
        "Midnight", "Northern", "Paper", "Quiet", "Restless", "Scarlet", "Shattered", "Velvet", "Wandering", "Iron")
_NOUN = ("Harbor", "Orchard", "Signal", "Lantern", "Meridian", "Tide", "Garden", "Circuit", "Kingdom", "Archive",
         "Horizon", "Compass", "Echo", "Frontier", "Mirror", "River", "Summit", "Voyage", "Winter", "Cathedral")
_SHOW = ("Night Shift", "The Lighthouse Keepers", "Parallel Lines", "Deep Field", "Small Town Signals",
         "The Long Weekend", "Copper Hill", "Static", "Northbound", "The Glass House")
_SOURCES = (("BluRay", 0.35), ("WEB-DL", 0.35), ("REMUX", 0.15), ("WEBRip", 0.1), ("DVDRip", 0.05))
_GB = 1024 ** 3


def _pick(rng: random.Random, weighted):
    r, acc = rng.random(), 0.0
    for value, weight in weighted:
        acc += weight
        if r < acc:
            return value
    return weighted[-1][0]


def _film(rng: random.Random, title: str, year: int) -> dict:
    tier = _pick(rng, (("2160p", 0.15), ("1080p", 0.6), ("720p", 0.15), ("480p", 0.1)))
    source = "DVDRip" if tier == "480p" else _pick(rng, _SOURCES[:4])
    codec = ("mpeg2video" if tier == "480p" and rng.random() < 0.5 else
             _pick(rng, (("h264", 0.7), ("hevc", 0.25), ("vc1", 0.05))) if tier != "480p" else "mpeg4")
    hdr = ("dv8" if rng.random() < 0.3 else "hdr10") if tier == "2160p" and rng.random() < 0.7 else None
    if hdr:
        codec = "hevc"  # HDR / Dolby Vision releases are HEVC
    width, height = {"2160p": (3840, 1600), "1080p": (1920, 800), "720p": (1280, 536), "480p": (720, 480)}[tier]
    duration = rng.randint(85, 165) * 60
    mbps = {"2160p": 45 if source == "REMUX" else 18, "1080p": 25 if source == "REMUX" else 9,
            "720p": 4.5, "480p": 2.5}[tier] * rng.uniform(0.7, 1.3)
    tag = {"h264": "x264", "hevc": "x265", "vc1": "VC-1", "mpeg2video": "MPEG2", "mpeg4": "XviD"}[codec]
    name = f"{title} ({year}) {tier} {source} {tag}.mkv"
    return {"path": f"/demo/Movies/{title} ({year})/{name}", "size": int(mbps * 1e6 / 8 * duration),
            "codec": codec, "width": width, "height": height, "duration": duration, "hdr": hdr}


def _audio(rng: random.Random, duration: float) -> list[dict]:
    main = _pick(rng, (("truehd", 0.2), ("dts", 0.3), ("eac3", 0.3), ("ac3", 0.2)))
    tracks = [{"stream_index": 1, "language": "eng", "codec": main, "channels": 8 if main == "truehd" else 6,
               "profile": "DTS-HD MA" if main == "dts" and rng.random() < 0.5 else "", "title": "",
               "bitrate": {"truehd": 4_000_000, "dts": 1_536_000, "eac3": 640_000, "ac3": 448_000}[main]}]
    for lang in rng.sample(["fre", "ger", "spa", "ita", "jpn"], rng.randint(0, 3)):
        tracks.append({"stream_index": len(tracks) + 1, "language": lang, "codec": "ac3", "channels": 6,
                       "profile": "", "title": "", "bitrate": 448_000})
    if rng.random() < 0.2:
        tracks.append({"stream_index": len(tracks) + 1, "language": "eng", "codec": "ac3", "channels": 2,
                       "profile": "", "title": "Director's Commentary", "bitrate": 192_000})
    for t in tracks:
        t["size_estimate_bytes"] = int(t["bitrate"] * duration / 8)
    return tracks


def _subs(rng: random.Random, first: int) -> list[dict]:
    langs = ["eng"] + rng.sample(["fre", "ger", "spa", "dut", "swe"], rng.randint(0, 4))
    codec = "hdmv_pgs_subtitle" if rng.random() < 0.5 else "subrip"
    return [{"stream_index": first + i, "language": lang, "codec": codec, "forced": False, "title": ""}
            for i, lang in enumerate(langs)]


def library(seed: int = 7) -> list[dict]:
    """The demo's files: scan_results-like dicts (no files behind them)."""
    rng = random.Random(seed)
    titles = sorted({f"The {a} {n}" if rng.random() < 0.5 else f"{a} {n}" for a in _ADJ for n in _NOUN})
    files = [_film(rng, title, rng.randint(1975, 2025)) for title in rng.sample(titles, 140)]
    for show in _SHOW:
        tier, codec = rng.choice((("1080p", "h264"), ("1080p", "hevc"), ("720p", "h264")))
        for season in (1, 2):
            for ep in range(1, rng.randint(6, 10) + 1):
                duration = rng.randint(22, 58) * 60
                tag = "x264" if codec == "h264" else "x265"
                name = f"{show} - S{season:02d}E{ep:02d} - Episode {ep} {tier} WEB-DL {tag}.mkv"
                files.append({"path": f"/demo/TV/{show}/Season {season:02d}/{name}",
                              "size": int(rng.uniform(4, 8) * 1e6 / 8 * duration), "codec": codec,
                              "width": 1920 if tier == "1080p" else 1280, "height": 1080 if tier == "1080p" else 720,
                              "duration": duration, "hdr": None})
    for f in files:
        f["audio"] = _audio(rng, f["duration"])
        f["subs"] = _subs(rng, len(f["audio"]) + 1)
    return files


async def seed(db_path: str) -> int:
    """Fill an empty database with the demo library; how many files."""
    import aiosqlite
    from backend import scanner
    from backend.encoding_estimates import video_conv_savings_bytes
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM scan_results") as cur:
            if (await cur.fetchone())[0]:
                return 0
    rng = random.Random(11)
    now = datetime.now(timezone.utc)
    source_codecs = scanner.expand_source_codecs(list(scanner.DEFAULT_SOURCE_CODECS))
    rules = scanner.TrackRules(audio_keep=frozenset({"eng"}), sub_keep=frozenset({"eng"}))
    rows, jobs = [], []
    for f in library():
        audio = [t.model_dump() for t in scanner.classify_audio_tracks(f["audio"], "eng", f["duration"], rules=rules)]
        subs = [t.model_dump() for t in scanner.classify_subtitle_tracks(f["subs"], "eng", rules=rules)]
        for done, tracks in ((audio, f["audio"]), (subs, f["subs"])):
            for out, src in zip(done, tracks):
                out.update({k: src[k] for k in ("bitrate", "size_estimate_bytes", "channels", "profile") if k in src})
        convert = f["codec"] in source_codecs
        added = now - timedelta(days=rng.randint(0, 400))
        converted = convert and rng.random() < 0.45
        path, size, codec = f["path"], f["size"], f["codec"]
        if converted:  # converted already: the row is the output's, with a job behind it
            out_size = int(size * rng.uniform(0.3, 0.6))
            out_path = path.replace("x264", "x265").replace("MPEG2", "x265").replace("VC-1", "x265").replace("XviD", "x265")
            done_at = now - timedelta(days=rng.randint(0, 90), minutes=rng.randint(0, 1440))
            removed = [t["stream_index"] for t in audio if not t["keep"]]
            jobs.append({"file_path": out_path, "original_file_path": path, "job_type": "combined" if removed else "convert",
                         "status": "completed", "encoder": "nvenc", "nvenc_cq": 20, "original_size": size,
                         "space_saved": size - out_size, "vmaf_score": round(rng.uniform(91, 98), 2),
                         "created_at": (done_at - timedelta(hours=2)).isoformat(),
                         "started_at": (done_at - timedelta(minutes=f["duration"] / 60 / 3)).isoformat(),
                         "completed_at": done_at.isoformat(), "audio_tracks_to_remove": json.dumps(removed),
                         "encoding_stats": json.dumps({
                             "encoder": "nvenc", "cq": 20, "preset": "p6", "input_size": size, "output_size": out_size,
                             "ratio": round((1 - out_size / size) * 100, 1), "duration": f["duration"],
                             "encode_seconds": round(f["duration"] / rng.uniform(2.5, 5), 1), "output_codec": "hevc",
                             "source_codec": codec, "source_width": f["width"], "source_height": f["height"]})})
            path, size, codec, convert = out_path, out_size, "hevc", False
            audio = [t for t in audio if t["keep"]]
        cq = 20
        rows.append((
            path, size, codec, int(convert), json.dumps(audio), json.dumps(subs), "eng", "api", now.isoformat(),
            added.timestamp(), f["duration"], scanner.removable_audio_flag(audio, "eng"),
            int(any(not s["keep"] for s in subs)),
            int(any(t["codec"] in ("truehd",) or "DTS-HD" in (t.get("profile") or "") for t in audio)),
            f["height"], f["width"], f["hdr"], int(converted),
            video_conv_savings_bytes(size, cq, duration=f["duration"], width=f["width"], height=f["height"],
                                     codec=codec, path=path) if convert else 0))
    pending = [r for r in rows if r[3]][:24]
    for r in pending:
        jobs.append({"file_path": r[0], "job_type": "convert", "status": "pending", "encoder": "nvenc",
                     "original_size": r[1], "created_at": now.isoformat(), "audio_tracks_to_remove": "[]"})
    jobs.append({"file_path": rows[3][0], "job_type": "convert", "status": "failed", "encoder": "nvenc",
                 "original_size": rows[3][1], "created_at": now.isoformat(), "completed_at": now.isoformat(),
                 "error_log": "ffmpeg exited with code 1", "error_key": "errors.ffmpegExited",
                 "error_params": json.dumps({"code": "1"}), "audio_tracks_to_remove": "[]"})
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
            "subtitle_tracks_json, native_language, language_source, scan_timestamp, file_mtime, duration, "
            "has_removable_tracks_flag, has_removable_subs_flag, has_lossless_audio_flag, video_height, video_width, "
            "hdr_format, converted, video_conv_savings_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?)", rows)
        for job in jobs:
            await db.execute(f"INSERT INTO jobs ({', '.join(job)}) VALUES ({', '.join('?' * len(job))})",
                             list(job.values()))
        await db.executemany("INSERT INTO media_dirs (path, label) VALUES (?, ?)",
                             [("/demo/Movies", "Movies"), ("/demo/TV", "TV Shows")])
        for key, value in (("queue_run_state", "paused"), ("setup_dismissed", "true"),
                           ("always_keep_languages", '["eng"]'), ("sub_keep_languages", '["eng"]')):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.execute("INSERT OR IGNORE INTO saved_views (name, filter, created_at) VALUES (?, ?, ?)",
                         ("Big x264 films", "x264,size_large", now.isoformat()))
        await db.commit()
    return len(rows)
