import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Optional

from backend.config import settings
from backend.models import AudioTrack, ScannedFile


async def probe_file(file_path: str) -> Optional[dict]:
    """Run ffprobe on a file and return parsed metadata dict, or None on failure."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.ffprobe_timeout
        )
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode())
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        print(f"[SCANNER] ffprobe timeout on: {file_path}", flush=True)
        return None
    except Exception:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return None

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_codec = ""
    video_width = 0
    video_height = 0
    video_fps: float = 0.0
    audio_tracks = []
    subtitle_tracks = []

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not video_codec:
            video_codec = stream.get("codec_name", "")
            video_width = stream.get("width", 0) or 0
            video_height = stream.get("height", 0) or 0
            # Frame rate: prefer r_frame_rate ("24000/1001" → 23.976),
            # fall back to avg_frame_rate. Used by progress estimation
            # downstream when ffmpeg's `time=` field is N/A (v0.3.43+).
            fr = stream.get("r_frame_rate") or stream.get("avg_frame_rate") or ""
            try:
                if "/" in fr:
                    num, den = fr.split("/")
                    den_v = float(den)
                    video_fps = float(num) / den_v if den_v else 0.0
                elif fr:
                    video_fps = float(fr)
            except (ValueError, ZeroDivisionError):
                video_fps = 0.0
        elif codec_type == "audio":
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            lang = (tags.get("language") or "und").lower()
            bitrate = stream.get("bit_rate")
            try:
                bitrate = int(bitrate) if bitrate else None
            except (ValueError, TypeError):
                bitrate = None
            audio_tracks.append({
                "stream_index": stream.get("index", len(audio_tracks) + 1),
                "language": lang,
                "codec": stream.get("codec_name", ""),
                "profile": stream.get("profile", ""),
                "channels": stream.get("channels", 2),
                "title": tags.get("title", ""),
                "bitrate": bitrate,
                "disposition": disposition,
            })
        elif codec_type == "subtitle":
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            lang = (tags.get("language") or "und").lower()
            subtitle_tracks.append({
                "stream_index": stream.get("index"),
                "language": lang,
                "codec": stream.get("codec_name", ""),
                "title": tags.get("title", ""),
                "forced": bool(disposition.get("forced", 0)),
            })

    try:
        duration = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        duration = 0.0

    try:
        raw_size = fmt.get("size")
        if raw_size is not None:
            file_size = int(raw_size)
        else:
            file_size = os.path.getsize(file_path)
    except (ValueError, TypeError, OSError):
        file_size = 0

    # Corruption heuristic: a media file with no video stream at all is almost
    # always a container that ffprobe couldn't fully parse (damaged headers,
    # truncated download, etc). Treat like a probe failure so it lands in the
    # corrupt branch of scan_directory() and shows up under the Corrupt filter.
    # We check the raw streams list (not just video_codec) so cover-art / image
    # attachments don't fool us.
    has_real_video = any(
        s.get("codec_type") == "video"
        and s.get("codec_name") not in ("mjpeg", "png", "bmp", "gif", "ansi")
        and s.get("disposition", {}).get("attached_pic", 0) != 1
        for s in streams
    )
    if not has_real_video:
        print(f"[SCANNER] No decodable video stream in: {file_path} — marking corrupt", flush=True)
        return None

    return {
        "video_codec": video_codec,
        "video_width": video_width,
        "video_height": video_height,
        "video_fps": video_fps,
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
        "duration": duration,
        "file_size": file_size,
    }


def detect_native_language(audio_tracks: list[dict]) -> str:
    """Detect native language: prefer disposition.original=1, else first track."""
    for track in audio_tracks:
        disposition = track.get("disposition", {}) or {}
        if disposition.get("original") == 1:
            return track.get("language", "und")
    if audio_tracks:
        return audio_tracks[0].get("language", "und")
    return "und"


def is_x264(codec: str) -> bool:
    """Return True if codec string represents H.264/AVC."""
    c = codec.lower()
    return c in ("h264", "x264", "avc", "avc1")


def is_x265(codec: str) -> bool:
    """Return True if codec string represents H.265/HEVC."""
    c = codec.lower()
    return c in ("h265", "x265", "hevc")


# Map settings source_codecs values to ffprobe codec names
CODEC_FAMILIES = {
    "h264": ("h264", "x264", "avc", "avc1"),
    "hevc": ("hevc", "h265", "x265"),
    "av1": ("av1", "av01", "libaom-av1", "libsvtav1", "svt-av1"),
    "mpeg2": ("mpeg2video", "mpeg2"),
    "mpeg4": ("mpeg4", "xvid", "divx", "dx50", "mp4v"),
    "vc1": ("vc1", "wmv3", "wmv2", "wmv1"),
    "msmpeg4v3": ("msmpeg4v3", "msmpeg4v2", "msmpeg4"),
    "vp9": ("vp9",),
}


def codec_matches_source(video_codec: str, source_codecs: list[str]) -> bool:
    """Check if a video codec matches any of the configured source codecs to convert."""
    c = video_codec.lower()
    for source in source_codecs:
        if source in CODEC_FAMILIES:
            if c in CODEC_FAMILIES[source]:
                return True
        elif c == source.lower():
            return True
    return False


def is_av1(codec: str) -> bool:
    """Return True if codec string represents AV1."""
    c = codec.lower()
    return c in ("av1", "av01", "libaom-av1", "libsvtav1", "svt-av1")


# Language variant groups — codes that represent the same spoken language
LANGUAGE_EQUIVALENTS = {
    # Norwegian
    "nor": {"nor", "nob", "nno"},
    "nob": {"nor", "nob", "nno"},
    "nno": {"nor", "nob", "nno"},
    # Chinese
    "zho": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    "chi": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    "cmn": {"zho", "chi", "cmn", "cn"},
    "yue": {"zho", "chi", "yue", "cn"},
    "cn": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    # Czech
    "ces": {"ces", "cze"},
    "cze": {"ces", "cze"},
    # Dutch
    "nld": {"nld", "dut"},
    "dut": {"nld", "dut"},
    # French
    "fra": {"fra", "fre"},
    "fre": {"fra", "fre"},
    # German
    "deu": {"deu", "ger"},
    "ger": {"deu", "ger"},
    # Greek
    "ell": {"ell", "gre"},
    "gre": {"ell", "gre"},
    # Icelandic
    "isl": {"isl", "ice"},
    "ice": {"isl", "ice"},
    # Persian
    "fas": {"fas", "per"},
    "per": {"fas", "per"},
    # Romanian
    "ron": {"ron", "rum"},
    "rum": {"ron", "rum"},
    # Slovak
    "slk": {"slk", "slo"},
    "slo": {"slk", "slo"},
    # Malay
    "msa": {"msa", "may"},
    "may": {"msa", "may"},
    # Portuguese (includes Brazilian Portuguese)
    "por": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt-br": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt-pt": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "ptb": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    # Spanish (includes Latin American variants)
    "spa": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es-mx": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es-419": {"spa", "es", "es-mx", "es-419", "es-es"},
    # English variants
    "eng": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en-us": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en-gb": {"eng", "en", "en-us", "en-gb", "en-au"},
    # Serbo-Croatian
    "srp": {"srp", "hrv", "bos", "hbs"},
    "hrv": {"srp", "hrv", "bos", "hbs"},
    "bos": {"srp", "hrv", "bos", "hbs"},
    "hbs": {"srp", "hrv", "bos", "hbs"},
}


def languages_match(lang1: str, lang2: str) -> bool:
    """Check if two language codes represent the same language, accounting for variants."""
    l1 = lang1.lower()
    l2 = lang2.lower()
    if l1 == l2:
        return True
    # Check equivalence groups
    group = LANGUAGE_EQUIVALENTS.get(l1)
    if group and l2 in group:
        return True
    return False


# ── External subtitle detection ──────────────────────────────────────────────

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx"}

# Known ISO 639-1 (2-letter) and 639-2/B (3-letter) codes for validation.
# We only need enough to distinguish real language tags from random filename parts.
_KNOWN_LANG_CODES = {
    # 2-letter
    "en", "es", "fr", "de", "it", "pt", "ru", "ja", "ko", "zh", "ar", "hi",
    "nl", "sv", "no", "da", "fi", "pl", "cs", "sk", "hu", "ro", "bg", "el",
    "tr", "he", "th", "vi", "id", "ms", "is", "hr", "sr", "sl", "uk", "ca",
    "et", "lv", "lt", "ga", "af", "sw", "tl", "bn",
    # 3-letter (common ones used in media)
    "eng", "spa", "fre", "fra", "ger", "deu", "ita", "por", "rus", "jpn",
    "kor", "zho", "chi", "ara", "hin", "nld", "dut", "swe", "nor", "nob",
    "dan", "fin", "pol", "cze", "ces", "slo", "slk", "hun", "rum", "ron",
    "bul", "gre", "ell", "tur", "heb", "tha", "vie", "ind", "msa", "may",
    "ice", "isl", "hrv", "srp", "slv", "ukr", "cat", "est", "lav", "lit",
    "gle", "afr", "swa", "tgl", "ben", "und",
}

import re as _re
_EXT_SUB_LANG_RE = _re.compile(
    r"\.([a-zA-Z]{2,3})"           # language code (2-3 letters)
    r"(?:\.(forced|sdh|cc|hi))?"    # optional flag
    r"$",
    _re.IGNORECASE,
)

_EXT_CODEC_MAP = {
    ".srt": "subrip",
    ".ass": "ass",
    ".ssa": "ass",
    ".vtt": "webvtt",
    ".sub": "subviewer",
    ".sup": "hdmv_pgs_subtitle",
    ".idx": "dvd_subtitle",
}


def detect_external_subtitles(video_path: str) -> list[dict]:
    """Detect external subtitle files alongside a video file.

    Matching strategies (in order):
      1. Sub filename starts with the full video stem (strictest)
      2. Sub filename shares the same S##E## pattern as the video (TV)
      3. If only one video file in the folder, all sub files belong to it
    """
    video = Path(video_path)
    video_stem = video.stem
    parent = video.parent
    results: list[dict] = []

    if not parent.exists():
        return results

    try:
        siblings = list(parent.iterdir())
    except OSError:
        return results

    sub_files = [f for f in siblings if f.suffix.lower() in SUBTITLE_EXTENSIONS]
    if not sub_files:
        return results

    # VobSub is a paired format: `.idx` (index/metadata) + `.sub` (bitmap
    # data) must both exist or ffmpeg's vobsub demuxer fails. ffmpeg
    # auto-resolves the `.sub` partner from disk when given the `.idx`
    # path, so we represent each VobSub pair via its `.idx` file alone.
    # Drop any `.sub` whose `.idx` partner is missing (orphan or
    # subviewer-format text — both unsafe to feed in blindly), and drop
    # any `.sub` whose `.idx` partner exists (the `.idx` will represent
    # the pair). Same for `.idx` without `.sub`. v0.3.46+.
    available_stems_lower = {f.stem.lower(): f for f in siblings}
    filtered: list[Path] = []
    for f in sub_files:
        ext = f.suffix.lower()
        if ext == ".sub":
            # Skip — the `.idx` partner (if it exists) will represent the pair.
            # If no `.idx` partner exists, this is an orphan we can't safely use.
            partner = f.parent / (f.stem + ".idx")
            if not partner.exists():
                print(f"[EXT-SUBS]   Skip '{f.name}' — VobSub `.sub` without paired `.idx`", flush=True)
            continue
        if ext == ".idx":
            partner = f.parent / (f.stem + ".sub")
            if not partner.exists():
                print(f"[EXT-SUBS]   Skip '{f.name}' — VobSub `.idx` without paired `.sub`", flush=True)
                continue
        filtered.append(f)
    sub_files = filtered
    if not sub_files:
        return results

    video_files = [f for f in siblings if f.suffix.lower() in {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v", ".webm"}]
    only_one_video = len(video_files) == 1

    # Extract S##E## pattern from the video filename for TV episode matching
    import re as _re
    ep_match = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", video_stem)
    video_ep_key = f"s{int(ep_match.group(1)):02d}e{int(ep_match.group(2)):02d}" if ep_match else None

    print(f"[EXT-SUBS] {video.name}: {len(sub_files)} sub file(s), {len(video_files)} video file(s) in folder", flush=True)

    for f in sub_files:
        fname = f.name
        match_reason = None

        # Strategy 1: full stem match
        if fname.lower().startswith(video_stem.lower()):
            match_reason = "stem"
        # Strategy 2: same episode key
        elif video_ep_key:
            sub_ep = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", fname)
            if sub_ep:
                sub_ep_key = f"s{int(sub_ep.group(1)):02d}e{int(sub_ep.group(2)):02d}"
                if sub_ep_key == video_ep_key:
                    match_reason = "episode"
        # Strategy 3: only one video in folder
        if not match_reason and only_one_video:
            match_reason = "single-video"

        if not match_reason:
            print(f"[EXT-SUBS]   Skip '{fname}' — no match (stem/episode/single)", flush=True)
            continue
        print(f"[EXT-SUBS]   Match '{fname}' via {match_reason}", flush=True)
        # Don't match the video file itself
        if f == video:
            continue

        # Parse language from the end of the sub's stem.
        # For stem match: "Video.eng.srt" → stem "Video.eng" → last segment "eng"
        # For episode match: "Show.S01E01.eng.srt" → stem "Show.S01E01.eng" → last "eng"
        # For single-video match: "subs.eng.forced.srt" → stem "subs.eng.forced" → "eng" + forced
        sub_stem = f.stem  # e.g. "Movie.eng.forced" or "Show.S01E01.eng"

        language = "und"
        forced = False
        sdh = False
        title_parts = []

        # Try the end of the sub stem (matches stem-match case): .eng[.forced|.sdh]?
        if True:
            m = _EXT_SUB_LANG_RE.search(sub_stem)
            if m:
                lang_candidate = m.group(1).lower()
                if lang_candidate in _KNOWN_LANG_CODES:
                    language = lang_candidate
                flag = (m.group(2) or "").lower()
                if flag == "forced":
                    forced = True
                elif flag in ("sdh", "hi", "cc"):
                    sdh = True
                    title_parts.append(flag.upper())

        codec = _EXT_CODEC_MAP.get(f.suffix.lower(), "subrip")

        results.append({
            "language": language,
            "codec": codec,
            "title": " ".join(title_parts) if title_parts else f.name,
            "forced": forced,
            "external_path": str(f),
            "stream_index": 0,  # placeholder, assigned by caller
        })

    # Sort by language then filename for deterministic order
    results.sort(key=lambda x: (x["language"], x["external_path"]))
    return results


_cleanup_enabled_cache: dict[str, bool] = {}


def _is_cleanup_enabled(key: str) -> bool:
    """Check if audio/subtitle cleanup is enabled in DB settings. Cached per key."""
    if key in _cleanup_enabled_cache:
        return _cleanup_enabled_cache[key]
    try:
        import sqlite3
        db = sqlite3.connect(settings.db_path)
        try:
            cur = db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            val = row[0].lower() == "true" if row else True
        finally:
            db.close()
    except Exception:
        val = True
    _cleanup_enabled_cache[key] = val
    return val


def classify_audio_tracks(
    tracks: list[dict], native_language: str, duration: float = 0
) -> list[AudioTrack]:
    """
    Classify audio tracks for keep/remove.

    Rules:
    - Always keep (locked=True): languages in settings.always_keep_languages
    - Keep native language (keep=True, locked=False unless native is in always_keep)
    - Ignore (keep=True, locked=False) und/unknown tracks
    - Everything else: keep=False
    """
    # If audio cleanup is disabled, keep all tracks
    if not _is_cleanup_enabled("audio_cleanup_enabled"):
        return [
            AudioTrack(
                stream_index=t.get("stream_index", 0),
                language=(t.get("language") or "und").lower(),
                codec=t.get("codec", ""),
                channels=t.get("channels", 2),
                title=t.get("title", ""),
                bitrate=t.get("bitrate"),
                profile=t.get("profile"),
                keep=True, locked=True,
            ) for t in tracks
        ]

    always_keep = {lang.lower() for lang in settings.always_keep_languages}
    native = native_language.lower() if native_language else "und"
    auto_keep_native = _is_cleanup_enabled("keep_native_language")  # defaults True

    result = []
    for track in tracks:
        lang = (track.get("language") or "und").lower()
        bitrate = track.get("bitrate")
        size_estimate = None
        if bitrate and duration > 0:
            try:
                # bitrate is in bits/second from ffprobe; convert to bytes for the full duration
                size_estimate = int(int(bitrate) * duration / 8)
            except (ValueError, TypeError):
                size_estimate = None

        # Check always_keep with variant matching
        is_always_keep = any(languages_match(lang, k) for k in always_keep)
        if is_always_keep:
            keep = True
            locked = True
        elif auto_keep_native and languages_match(lang, native):
            keep = True
            locked = False
        elif lang == "und":
            keep = True
            locked = False
        else:
            keep = False
            locked = False

        result.append(
            AudioTrack(
                stream_index=track.get("stream_index", 0),
                language=lang,
                codec=track.get("codec", ""),
                channels=track.get("channels", 2),
                title=track.get("title", ""),
                bitrate=bitrate,
                profile=track.get("profile"),
                size_estimate_bytes=size_estimate,
                keep=keep,
                locked=locked,
            )
        )

    # Safety: if only 1 track, always keep it regardless of language
    if len(result) == 1 and not result[0].keep:
        result[0] = result[0].model_copy(update={"keep": True, "locked": True})

    # Safety: never remove ALL tracks — if all would be removed, keep the first one
    if len(result) > 1 and all(not t.keep for t in result):
        result[0] = result[0].model_copy(update={"keep": True, "locked": False})

    # Reorder: move native language tracks to the top so they become the default
    # playback track. Even if an always_keep language (e.g. English) is first,
    # the native language (original audio) should be the primary track.
    if native and native != "und":
        native_tracks = [t for t in result if languages_match(t.language, native)]
        other_tracks = [t for t in result if not languages_match(t.language, native)]
        result = native_tracks + other_tracks

    return result


_sub_settings_cache: dict | None = None
_sub_settings_loaded = False


def _load_sub_settings() -> tuple[set[str], bool]:
    """Load subtitle settings from DB once and cache."""
    global _sub_settings_cache, _sub_settings_loaded
    if _sub_settings_loaded and _sub_settings_cache is not None:
        return _sub_settings_cache["langs"], _sub_settings_cache["keep_unknown"]

    sub_keep_langs = {lang.lower() for lang in settings.always_keep_languages}
    sub_keep_unknown = True

    try:
        import sqlite3
        db = sqlite3.connect(settings.db_path)
        try:
            cur = db.execute("SELECT key, value FROM settings WHERE key IN ('sub_keep_languages', 'sub_keep_unknown')")
            for row in cur:
                if row[0] == "sub_keep_languages":
                    sub_keep_langs = {l.lower() for l in json.loads(row[1])}
                elif row[0] == "sub_keep_unknown":
                    sub_keep_unknown = row[1].lower() == "true"
        finally:
            db.close()
    except Exception:
        pass

    _sub_settings_cache = {"langs": sub_keep_langs, "keep_unknown": sub_keep_unknown}
    _sub_settings_loaded = True
    return sub_keep_langs, sub_keep_unknown


def invalidate_sub_settings_cache():
    """Call when subtitle/audio cleanup settings are updated to force a reload."""
    global _sub_settings_cache, _sub_settings_loaded, _cleanup_enabled_cache
    _sub_settings_cache = None
    _sub_settings_loaded = False
    _cleanup_enabled_cache.clear()


def classify_subtitle_tracks(
    tracks: list[dict], native_language: str
) -> list["SubtitleTrack"]:
    """
    Classify subtitle tracks for keep/remove.

    Uses separate settings: sub_keep_languages and sub_keep_unknown.
    Forced subtitles are always kept.
    """
    from backend.models import SubtitleTrack

    # If subtitle cleanup is disabled, keep all tracks
    if not _is_cleanup_enabled("sub_cleanup_enabled"):
        return [
            SubtitleTrack(
                stream_index=t.get("stream_index", 0),
                language=(t.get("language") or "und").lower(),
                codec=t.get("codec", ""),
                title=t.get("title", ""),
                forced=t.get("forced", False),
                keep=True, locked=True,
            ) for t in tracks
        ]

    sub_keep_langs, sub_keep_unknown = _load_sub_settings()
    native = native_language.lower() if native_language else "und"
    auto_keep_native = _is_cleanup_enabled("keep_native_language")  # defaults True

    result = []
    for track in tracks:
        lang = (track.get("language") or "und").lower()
        forced = track.get("forced", False)

        # Forced subs only kept if they match native language or user's keep languages
        if forced:
            is_relevant = (lang == "und"
                or (auto_keep_native and languages_match(lang, native))
                or any(languages_match(lang, k) for k in sub_keep_langs))
            keep = is_relevant
            locked = is_relevant
        elif any(languages_match(lang, k) for k in sub_keep_langs):
            keep = True
            locked = True
        elif auto_keep_native and languages_match(lang, native):
            keep = True
            locked = False
        elif lang == "und":
            keep = sub_keep_unknown
            locked = False
        else:
            keep = False
            locked = False

        result.append(
            SubtitleTrack(
                stream_index=track.get("stream_index", 0),
                language=lang,
                codec=track.get("codec", ""),
                title=track.get("title", ""),
                forced=forced,
                keep=keep,
                locked=locked,
            )
        )

    return result


def estimate_savings(
    file_size: int,
    needs_conversion: bool,
    tracks_to_remove: list[AudioTrack],
    duration: float,
) -> int:
    """
    Estimate bytes saved.

    - 30% of file_size for video conversion (if needs_conversion)
    - Sum of bitrate * duration / 8 for each audio track being removed
    """
    savings = 0
    if needs_conversion:
        savings += int(file_size * 0.30)
    for track in tracks_to_remove:
        if track.bitrate and duration:
            savings += int(track.bitrate * duration / 8)
    return savings


async def scan_directory(
    dir_path: str,
    progress_callback: Optional[Callable] = None,
    result_callback: Optional[Callable] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[ScannedFile]:
    """
    Walk dir_path, probe video files, classify tracks, build ScannedFile list.

    If result_callback is provided, each result is passed to it immediately
    (for streaming/batched DB writes). Results are still returned as a list
    for backward compatibility, but with result_callback the list is empty
    to save memory.

    Skips files that are already x265 with no removable tracks.
    """
    dir_path = Path(dir_path)
    extensions = {ext.lower() for ext in settings.video_extensions}

    # Load configured source codecs from DB
    source_codecs = ["h264"]  # default
    try:
        import json as _json
        import aiosqlite as _aiosqlite
        _db = await _aiosqlite.connect(settings.db_path)
        try:
            async with _db.execute("SELECT value FROM settings WHERE key = 'source_codecs'") as _cur:
                _row = await _cur.fetchone()
                if _row and _row[0]:
                    source_codecs = _json.loads(_row[0])
        finally:
            await _db.close()
    except Exception:
        pass
    print(f"[SCANNER] Source codecs to convert: {source_codecs}", flush=True)

    # Collect all candidate files first
    all_files = []
    for root, _dirs, files in os.walk(dir_path):
        for name in files:
            if name.startswith("."):
                continue  # Skip hidden/dot files (macOS resource forks, etc.)
            if Path(name).suffix.lower() in extensions:
                all_files.append(Path(root) / name)

    # Detect duplicate x264 / HEVC pairs — if an HEVC version of the same
    # release already exists next to the x264 source, skip the x264. This
    # happens when a conversion was interrupted after writing the output
    # but before deleting the original. The HEVC output's filename tag
    # depends on the encoder used:
    #   - libx265 output → `x265` in the filename
    #   - NVENC   output → `h265`
    # so we check BOTH possibilities, not just `x265`.
    from backend.converter import rename_source_to_target_codec
    all_paths_set = {str(f) for f in all_files}
    skip_paths: set[str] = set()
    for f in all_files:
        name = f.name
        candidates: set[str] = set()
        for encoder in ("libx265", "nvenc"):
            renamed = rename_source_to_target_codec(name, encoder=encoder)
            if renamed != name:
                # The conversion pipeline always writes .mkv regardless of
                # source container, so match the HEVC sibling with that
                # extension explicitly.
                stem_only = renamed.rsplit(".", 1)[0] if "." in renamed else renamed
                candidates.add(str(f.parent / f"{stem_only}.mkv"))
        hits = [c for c in candidates if c in all_paths_set and str(f) != c]
        if hits:
            skip_paths.add(str(f))
            print(
                f"[SCANNER] Skipping duplicate x264 (HEVC version exists: "
                f"{Path(hits[0]).name}): {f.name}",
                flush=True,
            )

    all_files = [f for f in all_files if str(f) not in skip_paths]
    total = len(all_files)
    results: list[ScannedFile] = []

    # Load ignored files set
    import aiosqlite
    ignored_paths: set[str] = set()
    try:
        db = await aiosqlite.connect(settings.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT file_path FROM ignored_files") as cur:
                rows = await cur.fetchall()
                ignored_paths = {row["file_path"] for row in rows}
        finally:
            await db.close()
    except Exception:
        pass

    for idx, file_path in enumerate(all_files):
        if cancel_check and cancel_check():
            print(f"[SCANNER] Cancelled after {idx} files in {dir_path}", flush=True)
            break

        # Yield to event loop so other tasks (queue worker, websocket, API) can run
        await asyncio.sleep(0.005)

        if progress_callback:
            await progress_callback(
                status="scanning",
                current_file=str(file_path),
                files_found=total,
                files_probed=idx,
                total_files=total,
            )

        probe = await probe_file(str(file_path))
        if idx == 0:
            if probe is None:
                print(f"[SCANNER] WARNING: First file probe FAILED: {file_path}", flush=True)
            else:
                print(f"[SCANNER] First file probe OK: codec={probe.get('video_codec')}, dur={probe.get('duration')}", flush=True)
            # Test ffprobe binary
            import shutil
            ffprobe_path = shutil.which("ffprobe")
            print(f"[SCANNER] ffprobe binary: {ffprobe_path}", flush=True)
            if probe is None:
                try:
                    test_proc = await asyncio.create_subprocess_exec(
                        "ffprobe", "-version",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    test_out, test_err = await asyncio.wait_for(test_proc.communicate(), timeout=5)
                    print(f"[SCANNER] ffprobe -version: rc={test_proc.returncode}, {test_out.decode()[:200]}", flush=True)
                except Exception as test_exc:
                    print(f"[SCANNER] ffprobe binary ERROR: {test_exc}", flush=True)
                # Also try probing with full stderr to see what's wrong
                try:
                    test_proc2 = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error", "-show_format", str(file_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    t_out, t_err = await asyncio.wait_for(test_proc2.communicate(), timeout=10)
                    print(f"[SCANNER] ffprobe debug: rc={test_proc2.returncode}, stderr={t_err.decode()[:300]}", flush=True)
                except Exception as te:
                    print(f"[SCANNER] ffprobe debug failed: {te}", flush=True)
        if probe is None:
            # Store corrupt file entry so it appears in the Corrupt filter.
            # Even for unprobed files, still detect external subtitles so they
            # show up in the UI (independent of ffprobe success).
            try:
                file_size_corrupt = os.path.getsize(str(file_path))
                file_mtime_corrupt = os.path.getmtime(str(file_path))
            except OSError:
                file_size_corrupt = 0
                file_mtime_corrupt = None

            # Detect external subs even for corrupt files
            corrupt_subs: list = []
            corrupt_has_ext = False
            try:
                ext_subs_raw_c = detect_external_subtitles(str(file_path))
                corrupt_has_ext = len(ext_subs_raw_c) > 0
                if ext_subs_raw_c:
                    for i, es in enumerate(ext_subs_raw_c):
                        es["stream_index"] = -(i + 1)
                    for cls_track, raw in zip(classify_subtitle_tracks(ext_subs_raw_c, "und"), ext_subs_raw_c):
                        cls_track = cls_track.model_copy(update={
                            "external": True,
                            "external_path": raw["external_path"],
                        })
                        corrupt_subs.append(cls_track)
            except Exception as exc:
                print(f"[SCANNER] Ext sub detection failed for unprobed {file_path.name}: {exc}", flush=True)

            corrupt_entry = ScannedFile(
                file_path=str(file_path),
                file_name=file_path.name,
                folder_name=file_path.parent.name,
                file_size=file_size_corrupt,
                file_size_gb=round(file_size_corrupt / (1024 ** 3), 3),
                video_codec="unknown",
                needs_conversion=False,
                audio_tracks=[],
                subtitle_tracks=corrupt_subs,
                native_language="und",
                has_removable_tracks=False,
                has_removable_subs=False,
                has_external_subs=corrupt_has_ext,
                estimated_savings_bytes=0,
                estimated_savings_gb=0,
                file_mtime=file_mtime_corrupt,
                duration=0,
                probe_status="corrupt",
            )
            if result_callback:
                await result_callback(corrupt_entry)
            else:
                results.append(corrupt_entry)
            continue

        video_codec = probe["video_codec"]
        raw_tracks = probe["audio_tracks"]
        duration = probe["duration"]
        file_size = probe["file_size"]

        # Try API-based language detection first (skip if cancelled to allow fast exit).
        # Also skip when the file lives in a directory the user marked
        # "Other" — those directories hold non-movie/non-tv content (home
        # videos, music videos, lectures, etc.) and TMDB matches against
        # them produce spurious results. v0.3.33+.
        api_lang = None
        if not (cancel_check and cancel_check()):
            try:
                from backend.media_paths import is_other_typed_dir
                if await is_other_typed_dir(str(file_path)):
                    pass  # Skip TMDB lookup — directory is non-cataloguable
                else:
                    from backend.metadata import lookup_original_language
                    api_lang = await asyncio.wait_for(
                        lookup_original_language(str(file_path)),
                        timeout=10,
                    )
            except asyncio.TimeoutError:
                print(f"[SCANNER] Metadata lookup timed out for {file_path.name}", flush=True)
            except Exception as exc:
                print(f"[SCANNER] Metadata lookup failed for {file_path.name}: {exc}", flush=True)

        native_lang = api_lang if api_lang else detect_native_language(raw_tracks)
        language_source = "api" if api_lang else "heuristic"

        needs_conversion = codec_matches_source(video_codec, source_codecs)
        audio_tracks = classify_audio_tracks(raw_tracks, native_lang, duration)
        raw_subs = probe.get("subtitle_tracks", [])
        subtitle_tracks = classify_subtitle_tracks(raw_subs, native_lang)

        # Detect external subtitle files (.srt, .ass, etc.) alongside the video
        ext_subs_raw = detect_external_subtitles(str(file_path))
        has_external_subs = len(ext_subs_raw) > 0
        if ext_subs_raw:
            # Assign negative stream indices to avoid collision with embedded
            for i, es in enumerate(ext_subs_raw):
                es["stream_index"] = -(i + 1)
            # Classify with the same language rules as embedded subs
            ext_classified = classify_subtitle_tracks(ext_subs_raw, native_lang)
            # Carry over external fields that classify doesn't know about
            for cls_track, raw in zip(ext_classified, ext_subs_raw):
                cls_track = cls_track.model_copy(update={
                    "external": True,
                    "external_path": raw["external_path"],
                })
                subtitle_tracks.append(cls_track)

        tracks_to_remove = [t for t in audio_tracks if not t.keep]
        has_removable = len(tracks_to_remove) > 0
        has_removable_subs = any(not t.keep for t in subtitle_tracks)

        # Check if native-language audio isn't the first stream (needs reorder)
        needs_audio_reorder = False
        if _is_cleanup_enabled("reorder_native_audio") and len(audio_tracks) > 1 and native_lang and native_lang.lower() != "und":
            first_lang = (audio_tracks[0].language or "").lower()
            needs_audio_reorder = not languages_match(first_lang, native_lang.lower())

        savings_bytes = estimate_savings(file_size, needs_conversion, tracks_to_remove, duration)
        savings_gb = round(savings_bytes / (1024 ** 3), 3)

        # Get file modification time from disk
        try:
            file_mtime = os.path.getmtime(str(file_path))
        except OSError:
            file_mtime = None

        scanned = ScannedFile(
            file_path=str(file_path),
            file_name=file_path.name,
            folder_name=file_path.parent.name,
            file_size=file_size,
            file_size_gb=round(file_size / (1024 ** 3), 3),
            video_codec=video_codec,
            needs_conversion=needs_conversion,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            native_language=native_lang,
            language_source=language_source,
            has_removable_tracks=has_removable,
            has_removable_subs=has_removable_subs,
            has_external_subs=has_external_subs,
            estimated_savings_bytes=savings_bytes,
            estimated_savings_gb=savings_gb,
            ignored=str(file_path) in ignored_paths,
            file_mtime=file_mtime,
            duration=duration,
            probe_status="ok",
            video_height=probe.get("video_height", 0),
        )
        if result_callback:
            await result_callback(scanned)
        else:
            results.append(scanned)

    if progress_callback:
        await progress_callback(
            status="done",
            current_file="",
            files_found=total,
            files_probed=total,
            total_files=total,
        )

    return results
