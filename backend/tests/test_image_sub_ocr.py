import os
import pytest
from unittest.mock import patch, AsyncMock


def test_build_mkvextract_cmd():
    from backend.image_sub_ocr import _build_mkvextract_cmd
    cmd = _build_mkvextract_cmd("/m/f.mkv", 3, "/tmp/out.sup")
    assert cmd == ["mkvextract", "tracks", "/m/f.mkv", "3:/tmp/out.sup"]


def test_strip_srt():
    from backend.image_sub_ocr import _strip_srt
    srt = ("1\n00:01:40,667 --> 00:01:46,250\nCiao buon anno nuovo.\n\n"
           "2\n00:03:13,542 --> 00:03:15,250\nSperiamo.\n")
    out = _strip_srt(srt)
    assert "Ciao buon anno nuovo." in out and "Speriamo." in out
    assert "00:01:40" not in out
    assert _strip_srt("") is None
    assert _strip_srt("1\n00:00:01,000 --> 00:00:02,000\n\n") is None


@pytest.mark.asyncio
async def test_detect_image_sub_latin_first_pass(monkeypatch):
    """Latin pass produces Italian text → detected, no fallback needed."""
    from backend import image_sub_ocr as io
    calls = []
    def fake_ocr(sup, langs):
        calls.append(langs)
        return "Ciao, questo e un sottotitolo italiano con abbastanza parole."
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(return_value="/tmp/w/sub.sup"))
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    lang, conf = await io.detect_image_sub_language("/m/f.mkv", 3, "hdmv_pgs_subtitle")
    assert lang == "ita", f"got {lang!r}"
    assert calls == [io._LATIN_LANGS], "should stop after the Latin pass"


@pytest.mark.asyncio
async def test_a_pass_out_of_time_ends_the_tracks_detection(monkeypatch):
    """Dead Tone's two PGS tracks kept a batch busy 20+ minutes: each pass
    stopped at its limit, then the next — more packs, the whole track —
    started. The first that runs out of time ends the track's detection."""
    from backend import image_sub_ocr as io
    calls = []

    def fake_ocr(sup, langs):
        calls.append(langs)
        raise io._PgsRipTimeout("eng")
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(return_value="/tmp/w/sub.sup"))
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    assert await io.detect_image_sub_language("/m/f.mkv", 3, "hdmv_pgs_subtitle") == (None, 0.0)
    assert calls == [io._LATIN_LANGS]


@pytest.mark.asyncio
async def test_detect_image_sub_falls_back_to_non_latin(monkeypatch):
    """Latin pass yields garbage langdetect can't ID → non-Latin pass runs
    and detects Japanese."""
    from backend import image_sub_ocr as io
    calls = []
    def fake_ocr(sup, langs):
        calls.append(langs)
        # tesseract `eng` on Japanese bitmaps typically yields no usable
        # text (empty after strip) → _pgsrip_to_text returns None.
        if langs == io._LATIN_LANGS:
            return None
        return "これは日本語の字幕です。テストのために十分な長さの文章を書いています。"
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(return_value="/tmp/w/sub.sup"))
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    lang, _ = await io.detect_image_sub_language("/m/f.mkv", 3, "hdmv_pgs_subtitle")
    assert lang == "jpn", f"got {lang!r}"
    assert calls == [io._LATIN_LANGS, io._NON_LATIN_LANGS], "both passes should run"


@pytest.mark.asyncio
async def test_detect_image_sub_failopen_no_sup(monkeypatch):
    from backend import image_sub_ocr as io
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(return_value=None))
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    assert await io.detect_image_sub_language("/m/f.mkv", 3, "hdmv_pgs_subtitle") == (None, 0.0)


@pytest.mark.asyncio
async def test_detect_image_sub_failopen_on_exception(monkeypatch):
    from backend import image_sub_ocr as io
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(side_effect=OSError("mkvextract gone")))
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    assert await io.detect_image_sub_language("/m/f.mkv", 3, "hdmv_pgs_subtitle") == (None, 0.0)


