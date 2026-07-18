"""Language detection for tracks Shrinkerr can't tag from container/IFO/
mpls/libbluray metadata (they come through as `und`).

detect_subtitle_language(text) — text subs, via langdetect (cheap).
Returns (ISO 639-2 B-form code, confidence 0-1) or (None, 0.0) when
detection fails or confidence is below threshold. Fail-open. v0.8.0+.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile

from backend.disc_metadata import _ISO639_1_TO_2, _normalize_iso639_2


def _get_setting(key: str) -> str | None:
    """Read one settings-table value (sync, uncached — detection is on-demand,
    not hot). Returns None on unset/error."""
    try:
        import sqlite3
        from backend.database import DB_PATH
        con = sqlite3.connect(DB_PATH)
        try:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row and row[0] not in (None, "") else None
        finally:
            con.close()
    except Exception:
        return None


def _tuned_float(env_key: str, setting_key: str, default: float) -> float:
    """Effective value: env var (advanced override) → Settings-UI value →
    built-in default."""
    raw = os.environ.get(env_key)
    if raw is None:
        raw = _get_setting(setting_key)
    try:
        return float(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


def _sub_min_confidence() -> float:
    return _tuned_float("SHRINKERR_LANG_DETECT_SUB_MIN", "lang_detect_sub_min", 0.7)


# v0.9.10: language NAME (as authored in a track title) → ISO 639-2/B.
# Forced/SDH subs frequently carry too little text to detect from content
# but name the language in the title ("Traditional Chinese", "Romanian"),
# which is a high-confidence signal. Base names only — modifiers like
# "Traditional"/"Simplified"/"Brazilian" collapse to the same ISO code.
_TITLE_LANG_MAP: dict[str, str] = {
    "english": "eng",
    "chinese": "chi", "mandarin": "chi", "cantonese": "chi",
    "portuguese": "por", "brazilian": "por",
    "spanish": "spa", "castilian": "spa", "espanol": "spa",
    "french": "fre", "francais": "fre",
    "german": "ger", "deutsch": "ger",
    "italian": "ita",
    "russian": "rus",
    "japanese": "jpn",
    "korean": "kor",
    "arabic": "ara",
    "dutch": "dut",
    "swedish": "swe",
    "norwegian": "nor",
    "danish": "dan",
    "finnish": "fin",
    "polish": "pol",
    "czech": "cze",
    "slovak": "slo",
    "slovenian": "slv", "slovene": "slv",
    "hungarian": "hun",
    "romanian": "rum",
    "bulgarian": "bul",
    "greek": "gre",
    "turkish": "tur",
    "hebrew": "heb",
    "thai": "tha",
    "vietnamese": "vie",
    "indonesian": "ind",
    "malay": "may",
    "icelandic": "ice",
    "croatian": "hrv",
    "serbian": "srp",
    "ukrainian": "ukr",
    "hindi": "hin",
    "bengali": "ben",
    "tamil": "tam",
    "telugu": "tel",
    "catalan": "cat",
    "estonian": "est",
    "latvian": "lav",
    "lithuanian": "lit",
    "persian": "per", "farsi": "per",
    "filipino": "tgl", "tagalog": "tgl",
    # v0.9.15: broader coverage — titles name plenty of languages beyond the
    # common European set (observed: an audio track titled "Wolof" that
    # whisper-tiny can't ID, left und even though the title said it outright).
    "wolof": "wol", "swahili": "swa", "amharic": "amh", "somali": "som",
    "yoruba": "yor", "hausa": "hau", "igbo": "ibo", "zulu": "zul",
    "xhosa": "xho", "afrikaans": "afr", "malagasy": "mlg",
    "malayalam": "mal", "kannada": "kan", "marathi": "mar", "punjabi": "pan",
    "gujarati": "guj", "urdu": "urd", "nepali": "nep", "sinhala": "sin",
    "sinhalese": "sin", "tamil": "tam", "telugu": "tel", "bengali": "ben",
    "khmer": "khm", "lao": "lao", "burmese": "bur", "mongolian": "mon",
    "georgian": "geo", "armenian": "arm", "albanian": "alb",
    "macedonian": "mac", "belarusian": "bel", "kazakh": "kaz",
    "uzbek": "uzb", "azerbaijani": "aze", "basque": "baq", "welsh": "wel",
    "irish": "gle", "galician": "glg", "latin": "lat", "maltese": "mlt",
    "luxembourgish": "ltz", "tibetan": "tib", "sanskrit": "san",
    "pashto": "pus", "sindhi": "snd", "maori": "mao", "hawaiian": "haw",
}
_TITLE_LANG_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _TITLE_LANG_MAP) + r")\b"
)


def detect_language_from_title(title: str | None) -> str | None:
    """Map a language named in a track title ('Traditional Chinese',
    'Romanian') to ISO 639-2/B. A reliable signal for forced/SDH tracks that
    carry too little text to detect from content. Returns None when the title
    names no known language."""
    if not title:
        return None
    import unicodedata
    # Strip diacritics so native names ("français") match ASCII keys.
    flat = "".join(
        c for c in unicodedata.normalize("NFKD", title.lower())
        if not unicodedata.combining(c)
    )
    m = _TITLE_LANG_RE.search(flat)
    if not m:
        return None
    return _normalize_iso639_2(_TITLE_LANG_MAP[m.group(1)])


def _iso_to_iso639_2b(code: str | None) -> str | None:
    """ISO 639-1 (or a whisper 3-letter code) → ISO 639-2/B. Strips a region
    suffix (zh-cn → zh). Curated map first; babelfish covers the long tail
    when present (it ships with pgsrip). None if unmappable."""
    if not code:
        return None
    code = code.split("-")[0].lower()
    mapped = _ISO639_1_TO_2.get(code)
    if mapped:
        return _normalize_iso639_2(mapped)
    try:
        from babelfish import Language
        lang = Language.fromalpha2(code) if len(code) == 2 else Language(code)
        b = getattr(lang, "alpha3b", None) or lang.alpha3
        return _normalize_iso639_2(b) if b else None
    except Exception:
        return None


def detect_subtitle_language(text: str) -> tuple[str | None, float]:
    """Detect language of subtitle text. langdetect yields ISO 639-1 +
    probability; map to 639-2 B-form to match the codebase."""
    if not text or not text.strip():
        return (None, 0.0)
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        results = detect_langs(text)
    except Exception as exc:
        print(f"[LANG-DETECT] subtitle: langdetect error on {len(text)} chars: {exc}", flush=True)
        return (None, 0.0)
    if not results:
        print(f"[LANG-DETECT] subtitle: langdetect returned nothing for {len(text)} chars", flush=True)
        return (None, 0.0)
    best = results[0]
    iso1 = best.lang.lower()
    conf = float(best.prob)
    _min = _sub_min_confidence()
    if conf < _min:
        print(f"[LANG-DETECT] subtitle: below confidence {iso1}@{conf:.2f} < {_min:.2f} ({len(text)} chars)", flush=True)
        return (None, 0.0)
    # v0.8.2: langdetect returns regional codes for some languages
    # (e.g. "zh-cn"/"zh-tw" for Chinese). Strip the region before the
    # 639-1→639-2 lookup — both Chinese variants map to "chi" (ISO 639-2
    # has no simplified/traditional distinction).
    iso2 = _iso_to_iso639_2b(iso1)
    if not iso2:
        print(f"[LANG-DETECT] subtitle: {iso1} has no ISO-639-2 mapping", flush=True)
        return (None, 0.0)
    return (iso2, conf)


_WHISPER_MODEL = None
_WHISPER_MODEL_NAME = None
_WHISPER_LOAD_FAILED = False


def _configured_whisper_model() -> str:
    """Which faster-whisper model to use: env var → Settings-UI value → tiny."""
    return (os.environ.get("SHRINKERR_WHISPER_MODEL")
            or _get_setting("lang_detect_whisper_model")
            or "tiny")


def _audio_min_confidence() -> float:
    return _tuned_float("SHRINKERR_LANG_DETECT_AUDIO_MIN", "lang_detect_audio_min", 0.6)


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


def _sample_seeks(duration: float) -> list[float]:
    """v0.8.4: positions (seconds) to sample for language ID. A single
    30s window can land on music/action/silence and yield a low-
    confidence guess (observed: a 5.1 track returning pt@0.40 because
    the 33% window had little clear speech). Try a few spread-out
    positions; detect_audio_language stops at the first confident one,
    so clear tracks still cost a single sample. Short files → just 0."""
    if not duration or duration <= 90:
        return [0.0]
    return [duration * f for f in (0.33, 0.55, 0.15, 0.72)]


async def _extract_audio_clip(input_path: str, stream_index: int, seek: float) -> str:
    """Extract a 30s clip starting at `seek` seconds to a temp WAV.
    Returns the temp path. Raises on ffmpeg failure (caller fail-opens)."""
    fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="shrinkerr_lang_")
    os.close(fd)
    cmd = _build_audio_clip_cmd(input_path, stream_index, seek, out_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        # v0.9.19: kill the ffmpeg process on timeout — it was orphaned before,
        # left running and burning CPU while detection moved on.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise OSError("ffmpeg clip extract timed out (killed)")
    if proc.returncode != 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise OSError(f"ffmpeg clip extract failed: {stderr.decode(errors='replace')[-300:]}")
    return out_path


def _get_whisper_model():
    """Load the configured model (cached), reloading if the setting changed.
    Returns None if load/download fails (offline first-use) — caller
    fail-opens. `tiny` is under-confident on non-English speech; `base`/`small`
    push it over the gate at the cost of size/speed (Settings → Video)."""
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME, _WHISPER_LOAD_FAILED
    want = _configured_whisper_model()
    # Live reload: if the user changed the model in Settings, drop the cached
    # one and load the new model without a restart.
    if _WHISPER_MODEL_NAME == want:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        if _WHISPER_LOAD_FAILED:
            return None
    try:
        from faster_whisper import WhisperModel
        cache_dir = os.environ.get("SHRINKERR_WHISPER_CACHE", "/app/data/models")
        os.makedirs(cache_dir, exist_ok=True)
        # v0.9.19: cap CPU threads. CTranslate2 defaults to ALL cores, so one
        # detection that wedges (observed on a specific DTS track) pegs every
        # core and starves the event loop → the whole app goes unresponsive.
        # Leave a couple of cores for uvicorn. Ignored when running on GPU.
        _cpu_threads = max(1, (os.cpu_count() or 4) - 2)
        _WHISPER_MODEL = WhisperModel(want, device="auto", compute_type="int8",
                                      download_root=cache_dir, cpu_threads=_cpu_threads)
        _WHISPER_MODEL_NAME = want
        _WHISPER_LOAD_FAILED = False
        print(f"[LANG-DETECT] Whisper model '{want}' loaded", flush=True)
        return _WHISPER_MODEL
    except Exception as exc:
        print(f"[LANG-DETECT] Whisper model '{want}' load failed, audio detection disabled: {exc}", flush=True)
        _WHISPER_MODEL = None
        _WHISPER_MODEL_NAME = want
        _WHISPER_LOAD_FAILED = True
        return None


def _run_whisper_lang(clip_path: str) -> tuple[str | None, float]:
    """Run faster-whisper language ID on a clip. Returns (ISO 639-1, prob)
    or (None, 0.0). Runs inside the killable worker subprocess (see
    audio_lang_worker) — NOT on the event-loop process."""
    model = _get_whisper_model()
    if model is None:
        return (None, 0.0)
    _segments, info = model.transcribe(clip_path, language=None)
    return (getattr(info, "language", None), float(getattr(info, "language_probability", 0.0)))


async def _detect_clip_language(clip_path: str, timeout: int = 120) -> tuple[str | None, float]:
    """Language-ID a clip in a KILLABLE subprocess. v0.9.21: a wedged whisper
    transcribe used to leak an un-cancellable executor thread that pegged every
    core and starved the event loop (the whole app went unresponsive). Running
    it as a subprocess means a stuck one is KILLED on timeout, freeing the CPU.
    Raises asyncio.TimeoutError on timeout (caller abandons the track);
    otherwise returns (ISO 639-1 | None, confidence). Fail-open."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "backend.audio_lang_worker", clip_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        print(f"[LANG-DETECT] whisper subprocess killed after {timeout}s: {clip_path}", flush=True)
        raise
    if proc.returncode != 0 or not out:
        return (None, 0.0)
    # The worker prints a `RESULT\t<lang>\t<conf>` line; ignore any model-load
    # chatter on stdout by scanning for that marker from the end.
    for line in reversed(out.decode(errors="replace").splitlines()):
        if line.startswith("RESULT\t"):
            parts = line.split("\t")
            lang = parts[1] if len(parts) > 1 else ""
            try:
                conf = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
            except ValueError:
                conf = 0.0
            return (lang or None, conf)
    return (None, 0.0)


