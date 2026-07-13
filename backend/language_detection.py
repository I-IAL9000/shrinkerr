"""Language detection for tracks Shrinkerr can't tag from container/IFO/
mpls/libbluray metadata (they come through as `und`).

detect_subtitle_language(text) — text subs, via langdetect (cheap).
Returns (ISO 639-2 B-form code, confidence 0-1) or (None, 0.0) when
detection fails or confidence is below threshold. Fail-open. v0.8.0+.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from backend.disc_metadata import _ISO639_1_TO_2, _normalize_iso639_2


def _sub_min_confidence() -> float:
    try:
        return float(os.environ.get("SHRINKERR_LANG_DETECT_SUB_MIN", "0.7"))
    except ValueError:
        return 0.7


def detect_subtitle_language(text: str) -> tuple[str | None, float]:
    """Detect language of subtitle text. langdetect yields ISO 639-1 +
    probability; map to 639-2 B-form to match the codebase."""
    if not text or not text.strip():
        return (None, 0.0)
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        results = detect_langs(text)
    except Exception:
        return (None, 0.0)
    if not results:
        return (None, 0.0)
    best = results[0]
    iso1 = best.lang.lower()
    conf = float(best.prob)
    if conf < _sub_min_confidence():
        return (None, 0.0)
    # v0.8.2: langdetect returns regional codes for some languages
    # (e.g. "zh-cn"/"zh-tw" for Chinese). Strip the region before the
    # 639-1→639-2 lookup — both Chinese variants map to "chi" (ISO 639-2
    # has no simplified/traditional distinction).
    iso1_base = iso1.split("-")[0]
    iso2 = _ISO639_1_TO_2.get(iso1_base, "")
    if not iso2:
        return (None, 0.0)
    return (_normalize_iso639_2(iso2), conf)


_WHISPER_MODEL = None
_WHISPER_LOAD_FAILED = False


def _audio_min_confidence() -> float:
    try:
        return float(os.environ.get("SHRINKERR_LANG_DETECT_AUDIO_MIN", "0.6"))
    except ValueError:
        return 0.6


def _build_audio_clip_cmd(input_path: str, stream_index: int, seek: float, out_path: str) -> list[str]:
    """ffmpeg command: 30s mono 16 kHz PCM WAV from `stream_index`, seeking
    to `seek` seconds. 16 kHz mono is what Whisper expects."""
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(seek),
        "-i", input_path,
        "-map", f"0:{stream_index}",
        "-t", "30",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_path,
    ]


async def _extract_audio_clip(input_path: str, stream_index: int, duration: float) -> str:
    """Extract a 30s clip from ~33% into the file to a temp WAV. Returns the
    temp path. Raises on ffmpeg failure (caller fail-opens)."""
    seek = max(0.0, duration * 0.33) if duration and duration > 90 else 0.0
    fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="shrinkerr_lang_")
    os.close(fd)
    cmd = _build_audio_clip_cmd(input_path, stream_index, seek, out_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise OSError(f"ffmpeg clip extract failed: {stderr.decode(errors='replace')[-300:]}")
    return out_path


def _get_whisper_model():
    """Load the tiny model once. Returns None if load/download fails
    (offline first-use) — caller fail-opens."""
    global _WHISPER_MODEL, _WHISPER_LOAD_FAILED
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    if _WHISPER_LOAD_FAILED:
        return None
    try:
        from faster_whisper import WhisperModel
        cache_dir = os.environ.get("SHRINKERR_WHISPER_CACHE", "/app/data/models")
        os.makedirs(cache_dir, exist_ok=True)
        _WHISPER_MODEL = WhisperModel("tiny", device="auto", compute_type="int8", download_root=cache_dir)
        return _WHISPER_MODEL
    except Exception as exc:
        print(f"[LANG-DETECT] Whisper model load failed, audio detection disabled: {exc}", flush=True)
        _WHISPER_LOAD_FAILED = True
        return None


def _run_whisper_lang(clip_path: str) -> tuple[str | None, float]:
    """Run faster-whisper language ID on a clip. Returns (ISO 639-1, prob)
    or (None, 0.0). Isolated so tests mock this single seam."""
    model = _get_whisper_model()
    if model is None:
        return (None, 0.0)
    _segments, info = model.transcribe(clip_path, language=None)
    return (getattr(info, "language", None), float(getattr(info, "language_probability", 0.0)))


async def detect_audio_language(file_path: str, stream_index: int, duration: float = 0.0) -> tuple[str | None, float]:
    """Detect an audio track's spoken language via a 30s clip + faster-whisper.
    Returns (ISO 639-2 B-form, confidence) or (None, 0.0). Fail-open."""
    clip_path = None
    try:
        clip_path = await _extract_audio_clip(file_path, stream_index, duration)
        iso1, conf = await asyncio.get_event_loop().run_in_executor(None, _run_whisper_lang, clip_path)
    except Exception as exc:
        print(f"[LANG-DETECT] audio detection failed for {file_path} s{stream_index}: {exc}", flush=True)
        return (None, 0.0)
    finally:
        if clip_path and os.path.exists(clip_path):
            try:
                os.unlink(clip_path)
            except OSError:
                pass
    if not iso1 or conf < _audio_min_confidence():
        return (None, 0.0)
    iso2 = _ISO639_1_TO_2.get(iso1.lower(), "")
    if not iso2:
        return (None, 0.0)
    return (_normalize_iso639_2(iso2), conf)


# Text subtitle codecs whose language we can detect from their text.
# Image codecs (hdmv_pgs_subtitle, dvd_subtitle, dvb_subtitle, vobsub) are
# out of scope for v0.8.0 (OCR is v0.8.1).
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "tx3g"}


def maybe_detect_subtitle_track_language(language: str, codec: str, text: str | None) -> str:
    """Return an upgraded ISO 639-2 language for a subtitle track, or the
    original `language` unchanged. Only detects for und TEXT subs with
    extractable text. Image subs and already-tagged tracks pass through."""
    if (language or "und").lower() != "und":
        return language
    if (codec or "").lower() not in _TEXT_SUB_CODECS:
        return language
    if not text:
        return language
    detected, _conf = detect_subtitle_language(text)
    return detected if detected else "und"


# ── Writing detected languages back to the file (v0.8.3) ────────────────────
#
# Detection updates Shrinkerr's DB, but the FILE still carries `und` tracks
# until it's converted. To "just fix the unknown tracks" without a re-encode,
# we write the ISO 639-2 language tags into the file:
#   - mkv  → mkvpropedit: edits the header IN PLACE (instant, no rewrite,
#            no disk churn even on huge files).
#   - other → ffmpeg `-c copy` remux to a temp file + atomic replace
#            (rewrites the container but no re-encode).
# Fail-open: a write failure never loses the DB-side detection.


def _build_mkvpropedit_cmd(
    file_path: str,
    audio_langs: list[str | None],
    sub_langs: list[str | None],
) -> list[str] | None:
    """Build an mkvpropedit command that sets languages on the given tracks,
    or None if there's nothing to set.

    `audio_langs[i]` is the ISO 639-2 code for the (i+1)-th audio track
    (None = leave alone); `sub_langs[j]` likewise for subtitle tracks.
    mkvpropedit selectors are 1-based per type: `track:a1`, `track:s2`."""
    edits: list[str] = []
    for i, code in enumerate(audio_langs):
        if code:
            edits += ["--edit", f"track:a{i + 1}", "--set", f"language={code}"]
    for j, code in enumerate(sub_langs):
        if code:
            edits += ["--edit", f"track:s{j + 1}", "--set", f"language={code}"]
    if not edits:
        return None
    return ["mkvpropedit", file_path] + edits


def _build_metadata_remux_cmd(
    file_path: str,
    out_path: str,
    audio_langs: list[str | None],
    sub_langs: list[str | None],
) -> list[str]:
    """Build an ffmpeg `-c copy` command that copies all streams and sets
    audio/subtitle language metadata. Output-stream metadata selectors are
    0-based per type: `-metadata:s:a:0`, `-metadata:s:s:1`."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", file_path, "-map", "0", "-c", "copy"]
    for i, code in enumerate(audio_langs):
        if code:
            cmd += [f"-metadata:s:a:{i}", f"language={code}"]
    for j, code in enumerate(sub_langs):
        if code:
            cmd += [f"-metadata:s:s:{j}", f"language={code}"]
    cmd += [out_path]
    return cmd