@pytest.mark.asyncio
async def test_detect_vobsub_uses_subtile_ocr(monkeypatch):
    """dvd_subtitle routes to the VobSub pipeline (subtile-ocr), not pgsrip."""
    from backend import image_sub_ocr as io
    calls = []
    def fake_ocr(idx, lang):
        calls.append(lang)
        return "Hello, this is an English subtitle with plenty of words to detect."
    monkeypatch.setattr(io, "_extract_vobsub", AsyncMock(return_value="/tmp/w/sub.idx"))
    monkeypatch.setattr(io, "_subtile_ocr_to_text", fake_ocr)
    # pgsrip path must NOT be touched for a VobSub codec.
    monkeypatch.setattr(io, "_extract_sup", AsyncMock(side_effect=AssertionError("PGS path used for VobSub")))
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    lang, conf = await io.detect_image_sub_language("/m/f.mkv", 3, "dvd_subtitle")
    assert lang == "eng", f"got {lang!r}"
    assert calls == [io._VOBSUB_LATIN_LANG], "should detect on the Latin pass"


@pytest.mark.asyncio
async def test_detect_vobsub_failopen_when_extract_fails(monkeypatch):
    from backend import image_sub_ocr as io
    monkeypatch.setattr(io, "_extract_vobsub", AsyncMock(return_value=None))
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    assert await io.detect_image_sub_language("/m/f.mkv", 3, "vobsub") == (None, 0.0)


def test_subtile_ocr_missing_tool_returns_none(monkeypatch):
    """Fail-open: no subtile-ocr binary → None, never raises."""
    from backend import image_sub_ocr as io
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert io._subtile_ocr_to_text("/tmp/sub.idx", "eng") is None


def test_build_sup_sample_cmd():
    from backend.image_sub_ocr import _build_sup_sample_cmd
    cmd = _build_sup_sample_cmd("/m/f.mkv", 2, "/tmp/w/sub.sup", 1200)
    assert cmd == ["ffmpeg", "-v", "error", "-y", "-i", "/m/f.mkv",
                   "-map", "0:2", "-c:s", "copy", "-t", "1200", "/tmp/w/sub.sup"]


def test_pgs_sample_seconds_env(monkeypatch):
    from backend import image_sub_ocr as io
    monkeypatch.delenv("SHRINKERR_PGS_SAMPLE_SECONDS", raising=False)
    assert io._pgs_sample_seconds() == 1200
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "600")
    assert io._pgs_sample_seconds() == 600
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "0")   # 0 = disable sampling
    assert io._pgs_sample_seconds() == 0
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "garbage")
    assert io._pgs_sample_seconds() == 1200


@pytest.mark.asyncio
async def test_detect_pgs_samples_first(monkeypatch):
    """Common case: the sample yields text → no full extraction needed."""
    from backend import image_sub_ocr as io
    extract_calls = []
    async def fake_extract(fp, idx, workdir, sample_seconds=None):
        extract_calls.append(sample_seconds)
        return "/tmp/w/sub.sup"
    def fake_ocr(sup, langs):
        return "Hello there, this is plainly English dialogue with many words."
    monkeypatch.setattr(io, "_extract_sup", fake_extract)
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "1200")
    lang, _c = await io.detect_image_sub_language("/m/f.mkv", 2, "hdmv_pgs_subtitle")
    assert lang == "eng", f"got {lang!r}"
    assert extract_calls == [1200], "should stop after the sample extraction"


@pytest.mark.asyncio
async def test_detect_pgs_falls_back_to_full_when_sample_empty(monkeypatch):
    """Sparse/forced: sample OCR finds nothing → full track is extracted + OCR'd."""
    from backend import image_sub_ocr as io
    extract_calls = []
    async def fake_extract(fp, idx, workdir, sample_seconds=None):
        extract_calls.append(sample_seconds)
        return "/tmp/w/sub.sup"
    ocr_results = ["", "", "Hello there, this is plainly English dialogue with many words."]
    def fake_ocr(sup, langs):
        return ocr_results.pop(0) if ocr_results else ""
    monkeypatch.setattr(io, "_extract_sup", fake_extract)
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "1200")
    lang, _c = await io.detect_image_sub_language("/m/f.mkv", 2, "hdmv_pgs_subtitle")
    assert lang == "eng", f"got {lang!r}"
    assert extract_calls == [1200, None], "sample then full extraction"


