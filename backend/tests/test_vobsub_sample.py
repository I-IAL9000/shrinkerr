"""DVD (VobSub) subtitles: language detection OCRs a leading sample first, as
PGS has since v0.9.13 (v0.10.0). It OCR'd every image in the film — twice
when the Latin pass found nothing — so an external .idx/.sub took minutes."""
import os
from unittest.mock import AsyncMock

import pytest

from backend import image_sub_ocr as io

IDX = """# VobSub index file, v7 (do not modify this line!)
size: 720x576
palette: 000000, f0f0f0, cccccc, 999999
id: en, index: 0
timestamp: 00:00:10:000, filepos: 000000000
timestamp: 00:15:00:500, filepos: 000000800
timestamp: 00:25:00:000, filepos: 000001000
timestamp: 01:40:00:000, filepos: 000001800
"""
ENGLISH = "Hello, this is an English subtitle with plenty of words to detect the language from. " * 10


def _pair(folder, idx_text=IDX):
    idx = folder / "sub.idx"
    idx.write_text(idx_text)
    (folder / "sub.sub").write_bytes(b"\x00" * 64)
    return str(idx)


def test_the_sample_lists_the_first_twenty_minutes(tmp_path):
    sample = io._sample_idx(_pair(tmp_path), 1200)
    text = open(sample).read()
    assert "00:00:10:000" in text and "00:15:00:500" in text
    assert "00:25:00" not in text and "01:40:00" not in text
    assert text.startswith("# VobSub index file") and "palette:" in text and "id: en" in text
    assert open(os.path.splitext(sample)[0] + ".sub", "rb").read() == b"\x00" * 64  # the same images


def test_no_sample_when_it_would_be_the_whole_track_or_nothing(tmp_path):
    idx = _pair(tmp_path)
    assert io._sample_idx(idx, 7200) is None  # every subtitle: just read the track
    late = IDX.replace("00:00:10:000", "00:30:00:000").replace("00:15:00:500", "00:31:00:000")
    assert io._sample_idx(_pair(tmp_path, late), 1200) is None  # none in the window


async def _detect(tmp_path, monkeypatch, ocr):
    idx = _pair(tmp_path)
    read = []

    def fake_ocr(path, lang):
        read.append(("sample" if "/sample/" in path else "track", lang))
        return ocr(path, lang)
    monkeypatch.setattr(io, "_extract_vobsub", AsyncMock(return_value=idx))
    monkeypatch.setattr(io, "_subtile_ocr_to_text", fake_ocr)
    return await io.detect_image_sub_language("/m/f.mkv", 3, "dvd_subtitle"), read


@pytest.mark.asyncio
async def test_the_sample_is_enough(tmp_path, monkeypatch):
    (lang, _), read = await _detect(tmp_path, monkeypatch, lambda path, lang: ENGLISH)
    assert lang == "eng" and read == [("sample", io._VOBSUB_LATIN_LANG)]


@pytest.mark.asyncio
async def test_a_sample_without_text_reads_the_track(tmp_path, monkeypatch):
    (lang, _), read = await _detect(tmp_path, monkeypatch,
                                    lambda path, lang: ENGLISH if "/sample/" not in path else None)
    assert lang == "eng"
    assert read == [("sample", io._VOBSUB_LATIN_LANG), ("sample", io._VOBSUB_NON_LATIN_LANG),
                    ("track", io._VOBSUB_LATIN_LANG)]


@pytest.mark.asyncio
async def test_plenty_of_text_and_no_language_stops_at_the_sample(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.language_detection.detect_subtitle_language", lambda text: (None, 0.0))
    (lang, _), read = await _detect(tmp_path, monkeypatch, lambda path, lang: ENGLISH)
    assert lang is None and {scope for scope, _ in read} == {"sample"}


@pytest.mark.asyncio
async def test_an_external_pair_is_sampled_too(tmp_path, monkeypatch):
    folder = tmp_path / "Film (2004)"
    folder.mkdir()
    idx = folder / "Film.idx"
    idx.write_text(IDX)
    (folder / "Film.sub").write_bytes(b"\x00" * 64)
    read = []

    def fake_ocr(path, lang):
        read.append("sample" if "/sample/" in path else "track")
        return ENGLISH
    monkeypatch.setattr(io, "_subtile_ocr_to_text", fake_ocr)
    lang, _ = await io.detect_external_vobsub_language(str(idx))
    assert lang == "eng" and read == ["sample"]
