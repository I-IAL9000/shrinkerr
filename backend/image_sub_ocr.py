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


def _pgs_sample_seconds() -> int:
    """How much of a PGS track to OCR for language detection. Detecting a
    language needs a few dozen lines, not the whole 2-hour track — pgsrip
    OCRs every image, so sampling the leading slice is a big speed-up.
    Tunable; 0 disables sampling (always OCR the full track)."""
    try:
        return int(os.environ.get("SHRINKERR_PGS_SAMPLE_SECONDS", "1200"))
    except ValueError:
        return 1200


def _build_sup_sample_cmd(file_path: str, stream_index: int, out_sup: str, seconds: int) -> list[str]:
    """ffmpeg command copying the first `seconds` of a PGS track to a .sup."""
    return ["ffmpeg", "-v", "error", "-y", "-i", file_path,
            "-map", f"0:{stream_index}", "-c:s", "copy", "-t", str(seconds), out_sup]


async def _run_extract(cmd: list[str], out_path: str, timeout: int = 600) -> bool:
    """Run an extraction subprocess; True if it exits 0 with a non-empty out.
    Logs rc + stderr tail on failure — extraction failures were silent before,
    hiding a whole class of 'stayed und'."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, OSError) as exc:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        print(f"[IMG-OCR] extract error ({cmd[0]}): {exc}", flush=True)
        return False
    ok = proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    if not ok:
        tail = (stderr.decode(errors="replace")[-300:] if stderr else "").strip()
        print(f"[IMG-OCR] extract failed ({cmd[0]} rc={proc.returncode}): {tail}", flush=True)
    return ok


async def _extract_sup(file_path: str, stream_index: int, workdir: str,
                       sample_seconds: int | None = None) -> str | None:
    """Extract the PGS track to a .sup in `workdir`. When `sample_seconds` is
    set, copy only that leading slice via ffmpeg (fast OCR sample); on ffmpeg
    failure, fall back to a full mkvextract. Returns the path or None."""
    sup = os.path.join(workdir, "sub.sup")
    if sample_seconds:
        if await _run_extract(_build_sup_sample_cmd(file_path, stream_index, sup, sample_seconds), sup, 300):
            return sup
        if os.path.exists(sup):
            try:
                os.unlink(sup)
            except OSError:
                pass
    if await _run_extract(_build_mkvextract_cmd(file_path, stream_index, sup), sup, 300):
        return sup
    return None


def _patch_pgsrip_from_hex() -> None:
    """pgsrip 0.1.11 does `int(b.hex(), 16)` (utils.from_hex) with no guard,
    so an empty byte slice — a truncated/padded trailing segment at the end
    of a .sup — raises ValueError, which aborts the WHOLE rip and discards the
    valid subtitles parsed before it. Make from_hex empty-safe (empty → 0) so
    parsing finishes on the good segments. Patches every already-imported
    pgsrip module that references the name. Idempotent, fail-open."""
    try:
        import sys as _sys
        from pgsrip import utils as _u
        if getattr(_u, "_shrinkerr_hexpatch", False):
            return
        _orig = _u.from_hex

        def _safe(b):
            return _orig(b) if b else 0

        _u.from_hex = _safe
        _u._shrinkerr_hexpatch = True
        for _name, _mod in list(_sys.modules.items()):
            if _name.startswith("pgsrip") and getattr(_mod, "from_hex", None) is _orig:
                _mod.from_hex = _safe
    except Exception:
        pass


def _pgsrip_to_text(sup_path: str, tess_langs: tuple[str, ...]) -> str | None:
    """Run pgsrip on a .sup with the given tesseract language(s); read the
    produced .srt and return its dialogue text. Sync (pgsrip is blocking);
    the caller runs it in an executor. Returns None on failure/empty."""
    import io
    import logging
    _buf = io.StringIO()
    _handler = logging.StreamHandler(_buf)
    _pgs_logger = logging.getLogger("pgsrip")
    try:
        from pgsrip import pgsrip, Sup, Options
        from babelfish import Language
        _patch_pgsrip_from_hex()  # v0.9.22: survive empty-byte reads
        media = Sup(sup_path)
        langs = {Language(code) for code in tess_langs}
        _pgs_logger.addHandler(_handler)  # capture pgsrip's swallowed errors
        try:
            pgsrip.rip(media, Options(languages=langs, overwrite=True))
        finally:
            _pgs_logger.removeHandler(_handler)
    except Exception as exc:
        print(f"[IMG-OCR] pgsrip failed ({','.join(tess_langs)}): {exc}", flush=True)
        return None
    srt = os.path.splitext(sup_path)[0] + ".srt"
    if not os.path.exists(srt):
        _pgs_err = _buf.getvalue().strip().replace("\n", " ")[-300:]
        _detail = f": {_pgs_err}" if _pgs_err else ""
        print(f"[IMG-OCR] pgsrip produced no srt ({','.join(tess_langs)}){_detail}", flush=True)
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
        print(f"[IMG-OCR] subtile-ocr produced no srt ({tess_lang})", flush=True)
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

        # PGS (Blu-ray) → pgsrip. Two OCR passes (Latin, then non-Latin) over
        # a given .sup.
        async def _ocr_pgs(sup_path):
            await _report(f"OCR (Latin) on subtitle track {stream_index}…")
            text = await loop.run_in_executor(None, _pgsrip_to_text, sup_path, _LATIN_LANGS)
            if text:
                lang, conf = detect_subtitle_language(text)
                if lang:
                    return (lang, conf)
            await _report(f"OCR (CJK/Cyrillic/Arabic) on subtitle track {stream_index}…")
            text = await loop.run_in_executor(None, _pgsrip_to_text, sup_path, _NON_LATIN_LANGS)
            if text:
                lang, conf = detect_subtitle_language(text)
                if lang:
                    return (lang, conf)
            return None

        # v0.9.13: OCR a leading sample first — pgsrip OCRs every image, so a
        # full 2-hour track can take 10+ minutes when we only need a few dozen
        # lines to ID the language.
        sample = _pgs_sample_seconds()
        if sample:
            await _report(f"Extracting subtitle sample (track {stream_index})…")
            sup = await _extract_sup(file_path, stream_index, workdir, sample_seconds=sample)
            if sup:
                result = await _ocr_pgs(sup)
                if result:
                    return result
        # Full track — either sampling is disabled, or the sample had no
        # detectable text (sparse opening / forced sub whose events fall
        # outside the window).
        await _report(f"Extracting full subtitle track {stream_index}…")
        sup = await _extract_sup(file_path, stream_index, workdir, sample_seconds=None)
        if not sup:
            return (None, 0.0)
        result = await _ocr_pgs(sup)
        return result if result else (None, 0.0)
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