@pytest.mark.asyncio
async def test_detect_pgs_rip_error_is_terminal(monkeypatch):
    """A hard pgsrip decode failure on the first pass must not trigger the
    non-Latin pass or the full-track re-extraction — those fail identically
    and waste minutes. Only one OCR call, one (sample) extraction. v0.9.53."""
    from backend import image_sub_ocr as io
    extract_calls = []
    ocr_calls = []
    async def fake_extract(fp, idx, workdir, sample_seconds=None):
        extract_calls.append(sample_seconds)
        return "/tmp/w/sub.sup"
    def fake_ocr(sup, langs):
        ocr_calls.append(langs)
        raise io._PgsRipError(",".join(langs))
    monkeypatch.setattr(io, "_extract_sup", fake_extract)
    monkeypatch.setattr(io, "_pgsrip_to_text", fake_ocr)
    monkeypatch.setattr("tempfile.mkdtemp", lambda **k: "/tmp/w")
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    monkeypatch.setenv("SHRINKERR_PGS_SAMPLE_SECONDS", "1200")
    result = await io.detect_image_sub_language("/m/f.mkv", 2, "hdmv_pgs_subtitle")
    assert result == (None, 0.0)
    assert ocr_calls == [io._LATIN_LANGS], "must stop after the first failed pass"
    assert extract_calls == [1200], "must NOT re-extract the full track"


def test_normalize_idx_palette_fixes_mkvextract_separators(tmp_path):
    """mkvextract's bare-comma palette (no space after every 4th colour) is
    rewritten to uniform ', ' so subtile-ocr's parser accepts it."""
    from backend.image_sub_ocr import _normalize_idx_palette
    idx = tmp_path / "sub.idx"
    idx.write_text(
        "# VobSub index file, v7\n"
        "size: 1920x1080\n"
        "palette: 000000, f0f0f0, cccccc, 999999,3333fa, 1111bb, fa3333, bb1111,33fa33\n"
        "id: und, index: 0\n"
        "timestamp: 00:00:27:486, filepos: 000000000\n"
    )
    _normalize_idx_palette(str(idx))
    txt = idx.read_text()
    pal = [l for l in txt.splitlines() if l.startswith("palette:")][0]
    assert pal == "palette: 000000, f0f0f0, cccccc, 999999, 3333fa, 1111bb, fa3333, bb1111, 33fa33"
    assert "size: 1920x1080" in txt and "id: und, index: 0" in txt  # other lines untouched


def test_normalize_idx_palette_idempotent(tmp_path):
    from backend.image_sub_ocr import _normalize_idx_palette
    idx = tmp_path / "sub.idx"
    good = "palette: 000000, f0f0f0, cccccc, 999999\n"
    idx.write_text("size: 720x480\n" + good)
    _normalize_idx_palette(str(idx))
    assert good in idx.read_text()


def test_subtile_ocr_reads_srt_and_returns_text(monkeypatch, tmp_path):
    """Real body: subtile-ocr writes an .srt → we read + strip it and the
    OCR-yield log runs with the real `tess_lang`. Guards the v0.9.77-class
    bug where a wrong variable name NameErrors only on a produced srt (which
    the mocked tests never exercise)."""
    from backend import image_sub_ocr as io
    idx = tmp_path / "sub.idx"
    idx.write_text("palette: 000000, ffffff\n")
    out_srt = tmp_path / "sub.srt"
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/subtile-ocr")

    def fake_run(cmd, **kw):
        out_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nRight. Okay.\n")
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    out = io._subtile_ocr_to_text(str(idx), "eng")
    assert out is not None and "Right. Okay." in out


