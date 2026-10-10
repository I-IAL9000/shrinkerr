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

On-demand only. Fail-open: any failure returns (None, 0.0).

The same OCR also turns kept image subtitles into SRT tracks (v0.10.0,
srt_plan / image_subs_to_srt) — players that can't show PGS or VobSub
transcode to burn them in."""
from __future__ import annotations

import asyncio
import functools
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
    _patch_pgsrip_to_time()


def _patch_pgsrip_to_time() -> None:
    """pgsrip 0.1.11 `utils.to_time` is `SubRipTime.from_ordinal(value) if
    value else None` — so a cue whose timestamp ordinal is 0 (a subtitle at
    00:00:00, or a zero-value PGS segment) yields None instead of 00:00:00.
    ripper then builds `SubRipItem(0, None, ...)` and pysrt dies with
    "SubRipTime() argument after * must be an iterable, not NoneType",
    aborting the whole rip → the track stays und (the SubRipTime crash the
    v0.9.53 fail-fast merely stopped retrying). Treat 0 as a real time
    (`value is not None`). Patches every pgsrip module that imported the
    name. Idempotent, fail-open. v0.9.72."""
    try:
        import sys as _sys
        from pgsrip import utils as _u
        from pysrt import SubRipTime as _SRT
        if getattr(_u, "_shrinkerr_timepatch", False):
            return
        _orig = _u.to_time

        def _safe_time(value):
            return _SRT.from_ordinal(value) if value is not None else None

        _u.to_time = _safe_time
        _u._shrinkerr_timepatch = True
        for _name, _mod in list(_sys.modules.items()):
            if _name.startswith("pgsrip") and getattr(_mod, "to_time", None) is _orig:
                _mod.to_time = _safe_time
    except Exception:
        pass


class _PgsRipError(Exception):
    """pgsrip hit a hard decode failure on this .sup (it logs 'Error while
    trying to rip'). Retrying with other tesseract languages or on the full
    track fails identically, so treat it as terminal for this track and stop
    burning minutes re-extracting. v0.9.53."""


def _pgsrip_rip(sup_path: str, tess_langs: tuple[str, ...] = ()) -> str | None:
    """Run pgsrip on a .sup; the path of the .srt it wrote, or None. Sync
    (pgsrip is blocking). Raises _PgsRipError when pgsrip cannot decode the
    .sup at all. pgsrip OCRs in the language named in the .sup's file name
    (none: tesseract's default, English); `tess_langs` only labels the logs."""
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
        # "Error while trying to rip" = pgsrip couldn't decode this .sup (not
        # merely blank OCR). Surface it so the caller skips the other language
        # pass and the full-track retry, which fail identically. v0.9.53.
        if "error while trying to rip" in _pgs_err.lower():
            raise _PgsRipError(",".join(tess_langs))
        return None
    return srt


def _pgsrip_to_text(sup_path: str, tess_langs: tuple[str, ...]) -> str | None:
    """Run pgsrip on a .sup with the given tesseract language(s); read the
    produced .srt and return its dialogue text. Sync (pgsrip is blocking);
    the caller runs it in an executor. Returns None on empty output; raises
    _PgsRipError when pgsrip cannot decode the .sup at all."""
    srt = _pgsrip_rip(sup_path, tess_langs)
    if not srt:
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
    stripped = _strip_srt(text)
    # v0.9.79: log the PGS OCR yield too (was silent on the empty-srt path).
    if not stripped:
        print(f"[IMG-OCR] pgsrip ({','.join(tess_langs)}): srt written but no "
              f"readable text ({len(raw)} raw bytes)", flush=True)
    else:
        _sample = " ".join(stripped.split())[:80]
        print(f"[IMG-OCR] pgsrip ({','.join(tess_langs)}): OCR'd {len(stripped)} "
              f"chars, sample={_sample!r}", flush=True)
    return stripped


async def _extract_vobsub(file_path: str, stream_index: int, workdir: str) -> str | None:
    """mkvextract a VobSub track to <workdir>/sub.idx (+ sub.sub). Returns the
    .idx path, or None on failure/empty. VobSub is a paired format — both
    files must exist and the .sub must be non-empty."""
    idx = os.path.join(workdir, "sub.idx")
    sub = os.path.join(workdir, "sub.sub")
    cmd = ["mkvextract", "tracks", file_path, f"{stream_index}:{idx}"]
    # v0.9.74: capture output (mkvtoolnix logs to stdout) and log WHY extraction
    # failed — previously it was discarded, so a VobSub that couldn't be pulled
    # just went silently to und with no [IMG-OCR] line at all.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except (asyncio.TimeoutError, OSError) as exc:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        print(f"[IMG-OCR] mkvextract VobSub s{stream_index} timed out/errored: {exc}", flush=True)
        return None
    sub_sz = os.path.getsize(sub) if os.path.exists(sub) else 0
    if proc.returncode != 0 or not os.path.exists(idx) or sub_sz == 0:
        print(f"[IMG-OCR] mkvextract VobSub s{stream_index} produced nothing usable "
              f"(rc={proc.returncode}, idx_exists={os.path.exists(idx)}, sub_bytes={sub_sz}): "
              f"{(out or b'').decode(errors='replace')[-300:]!r}", flush=True)
        return None
    return idx


def _normalize_idx_palette(idx_path: str) -> None:
    """mkvextract writes the VobSub `.idx` `palette:` line with inconsistent
    separators — a bare `,` (no space) after every 4th colour — which
    subtile-ocr's strict parser rejects ("error during palette parsing"):

        palette: 000000, f0f0f0, cccccc, 999999,3333fa, 1111bb, fa3333, bb1111,33fa33, ...

    Rewrite it with a uniform `, ` between all entries. Idempotent (a native
    .idx already formatted this way is unchanged). Fail-open."""
    try:
        with open(idx_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for i, line in enumerate(lines):
            if line.startswith("palette:"):
                colors = [c.strip() for c in line[len("palette:"):].split(",") if c.strip()]
                fixed = "palette: " + ", ".join(colors) + "\n"
                if fixed != line:
                    lines[i] = fixed
                    with open(idx_path, "w", encoding="utf-8") as fh:
                        fh.writelines(lines)
                break
    except Exception:
        pass


def _subtile_ocr_run(idx_path: str, tess_lang: str) -> str | None:
    """OCR a VobSub .idx/.sub pair with subtile-ocr (tesseract under the
    hood) into an .srt beside it; its path, or None if the tool is absent or
    OCR fails. Sync (blocking)."""
    import shutil as _shutil
    import subprocess
    exe = _shutil.which("subtile-ocr")
    if not exe:
        print("[IMG-OCR] subtile-ocr not installed — VobSub OCR unavailable "
              "(image has PGS support only)", flush=True)
        return None
    out_srt = os.path.splitext(idx_path)[0] + ".srt"
    # v0.9.23: mkvextract's .idx palette formatting trips subtile-ocr's parser.
    _normalize_idx_palette(idx_path)
    cmd = [exe, "-l", tess_lang, "-o", out_srt, idx_path]
    try:
        # v0.9.23: capture stderr so a non-zero exit reports WHY, not just
        # "exit status 1" (mirrors the pgsrip error-surfacing fix).
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=1800, check=True,
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr.decode(errors="replace").strip().replace("\n", " ")[-400:]
               if exc.stderr else "")
        print(f"[IMG-OCR] subtile-ocr failed ({tess_lang}) rc={exc.returncode}: {err}", flush=True)
        return None
    except Exception as exc:
        print(f"[IMG-OCR] subtile-ocr failed ({tess_lang}): {exc}", flush=True)
        return None
    if not os.path.exists(out_srt):
        print(f"[IMG-OCR] subtile-ocr produced no srt ({tess_lang})", flush=True)
        return None
    return out_srt


def _subtile_ocr_to_text(idx_path: str, tess_lang: str) -> str | None:
    """OCR a VobSub .idx/.sub pair to text with subtile-ocr (tesseract under
    the hood). Sync (blocking); caller runs it in an executor. Fail-open:
    returns None if the tool is absent or OCR fails."""
    out_srt = _subtile_ocr_run(idx_path, tess_lang)
    if not out_srt:
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
    stripped = _strip_srt(text)
    # v0.9.79: subtile-ocr can exit 0 and write an .srt of only sequence
    # numbers + timestamps when tesseract recognised nothing in the rendered
    # bitmaps (e.g. an unusual VobSub palette — black text — that binarises
    # with too little contrast). That was the one silent VobSub path: no
    # [IMG-OCR] line, the track just went to und. Log the OCR yield so the
    # failure mode is visible (empty-but-present srt vs. clean text the
    # language detector then rejects). (v0.9.77 mistakenly put this log in the
    # PGS helper _pgsrip_to_text, so the VobSub path stayed silent.)
    if not stripped:
        print(f"[IMG-OCR] subtile-ocr ({tess_lang}): srt written but no readable "
              f"text ({len(raw)} raw bytes) — tesseract recognised nothing in the "
              f"bitmaps", flush=True)
    else:
        _sample = " ".join(stripped.split())[:80]
        print(f"[IMG-OCR] subtile-ocr ({tess_lang}): OCR'd {len(stripped)} chars, "
              f"sample={_sample!r}", flush=True)
    return stripped


async def detect_image_sub_language(
    file_path: str, stream_index: int, codec: str, progress_cb=None,
) -> tuple[str | None, float]:
    """OCR a PGS/VobSub track and detect its language. Returns
    (ISO 639-2 B-form, confidence) or (None, 0.0). Fail-open.

    `progress_cb` (v0.9.1): optional async callable(stage: str) invoked at
    coarse stages so the UI can show live status through the multi-minute
    OCR. Called with 'Extracting subtitle…', 'OCR (Latin)…',
    'OCR (CJK/Cyrillic/Arabic)…' plus `stage_key`/`stage_params` keywords
    (serverJobs `detect.*` i18n key) so the UI can translate the stage."""
    from backend.language_detection import detect_subtitle_language

    async def _report(stage: str, stage_key: str):
        if progress_cb is not None:
            try:
                await progress_cb(stage, stage_key=stage_key, stage_params={"track": stream_index})
            except Exception:
                pass

    codec_l = (codec or "").lower()
    workdir = tempfile.mkdtemp(prefix="shrinkerr_imgocr_")
    try:
        loop = asyncio.get_event_loop()
        if codec_l in _VOBSUB_CODECS:
            # VobSub (DVD) → subtile-ocr. Same Latin-first / non-Latin-fallback
            # shape as PGS; the extract + OCR tool differ.
            await _report(f"Extracting subtitle track {stream_index}…", stage_key="detect.extractingTrack")
            idx = await _extract_vobsub(file_path, stream_index, workdir)
            if not idx:
                return (None, 0.0)
            await _report(f"OCR (Latin) on subtitle track {stream_index}…", stage_key="detect.ocrLatin")
            text = await loop.run_in_executor(None, _subtile_ocr_to_text, idx, _VOBSUB_LATIN_LANG)
            if text:
                lang, conf = detect_subtitle_language(text)
                if lang:
                    return (lang, conf)
            await _report(f"OCR (CJK/Cyrillic/Arabic) on subtitle track {stream_index}…", stage_key="detect.ocrNonLatin")
            text = await loop.run_in_executor(None, _subtile_ocr_to_text, idx, _VOBSUB_NON_LATIN_LANG)
            if text:
                return detect_subtitle_language(text)
            return (None, 0.0)

        # PGS (Blu-ray) → pgsrip. Two OCR passes (Latin, then non-Latin) over
        # a given .sup.
        async def _ocr_pgs(sup_path):
            await _report(f"OCR (Latin) on subtitle track {stream_index}…", stage_key="detect.ocrLatin")
            text = await loop.run_in_executor(None, _pgsrip_to_text, sup_path, _LATIN_LANGS)
            if text:
                lang, conf = detect_subtitle_language(text)
                if lang:
                    return (lang, conf)
            await _report(f"OCR (CJK/Cyrillic/Arabic) on subtitle track {stream_index}…", stage_key="detect.ocrNonLatin")
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
            await _report(f"Extracting subtitle sample (track {stream_index})…", stage_key="detect.extractingSample")
            sup = await _extract_sup(file_path, stream_index, workdir, sample_seconds=sample)
            if sup:
                result = await _ocr_pgs(sup)
                if result:
                    return result
        # Full track — either sampling is disabled, or the sample had no
        # detectable text (sparse opening / forced sub whose events fall
        # outside the window).
        await _report(f"Extracting full subtitle track {stream_index}…", stage_key="detect.extractingFullTrack")
        sup = await _extract_sup(file_path, stream_index, workdir, sample_seconds=None)
        if not sup:
            return (None, 0.0)
        result = await _ocr_pgs(sup)
        return result if result else (None, 0.0)
    except _PgsRipError as exc:
        # Hard decode failure — propagated from the first pgsrip pass so we
        # skip the remaining passes and the full-track re-extraction. v0.9.53.
        print(f"[IMG-OCR] pgsrip cannot decode s{stream_index} ({exc}); "
              f"skipping remaining OCR passes", flush=True)
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
    # The pair lives on the media share: checks and the (multi-MB) copy run
    # in a thread, not on the event loop (SC-26, v0.10.0).
    if not await asyncio.to_thread(lambda: os.path.isfile(idx_path) and os.path.isfile(sub_path)):
        return (None, 0.0)
    workdir = tempfile.mkdtemp(prefix="shrinkerr_extvob_")
    try:
        tmp_idx = os.path.join(workdir, "sub.idx")

        def _copy_pair() -> None:
            shutil.copyfile(idx_path, tmp_idx)
            shutil.copyfile(sub_path, os.path.join(workdir, "sub.sub"))
        await asyncio.to_thread(_copy_pair)
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


# ── Image subtitles → SRT (v0.10.0) ────────────────────────────────────────

IMAGE_SUB_CODECS = _PGS_CODECS + _VOBSUB_CODECS
_TEXT_SUB_CODECS = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "tx3g", "text")
OCR_TRACK_TITLE = "OCR"


@functools.lru_cache(maxsize=1)
def tesseract_languages() -> frozenset:
    """The installed tesseract language packs (empty: no tesseract)."""
    import subprocess
    try:
        proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    lines = (proc.stdout or proc.stderr or "").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and " " not in line.strip())


