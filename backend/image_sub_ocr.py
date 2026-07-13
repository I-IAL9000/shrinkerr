"""Image-subtitle (PGS/VobSub) language detection via OCR (v0.9.0).

Validated pipeline (NUC spike): extract the image-sub track to a `.sup`
with mkvextract, OCR it to text with pgsrip (tesseract under the hood),
feed the text to the existing detect_subtitle_language -> confidence
gate -> ISO 639-2.

Multi-script without a separate OSD step: OCR first with `eng` (reads
all Latin-script languages cleanly, the common case); if langdetect
can't identify the result (typically because the sub is non-Latin and
the Latin model produced garbage), re-OCR with the CJK/Cyrillic/Arabic
tesseract packs and try again. langdetect then names the language.

On-demand only. Fail-open: any failure returns (None, 0.0)."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

# babelfish languages passed to pgsrip; it maps them to tesseract packs.
# Pass 1 is Latin (eng) — clean + fast for the common case. Pass 2 covers
# the non-Latin scripts we installed packs for.
_LATIN_LANGS = ("eng",)
_NON_LATIN_LANGS = ("zho", "jpn", "kor", "rus", "ara")


def _build_mkvextract_cmd(file_path: str, stream_index: int, out_sup: str) -> list[str]:
    """mkvextract command to pull an image-sub track to a .sup file."""
    return ["mkvextract", "tracks", file_path, f"{stream_index}:{out_sup}"]


def _strip_srt(text: str, max_chars: int = 8000) -> str | None:
    """Strip srt sequence numbers + timestamp lines, leaving dialogue for
    langdetect. Returns None if nothing usable remains."""
    import re as _re
    if not text:
        return None
    text = _re.sub(r"^\d+\s*$", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"\d{2}:\d{2}:\d{2},\d{3} --> .*$", "", text, flags=_re.MULTILINE)
    return text[:max_chars].strip() or None


async def _extract_sup(file_path: str, stream_index: int, workdir: str) -> str | None:
    """mkvextract the image-sub track to a .sup in `workdir`. Returns the
    path, or None on failure/empty."""
    sup = os.path.join(workdir, "sub.sup")
    cmd = _build_mkvextract_cmd(file_path, stream_index, sup)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=300)
    except (asyncio.TimeoutError, OSError):
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return None
    if proc.returncode != 0 or not os.path.exists(sup) or os.path.getsize(sup) == 0:
        return None
    return sup


def _pgsrip_to_text(sup_path: str, tess_langs: tuple[str, ...]) -> str | None:
    """Run pgsrip on a .sup with the given tesseract language(s); read the
    produced .srt and return its dialogue text. Sync (pgsrip is blocking);
    the caller runs it in an executor. Returns None on failure/empty."""
    try:
        from pgsrip import pgsrip, Sup, Options
        from babelfish import Language
        media = Sup(sup_path)
        langs = {Language(code) for code in tess_langs}
        pgsrip.rip(media, Options(languages=langs, overwrite=True))
    except Exception as exc:
        print(f"[IMG-OCR] pgsrip failed ({','.join(tess_langs)}): {exc}", flush=True)
        return None
    srt = os.path.splitext(sup_path)[0] + ".srt"
    if not os.path.exists(srt):
        return None
    try:
        with open(srt, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    finally:
        try:
            os.unlink(srt)
        except OSError:
            pass
    # OCR output should be UTF-8; decode tolerantly just in case.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    return _strip_srt(text)


async def detect_image_sub_language(
    file_path: str, stream_index: int, codec: str, progress_cb=None,
) -> tuple[str | None, float]:
    """OCR a PGS/VobSub track and detect its language. Returns
    (ISO 639-2 B-form, confidence) or (None, 0.0). Fail-open.

    `progress_cb` (v0.9.1): optional async callable(stage: str) invoked at
    coarse stages so the UI can show live status through the multi-minute
    OCR. Called with 'Extracting subtitle…', 'OCR (Latin)…',
    'OCR (CJK/Cyrillic/Arabic)…'."""
    from backend.language_detection import detect_subtitle_language

    async def _report(stage: str):
        if progress_cb is not None:
            try:
                await progress_cb(stage)
            except Exception:
                pass

    workdir = tempfile.mkdtemp(prefix="shrinkerr_imgocr_")
    try:
        await _report(f"Extracting subtitle track {stream_index}…")
        sup = await _extract_sup(file_path, stream_index, workdir)
        if not sup:
            return (None, 0.0)
        loop = asyncio.get_event_loop()
        # Pass 1: Latin (eng).
        await _report(f"OCR (Latin) on subtitle track {stream_index}…")
        text = await loop.run_in_executor(None, _pgsrip_to_text, sup, _LATIN_LANGS)
        if text:
            lang, conf = detect_subtitle_language(text)
            if lang:
                return (lang, conf)
        # Pass 2: non-Latin scripts.
        await _report(f"OCR (CJK/Cyrillic/Arabic) on subtitle track {stream_index}…")
        text = await loop.run_in_executor(None, _pgsrip_to_text, sup, _NON_LATIN_LANGS)
        if text:
            return detect_subtitle_language(text)
        return (None, 0.0)
    except Exception as exc:
        print(f"[IMG-OCR] detection failed for {file_path} s{stream_index}: {exc}", flush=True)
        return (None, 0.0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