def test_subtile_ocr_empty_srt_returns_none(monkeypatch, tmp_path):
    """srt with only timestamps (tesseract recognised nothing) → None, and the
    'no readable text' log branch runs cleanly."""
    from backend import image_sub_ocr as io
    idx = tmp_path / "sub.idx"
    idx.write_text("palette: 000000, ffffff\n")
    out_srt = tmp_path / "sub.srt"
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/subtile-ocr")

    def fake_run(cmd, **kw):
        out_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n\n")
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert io._subtile_ocr_to_text(str(idx), "eng") is None


def _fake_pgsrip(tmp_path, monkeypatch, mode: str) -> None:
    """A pgsrip and a tesseract for the OCR process (backend.pgsrip_child) to
    find: `mode` "ok" writes an .srt, "slow" never ends, "broken" can't rip."""
    fakes = tmp_path / "fakes"
    (fakes / "pgsrip").mkdir(parents=True)
    (fakes / "babelfish").mkdir()
    (fakes / "pgsrip" / "__init__.py").write_text(
        "import logging, os, time\n"
        "class Options:\n    def __init__(self, **kw): pass\n"
        "def Sup(path): return path\n"
        "class _Rip:\n"
        "    def rip(self, media, opts):\n"
        f"        mode = {mode!r}\n"
        "        if mode == 'slow': time.sleep(60)\n"
        "        if mode == 'broken':\n"
        "            logging.getLogger('pgsrip').error('Error while trying to rip'); return\n"
        "        open(os.path.splitext(media)[0] + '.srt', 'w').write("
        "'1\\n00:00:01,000 --> 00:00:02,000\\nHello there.\\n')\n"
        "pgsrip = _Rip()\n")
    (fakes / "pgsrip" / "utils.py").write_text("def from_hex(b): return int(b.hex(), 16)\n")
    (fakes / "pgsrip" / "ripper.py").write_text("tess = None\n")
    (fakes / "babelfish" / "__init__.py").write_text("def Language(code): return code\n")
    (fakes / "tesseract").write_text("#!/bin/sh\necho 'List of available languages (1):'\necho eng\n")
    (fakes / "tesseract").chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", str(fakes))
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}{os.environ['PATH']}")


def test_pgsrip_reads_srt_and_returns_text(monkeypatch, tmp_path, capsys):
    """Real body: pgsrip writes an .srt → we read + strip it and the OCR-yield
    log runs with `tess_langs`. Regression guard for the v0.9.77 wrong-variable
    (tess_lang vs tess_langs) NameError that only fires on a produced srt.
    pgsrip runs in its own process (v0.10.0), whose log reaches the server's."""
    from backend import image_sub_ocr as io
    sup = tmp_path / "sub.sup"
    sup.write_bytes(b"\x00")
    _fake_pgsrip(tmp_path, monkeypatch, "ok")
    out = io._pgsrip_to_text(str(sup), ("eng",))
    assert out is not None and "Hello there." in out
    assert "OCR'd" in capsys.readouterr().out


def test_pgsrip_that_runs_too_long_is_stopped(monkeypatch, tmp_path, capsys):
    """A Blu-ray's subtitles took pgsrip 25+ minutes, in a thread nothing
    could stop, and the app crawled meanwhile: it's a process now, stopped
    at its time limit."""
    import time
    from backend import image_sub_ocr as io
    sup = tmp_path / "sub.sup"
    sup.write_bytes(b"\x00")
    _fake_pgsrip(tmp_path, monkeypatch, "slow")
    started = time.monotonic()
    with pytest.raises(io._PgsRipTimeout):  # the track's other passes would only take longer
        io._pgsrip_rip_isolated(str(sup), ("eng",), timeout=2)
    assert time.monotonic() - started < 20 and "stopped after" in capsys.readouterr().out


def test_a_sup_pgsrip_cant_decode_is_still_told_apart(monkeypatch, tmp_path):
    from backend import image_sub_ocr as io
    sup = tmp_path / "sub.sup"
    sup.write_bytes(b"\x00")
    _fake_pgsrip(tmp_path, monkeypatch, "broken")
    with pytest.raises(io._PgsRipError):
        io._pgsrip_rip_isolated(str(sup), ("eng",), timeout=60)