def ocr_pack(language: str | None) -> str | None:
    """The tesseract pack that reads subtitles in `language` (English for an
    unknown one), or None when it isn't installed."""
    from backend.scanner import _ISO_T_TO_B, normalize_lang
    lang = normalize_lang(language)
    to_t = {b: t for t, b in _ISO_T_TO_B.items()}
    pack = "eng" if lang == "und" else {"chi": "chi_sim"}.get(lang) or to_t.get(lang, lang)
    return pack if pack in tesseract_languages() else None


def srt_plan(sub_tracks: list[dict], kept=None, merged: list[dict] | None = None) -> list[dict]:
    """The image subtitle tracks (PGS / VobSub) to read into SRT tracks: the
    kept ones with no text track in the same language (forced alike) kept or
    merged from a sidecar, in a language whose tesseract pack is installed.
    `sub_tracks`: a probe's subtitle_tracks; `kept`: the stream indices that
    stay (None: all); `merged`: the sidecar subtitles being merged."""
    from backend.scanner import normalize_lang
    keep = [t for t in sub_tracks if kept is None or t.get("stream_index") in kept]
    text = {(normalize_lang(t.get("language")), bool(t.get("forced")))
            for t in [*keep, *(merged or [])] if (t.get("codec") or "").lower() in _TEXT_SUB_CODECS}
    plan = []
    for t in keep:
        codec = (t.get("codec") or "").lower()
        lang = normalize_lang(t.get("language"))
        if codec not in IMAGE_SUB_CODECS or (lang, bool(t.get("forced"))) in text:
            continue
        pack = ocr_pack(lang)
        # pgsrip names the pack by the language's code: no chi_sim, aze_cyrl…
        if not pack or (codec in _PGS_CODECS and "_" in pack):
            print(f"[IMG-OCR] No OCR language pack for {lang}: subtitle #{t.get('stream_index')} "
                  f"stays an image", flush=True)
            continue
        plan.append({"stream_index": t.get("stream_index"), "codec": codec, "language": lang,
                     "forced": bool(t.get("forced")), "title": t.get("title") or "", "pack": pack})
    return plan


