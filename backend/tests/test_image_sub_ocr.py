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