async def detect_audio_language(file_path: str, stream_index: int, duration: float = 0.0) -> tuple[str | None, float, str | None]:
    """Detect an audio track's spoken language via faster-whisper on 30s
    clips. Returns (ISO 639-2 B-form, confidence, note). On success note is
    None; on failure lang is None and note explains why (v0.9.44). Fail-open.

    v0.8.4: samples multiple positions and takes the most confident,
    short-circuiting as soon as one clears the threshold — so a track
    whose 33% window is weak-speech (music/action) still gets detected
    from a better window, while clear tracks stop after one sample."""
    threshold = _audio_min_confidence()
    best_iso1: str | None = None
    best_conf = 0.0
    timed_out = False
    for seek in _sample_seeks(duration):
        clip_path = None
        try:
            clip_path = await _extract_audio_clip(file_path, stream_index, seek)
            # v0.9.21: killable subprocess. A wedged transcribe is KILLED on
            # timeout (frees CPU) rather than leaking a thread that starves the
            # event loop and takes the whole app down.
            iso1, conf = await _detect_clip_language(clip_path, timeout=120)
        except asyncio.TimeoutError:
            # Don't try the remaining windows — they'd wedge too and pile up
            # leaked threads. Abandon this track (stays und).
            print(f"[LANG-DETECT] audio s{stream_index}: whisper timed out @{seek:.0f}s — abandoning track", flush=True)
            iso1, conf, timed_out = None, 0.0, True
        except Exception as exc:
            print(f"[LANG-DETECT] audio detection failed for {file_path} s{stream_index} @{seek:.0f}s: {exc}", flush=True)
            iso1, conf = None, 0.0
        finally:
            if clip_path and os.path.exists(clip_path):
                try:
                    os.unlink(clip_path)
                except OSError:
                    pass
        if timed_out:
            break
        if iso1 and conf > best_conf:
            best_iso1, best_conf = iso1, conf
        if best_conf >= threshold:
            break  # confident enough — don't sample further
    if timed_out:
        return (None, 0.0, "audio detection timed out")
    if not best_iso1 or best_conf < threshold:
        print(f"[LANG-DETECT] audio s{stream_index}: below confidence "
              f"{best_iso1 or 'no-speech'}@{best_conf:.2f} < {threshold:.2f}", flush=True)
        note = (f"{best_iso1} {round(best_conf * 100)}% — below {round(threshold * 100)}% threshold"
                if best_iso1 else "no speech detected")
        return (None, 0.0, note)
    iso2 = _iso_to_iso639_2b(best_iso1)
    if not iso2:
        print(f"[LANG-DETECT] audio s{stream_index}: {best_iso1} has no ISO-639-2 mapping", flush=True)
        return (None, 0.0, f'detected "{best_iso1}" — no ISO-639-2 code')
    return (iso2, best_conf, None)


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


