"""Image-subtitle (PGS/VobSub) language detection via OCR.

Two pipelines by codec, converging on the same detect_subtitle_language ->
confidence gate -> ISO 639-2 step:
  - PGS (Blu-ray, v0.9.0): mkvextract → `.sup` → pgsrip (tesseract).
  - VobSub (DVD, dvd_subtitle, v0.9.8): mkvextract → `.idx`/`.sub` →
    subtile-ocr (tesseract). pgsrip is PGS-only and can't read VobSub, so
    it gets its own tool. Fail-open if subtile-ocr isn't in the image.

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

# v0.9.8: codec → pipeline. PGS (Blu-ray) goes through pgsrip; VobSub (DVD,
# dvd_subtitle) is a different bitstream pgsrip can't read, so it goes through
# subtile-ocr (mkvextract → .idx/.sub → tesseract). Kept separate because the
# two tools/formats share nothing but the final detect_subtitle_language step.
_PGS_CODECS = ("hdmv_pgs_subtitle", "pgs")
_VOBSUB_CODECS = ("dvd_subtitle", "vobsub")
# subtile-ocr passes -l straight to tesseract, which uses '+' to combine packs
# and the pack names (chi_sim, not the babelfish 'zho' pgsrip wants).
_VOBSUB_LATIN_LANG = "eng"
_VOBSUB_NON_LATIN_LANG = "chi_sim+chi_tra+jpn+kor+rus+ara"


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


async def _extract_vobsub(file_path: str, stream_index: int, workdir: str) -> str | None:
    """mkvextract a VobSub track to <workdir>/sub.idx (+ sub.sub). Returns the
    .idx path, or None on failure/empty. VobSub is a paired format — both
    files must exist and the .sub must be non-empty."""
    idx = os.path.join(workdir, "sub.idx")
    sub = os.path.join(workdir, "sub.sub")
    cmd = ["mkvextract", "tracks", file_path, f"{stream_index}:{idx}"]
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
    if (proc.returncode != 0 or not os.path.exists(idx)
            or not os.path.exists(sub) or os.path.getsize(sub) == 0):
        return None
    return idx


def _subtile_ocr_to_text(idx_path: str, tess_lang: str) -> str | None:
    """OCR a VobSub .idx/.sub pair to text with subtile-ocr (tesseract under
    the hood). Sync (blocking); caller runs it in an executor. Fail-open:
    returns None if the tool is absent or OCR fails."""
    import shutil as _shutil
    import subprocess
    exe = _shutil.which("subtile-ocr")
    if not exe:
        print("[IMG-OCR] subtile-ocr not installed — VobSub OCR unavailable "
              "(image has PGS support only)", flush=True)
        return None
    out_srt = os.path.splitext(idx_path)[0] + ".srt"
    cmd = [exe, "-l", tess_lang, "-o", out_srt, idx_path]
    try:
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1800, check=True,
        )
    except Exception as exc:
        print(f"[IMG-OCR] subtile-ocr failed ({tess_lang}): {exc}", flush=True)
        return None
    if not os.path.exists(out_srt):
        return None
    try:
        with open(out_srt, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    finally:
        try:
            os.unlink(out_srt)
        except OSError:
            pass
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

    codec_l = (codec or "").lower()
    workdir = tempfile.mkdtemp(prefix="shrinkerr_imgocr_")
    try:
        loop = asyncio.get_event_loop()
        if codec_l in _VOBSUB_CODECS:
            # VobSub (DVD) → subtile-ocr. Same Latin-first / non-Latin-fallback
            # shape as PGS; the extract + OCR tool differ.
            await _report(f"Extracting subtitle track {stream_index}…")
            idx = await _extract_vobsub(file_path, stream_index, workdir)
            if not idx:
                return (None, 0.0)
            await _report(f"OCR (Latin) on subtitle track {stream_index}…")
            text = await loop.run_in_executor(None, _subtile_ocr_to_text, idx, _VOBSUB_LATIN_LANG)
            if text:
                lang, conf = detect_subtitle_language(text)
                if lang:
                    return (lang, conf)
            await _report(f"OCR (CJK/Cyrillic/Arabic) on subtitle track {stream_index}…")
            text = await loop.run_in_executor(None, _subtile_ocr_to_text, idx, _VOBSUB_NON_LATIN_LANG)
            if text:
                return detect_subtitle_language(text)
            return (None, 0.0)

        # PGS (Blu-ray) → pgsrip.
        await _report(f"Extracting subtitle track {stream_index}…")
        sup = await _extract_sup(file_path, stream_index, workdir)
        if not sup:
            return (None, 0.0)
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


async def detect_external_vobsub_language(idx_path: str) -> tuple[str | None, float]:
    """OCR an on-disk external VobSub `.idx`/`.sub` pair (subtile-ocr) and
    detect its language. Same Latin-first → non-Latin fallback as embedded
    VobSub. Returns (ISO 639-2 B-form, confidence) or (None, 0.0). Fail-open.

    The pair is copied into a tempdir first so nothing is written into the
    user's media folder (subtile-ocr emits its .srt next to the .idx)."""
    from backend.language_detection import detect_subtitle_language
    sub_path = os.path.splitext(idx_path)[0] + ".sub"
    if not (os.path.isfile(idx_path) and os.path.isfile(sub_path)):
        return (None, 0.0)
    workdir = tempfile.mkdtemp(prefix="shrinkerr_extvob_")
    try:
        tmp_idx = os.path.join(workdir, "sub.idx")
        shutil.copyfile(idx_path, tmp_idx)
        shutil.copyfile(sub_path, os.path.join(workdir, "sub.sub"))
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _subtile_ocr_to_text, tmp_idx, _VOBSUB_LATIN_LANG)
        if text:
            lang, conf = detect_subtitle_language(text)
            if lang:
                return (lang, conf)
        text = await loop.run_in_executor(None, _subtile_ocr_to_text, tmp_idx, _VOBSUB_NON_LATIN_LANG)
        if text:
            return detect_subtitle_language(text)
        return (None, 0.0)
    except Exception as exc:
        print(f"[IMG-OCR] external VobSub detection failed for {idx_path}: {exc}", flush=True)
        return (None, 0.0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