async def apply_track_languages_to_file(
    file_path: str,
    audio_langs: list[str | None],
    sub_langs: list[str | None],
) -> bool:
    """Write ISO 639-2 language tags onto the file's audio/subtitle tracks
    without re-encoding. mkv → mkvpropedit (in place); other → ffmpeg
    `-c copy` remux + atomic replace. Returns True if the file was
    updated, False on no-op or failure. Fail-open: never raises, never
    leaves the original in a worse state."""
    import asyncio
    import os
    import tempfile

    if not any(audio_langs) and not any(sub_langs):
        return False

    is_mkv = file_path.lower().endswith(".mkv")

    if is_mkv:
        cmd = _build_mkvpropedit_cmd(file_path, audio_langs, sub_langs)
        if cmd is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (asyncio.TimeoutError, OSError, FileNotFoundError) as exc:
            print(f"[LANG-DETECT] mkvpropedit failed for {file_path}: {exc}", flush=True)
            return False
        # mkvpropedit: 0 = success, 1 = warnings (still applied), 2 = error.
        if proc.returncode in (0, 1):
            print(f"[LANG-DETECT] Wrote language tags in place: {file_path}", flush=True)
            return True
        print(
            f"[LANG-DETECT] mkvpropedit rc={proc.returncode} for {file_path}: "
            f"{stderr.decode(errors='replace')[-300:]}",
            flush=True,
        )
        return False

    # Non-mkv: ffmpeg -c copy remux to a temp file, then atomic replace.
    p_dir = os.path.dirname(file_path) or "."
    fd, tmp = tempfile.mkstemp(
        suffix=os.path.splitext(file_path)[1] or ".mkv",
        prefix=".shrinkerr_lang_", dir=p_dir,
    )
    os.close(fd)
    cmd = _build_metadata_remux_cmd(file_path, tmp, audio_langs, sub_langs)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
        if proc.returncode != 0:
            print(
                f"[LANG-DETECT] metadata remux failed for {file_path}: "
                f"{stderr.decode(errors='replace')[-300:]}",
                flush=True,
            )
            os.unlink(tmp)
            return False
        # Sanity: temp must be a plausible size (container copy ≈ source size).
        if os.path.getsize(tmp) < os.path.getsize(file_path) * 0.5:
            print(f"[LANG-DETECT] metadata remux output suspiciously small; discarding: {file_path}", flush=True)
            os.unlink(tmp)
            return False
        os.replace(tmp, file_path)  # atomic on same filesystem
        print(f"[LANG-DETECT] Wrote language tags via remux: {file_path}", flush=True)
        return True
    except Exception as exc:
        print(f"[LANG-DETECT] metadata remux error for {file_path}: {exc}", flush=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