# Containers we can't reliably tag in place, so the only fix is remuxing to
# mkv. Two reasons: (1) no per-track language field — ffmpeg `-c copy` copies
# the streams fine but silently drops `language=` (.avi/.mpg/.wmv/...); and
# (2) .m4v/.m4a, which ffmpeg muxes with the strict `ipod` muxer that aborts
# on streams it can't write ("Tag text incompatible with output codec id")
# even though the in-place write itself never succeeds. Both route to the
# stream-copy remux-to-mkv, which uses the permissive matroska muxer. v0.9.54.
_UNTAGGABLE_CONTAINERS = {".avi", ".mpg", ".mpeg", ".flv", ".wmv", ".asf",
                          ".vob", ".m4v", ".m4a"}


def _build_mkvpropedit_cmd(
    file_path: str,
    audio_langs: list[str | None],
    sub_langs: list[str | None],
) -> list[str] | None:
    """Build an mkvpropedit command that sets languages on the given tracks,
    or None if there's nothing to set.

    `audio_langs[i]` is the ISO 639-2 code for the (i+1)-th audio track
    (None = leave alone); `sub_langs[j]` likewise for subtitle tracks.
    mkvpropedit selectors are 1-based per type: `track:a1`, `track:s2`.

    v0.9.48: each edit also deletes the track's `language-ietf` (BCP-47)
    element. When present it takes precedence over the legacy `language`, so
    setting only `language` left players/ffprobe reading the old value
    (observed: mkvpropedit reports success but the tag verify still sees und).
    Deleting it makes the legacy `language` we just set authoritative."""
    edits: list[str] = []
    for i, code in enumerate(audio_langs):
        if code:
            edits += ["--edit", f"track:a{i + 1}", "--set", f"language={code}", "--delete", "language-ietf"]
    for j, code in enumerate(sub_langs):
        if code:
            edits += ["--edit", f"track:s{j + 1}", "--set", f"language={code}", "--delete", "language-ietf"]
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


