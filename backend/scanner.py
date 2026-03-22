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
    except Exception:
        return None

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_codec = ""
    audio_tracks = []

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not video_codec:
            video_codec = stream.get("codec_name", "")
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
                "channels": stream.get("channels", 2),
                "title": tags.get("title", ""),
                "bitrate": bitrate,
                "disposition": disposition,
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
        "audio_tracks": audio_tracks,
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


def classify_audio_tracks(
    tracks: list[dict], native_language: str
) -> list[AudioTrack]:
    """
    Classify audio tracks for keep/remove.

    Rules:
    - Always keep (locked=True): languages in settings.always_keep_languages
    - Keep native language (keep=True, locked=False unless native is in always_keep)
    - Ignore (keep=True, locked=False) und/unknown tracks
    - Everything else: keep=False
    """
    always_keep = {lang.lower() for lang in settings.always_keep_languages}
    native = native_language.lower() if native_language else "und"

    result = []
    for track in tracks:
        lang = (track.get("language") or "und").lower()
        bitrate = track.get("bitrate")
        size_estimate = None
        if bitrate:
            try:
                size_estimate = int(bitrate)
            except (ValueError, TypeError):
                size_estimate = None

        if lang in always_keep:
            keep = True
            locked = True
        elif lang == native:
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
                size_estimate_bytes=size_estimate,
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
) -> list[ScannedFile]:
    """
    Walk dir_path, probe video files, classify tracks, build ScannedFile list.

    Skips files that are already x265 with no removable tracks.
    """
    dir_path = Path(dir_path)
    extensions = {ext.lower() for ext in settings.video_extensions}

    # Collect all candidate files first
    all_files = []
    for root, _dirs, files in os.walk(dir_path):
        for name in files:
            if Path(name).suffix.lower() in extensions:
                all_files.append(Path(root) / name)

    total = len(all_files)
    results: list[ScannedFile] = []

    for idx, file_path in enumerate(all_files):
        if progress_callback:
            await progress_callback(
                status="scanning",
                current_file=str(file_path),
                files_found=total,
                files_probed=idx,
                total_files=total,
            )

        probe = await probe_file(str(file_path))
        if probe is None:
            continue

        video_codec = probe["video_codec"]
        raw_tracks = probe["audio_tracks"]
        duration = probe["duration"]
        file_size = probe["file_size"]

        native_lang = detect_native_language(raw_tracks)
        needs_conversion = is_x264(video_codec)
        audio_tracks = classify_audio_tracks(raw_tracks, native_lang)

        tracks_to_remove = [t for t in audio_tracks if not t.keep]
        has_removable = len(tracks_to_remove) > 0

        # Skip files already in x265 with nothing to do
        if is_x265(video_codec) and not has_removable:
            continue

        savings_bytes = estimate_savings(file_size, needs_conversion, tracks_to_remove, duration)
        savings_gb = round(savings_bytes / (1024 ** 3), 3)

        scanned = ScannedFile(
            file_path=str(file_path),
            file_name=file_path.name,
            folder_name=file_path.parent.name,
            file_size=file_size,
            file_size_gb=round(file_size / (1024 ** 3), 3),
            video_codec=video_codec,
            needs_conversion=needs_conversion,
            audio_tracks=audio_tracks,
            native_language=native_lang,
            has_removable_tracks=has_removable,
            estimated_savings_bytes=savings_bytes,
            estimated_savings_gb=savings_gb,
        )
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
