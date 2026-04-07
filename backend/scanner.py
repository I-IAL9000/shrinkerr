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
    audio_tracks = []
    subtitle_tracks = []

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not video_codec:
            video_codec = stream.get("codec_name", "")
            video_width = stream.get("width", 0) or 0
            video_height = stream.get("height", 0) or 0
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

    return {
        "video_codec": video_codec,
        "video_width": video_width,
        "video_height": video_height,
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

    # Detect duplicate x264/x265 pairs — if both exist, skip the x264
    # (happens when conversion was interrupted after creating x265 but before deleting x264)
    from backend.converter import rename_x264_to_x265
    all_paths_set = {str(f) for f in all_files}
    skip_paths: set[str] = set()
    for f in all_files:
        name = f.name
        renamed = rename_x264_to_x265(name)
        if renamed != name:
            # This is an x264 file — check if the x265 version exists
            x265_path = str(f.parent / renamed.replace(f.suffix, ".mkv") if f.suffix.lower() != ".mkv" else f.parent / renamed)
            if x265_path in all_paths_set and str(f) != x265_path:
                skip_paths.add(str(f))
                print(f"[SCANNER] Skipping duplicate x264 (x265 version exists): {f.name}", flush=True)

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
            # Store corrupt file entry so it appears in the Corrupt filter
            try:
                file_size_corrupt = os.path.getsize(str(file_path))
                file_mtime_corrupt = os.path.getmtime(str(file_path))
            except OSError:
                file_size_corrupt = 0
                file_mtime_corrupt = None
            corrupt_entry = ScannedFile(
                file_path=str(file_path),
                file_name=file_path.name,
                folder_name=file_path.parent.name,
                file_size=file_size_corrupt,
                file_size_gb=round(file_size_corrupt / (1024 ** 3), 3),
                video_codec="unknown",
                needs_conversion=False,
                audio_tracks=[],
                subtitle_tracks=[],
                native_language="und",
                has_removable_tracks=False,
                has_removable_subs=False,
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

        # Try API-based language detection first (skip if cancelled to allow fast exit)
        api_lang = None
        if not (cancel_check and cancel_check()):
            try:
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

        tracks_to_remove = [t for t in audio_tracks if not t.keep]
        has_removable = len(tracks_to_remove) > 0
        has_removable_subs = any(not t.keep for t in subtitle_tracks)

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