async def _probe_track_languages(file_path: str) -> tuple[list[str], list[str]]:
    """ffprobe the per-type-ordered language tags of a file's audio and
    subtitle streams. Returns (audio_langs, sub_langs) lowercased, "und" where
    absent. Empty lists signal a probe failure (caller should not treat that
    as a verified result)."""
    import asyncio
    import json as _json
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_type:stream_tags=language", "-of", "json", file_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        streams = _json.loads(out.decode(errors="replace")).get("streams", [])
    except Exception:
        return [], []
    a, s = [], []
    for st in streams:
        lang = ((st.get("tags") or {}).get("language") or "und").lower()
        if st.get("codec_type") == "audio":
            a.append(lang)
        elif st.get("codec_type") == "subtitle":
            s.append(lang)
    return a, s


def _languages_present(probed: list[str], intended: list[str | None]) -> bool:
    """True if every intended (non-None) language now shows a real (non-und)
    tag on the corresponding per-type stream ordinal."""
    for i, code in enumerate(intended):
        if code and (i >= len(probed) or probed[i] in ("", "und")):
            return False
    return True


async def _verify_written(file_path: str, audio_langs, sub_langs) -> bool | None:
    """Confirm the intended tags are actually present in `file_path`.
    True = verified present, False = ran but tags absent (container can't
    store them, e.g. AVI), None = couldn't probe (caller falls back to the
    process exit code)."""
    pa, ps = await _probe_track_languages(file_path)
    if not pa and not ps:
        return None
    return _languages_present(pa, audio_langs) and _languages_present(ps, sub_langs)