def ocr_workdir(input_path: str) -> str:
    """A fresh folder for one file's OCR under the system temp folder (never
    the media folder). Folders over a day old — left by a job that failed
    before removing its own — are swept first."""
    import hashlib
    import time
    root = os.path.join(tempfile.gettempdir(), "shrinkerr-ocr")
    os.makedirs(root, exist_ok=True)
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if time.time() - os.path.getmtime(path) > 86400:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    work = os.path.join(root, hashlib.sha1(input_path.encode()).hexdigest()[:16])
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    return work


def _sup_path(workdir: str, stream_index: int, pack: str) -> str | None:
    """Where pgsrip reads track `stream_index` with tesseract pack `pack`: it
    takes the OCR language from the .sup's name, spelt its own way ("fra"
    becomes ".fr.sup"). None when it can't name that pack."""
    from pgsrip.media_path import MediaPath
    path = str(MediaPath(os.path.join(workdir, f"s{stream_index}.{pack}.sup")))
    language = MediaPath(path).language
    return path if language and language.alpha3 == pack else None


def _srt_has_text(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return bool(_strip_srt(fh.read().decode("utf-8", errors="replace")))
    except OSError:
        return False


async def image_subs_to_srt(input_path: str, plan: list[dict], workdir: str) -> list[dict]:
    """Read the planned tracks (srt_plan) into .srt files in `workdir`, as
    subtitles to merge: {path, codec, language, forced, title, stream_index}.
    A track that can't be read is left out. One extraction pass per kind:
    ffmpeg (mkvextract as the fallback) for PGS, mkvextract for VobSub."""
    pgs: list[tuple[dict, str]] = []
    for t in plan:
        if t["codec"] not in _PGS_CODECS:
            continue
        try:
            sup = _sup_path(workdir, t["stream_index"], t["pack"])
        except Exception as exc:  # pgsrip missing
            print(f"[IMG-OCR] PGS OCR unavailable: {exc}", flush=True)
            sup = None
        if sup:
            pgs.append((t, sup))
    vob = [(t, os.path.join(workdir, f"s{t['stream_index']}.idx")) for t in plan if t["codec"] in _VOBSUB_CODECS]
    if pgs:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", input_path]
        for t, sup in pgs:
            cmd += ["-map", f"0:{t['stream_index']}", "-c:s", "copy", sup]
        if not await _run_extract(cmd, pgs[0][1], timeout=3600):
            await _run_extract(_build_mkvextract_cmd(input_path, pgs[0][0]["stream_index"], pgs[0][1])
                               + [f"{t['stream_index']}:{sup}" for t, sup in pgs[1:]], pgs[0][1], timeout=3600)
    if vob:
        await _run_extract(["mkvextract", "tracks", input_path, *[f"{t['stream_index']}:{idx}" for t, idx in vob]],
                           vob[0][1], timeout=3600)

    loop = asyncio.get_running_loop()
    out = []
    for t, src in [*pgs, *vob]:
        if not (os.path.exists(src) and os.path.getsize(src) > 0):
            print(f"[IMG-OCR] Couldn't extract subtitle #{t['stream_index']} for OCR", flush=True)
            continue
        try:
            if t["codec"] in _PGS_CODECS:
                srt = await loop.run_in_executor(None, _pgsrip_rip, src)
            else:
                srt = await loop.run_in_executor(None, _subtile_ocr_run, src, t["pack"])
        except _PgsRipError:
            srt = None
        for leftover in (src, os.path.splitext(src)[0] + ".sub"):  # the image track's served its turn
            try:
                os.unlink(leftover)
            except OSError:
                pass
        if not srt or not _srt_has_text(srt):
            print(f"[IMG-OCR] OCR read no text from subtitle #{t['stream_index']}", flush=True)
            continue
        print(f"[IMG-OCR] Subtitle #{t['stream_index']} ({t['language']}, {t['pack']}) read into SRT", flush=True)
        out.append({"path": srt, "codec": "subrip", "language": t["language"], "forced": t["forced"],
                    "title": f"{t['title']} (OCR)" if t["title"] else OCR_TRACK_TITLE,
                    "stream_index": t["stream_index"]})
    return out