async def apply_track_languages_to_file(
    file_path: str,
    audio_langs: list[str | None],
    sub_langs: list[str | None],
) -> bool:
    """Write ISO 639-2 language tags onto the file's audio/subtitle tracks
    without re-encoding. mkv → mkvpropedit (in place); other → ffmpeg
    `-c copy` remux + atomic replace. Returns True only when the tags are
    verified present in the file afterward — a container that silently drops
    per-track language (AVI) returns False so the caller keeps the track
    flagged. Fail-open: never raises, never leaves the original in a worse
    state."""
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
            verified = await _verify_written(file_path, audio_langs, sub_langs)
            if verified is not False:  # True (present) or None (couldn't probe)
                print(f"[LANG-DETECT] Wrote language tags in place: {file_path}", flush=True)
                return True
            # v0.9.49: mkvpropedit set the track-header Language, but a track
            # `LANGUAGE` SimpleTag overrides it for ffmpeg/Plex (the header edit
            # doesn't show). Fall through to the ffmpeg -c copy remux below,
            # whose -metadata:s:a:N language collapses both into one key and
            # rewrites it — overriding the SimpleTag.
            print(f"[LANG-DETECT] mkvpropedit didn't stick (LANGUAGE SimpleTag override?); "
                  f"trying remux: {file_path}", flush=True)
        else:
            print(
                f"[LANG-DETECT] mkvpropedit rc={proc.returncode} for {file_path}: "
                f"{stderr.decode(errors='replace')[-300:]}",
                flush=True,
            )
            return False

    # v0.9.26: containers with no per-track language field copy fine but drop
    # the tag, so an in-place fix is impossible — skip the (multi-GB) remux
    # entirely and signal that conversion to mkv is the only real fix.
    if os.path.splitext(file_path)[1].lower() in _UNTAGGABLE_CONTAINERS:
        print(f"[LANG-DETECT] {os.path.splitext(file_path)[1].lower()} can't store "
              f"per-track language; convert to mkv to fix: {file_path}", flush=True)
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
        # Verify the tags survived the remux BEFORE replacing the original — a
        # container with no per-track language field (AVI) copies fine but
        # drops the metadata, so ffmpeg exits 0 with nothing written.
        if await _verify_written(tmp, audio_langs, sub_langs) is False:
            print(f"[LANG-DETECT] container can't store per-track language; kept und: {file_path}", flush=True)
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
