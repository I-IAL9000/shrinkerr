"""A clean-up remux gives "und" tracks a language under Settings → "Auto-detect
languages for unknown tracks" (v0.10.0). The remux after a conversion that
kept the video stamped none (what the conversion detected was lost), a
clean-up never detected any, and an OCR'd subtitle took the "und" of the
image track it was read from."""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend import image_sub_ocr

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _audio_languages(path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return [(s.get("tags") or {}).get("language", "und") for s in json.loads(out)["streams"]]


async def _library(test_db, src, langs):
    from backend.tests.test_same_name_conversion import _mkv
    _mkv(src, langs)
    tracks = [{"stream_index": i + 1, "language": lang, "codec": "aac", "keep": True} for i, lang in enumerate(langs)]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, native_language, "
                         "language_source, audio_tracks_json, subtitle_tracks_json) VALUES (?, 1, 't', 'eng', 'api', ?, "
                         "'[]')", (str(src), json.dumps(tracks)))
        await db.commit()


async def _run(test_db, src, job_type, monkeypatch, **job) -> list[dict]:
    import backend.audio as audio
    from backend.queue import JobQueue, QueueWorker
    remuxed, remux = [], audio.remux_audio

    async def recorded(**kwargs):
        remuxed.append(kwargs)
        return await remux(**kwargs)
    monkeypatch.setattr(audio, "remux_audio", recorded)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), job_type, **job)
    await QueueWorker(test_db)._process_job(next(j for j in await queue.get_all_jobs() if j["id"] == job_id))
    return remuxed


def _whisper(monkeypatch, answers: dict) -> list[int]:
    import backend.language_detection as detection
    asked = []

    async def detect(file_path, stream_index, duration=0.0):
        asked.append(stream_index)
        return (answers[stream_index], 0.9, None) if stream_index in answers else (None, 0.0, None)
    monkeypatch.setattr(detection, "detect_audio_language", detect)
    return asked


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_kept_video_keeps_the_languages_its_conversion_found(test_db, tmp_path, monkeypatch):
    """UFO Abduction (1989): the VMAF target kept the video, the clean-up
    remuxed the original, and its two und tracks stayed und."""
    import backend.converter as converter
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", True)
    src = tmp_path / "UFO Abduction (1989).mkv"
    await _library(test_db, src, ["und", "und"])

    async def kept(**kwargs):  # as convert_file: detect (patching the caller's probe), then keep the video
        for t in kwargs["pre_probe"]["audio_tracks"]:
            t["language"] = "eng"
        return {"success": False, "output_path": None, "space_saved": 0,
                "error": "The video was kept as it is: VMAF target 95 needs CQ 14, and its samples came out "
                         "12% larger than the original's.",
                "error_key": "errors.vmafTargetLarger", "error_params": {"target": "95", "quality": "CQ 14", "pct": "12"}}
    monkeypatch.setattr(converter, "convert_file", kept)
    asked = _whisper(monkeypatch, {})

    remuxed = await _run(test_db, src, "combined", monkeypatch, encoder="nvenc")

    assert [r["audio_languages"] for r in remuxed] == [{1: "eng", 2: "eng"}]
    assert asked == []  # not detected a second time
    assert _audio_languages(src) == ["eng", "eng"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_cleanup_gives_und_audio_its_language(test_db, tmp_path, monkeypatch):
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", True)
    src = tmp_path / "Film (1989).mkv"
    await _library(test_db, src, ["und", "fre"])
    asked = _whisper(monkeypatch, {1: "eng"})

    remuxed = await _run(test_db, src, "audio", monkeypatch, audio_tracks_to_remove=[2])

    assert asked == [1]
    assert [r["audio_languages"] for r in remuxed] == [{1: "eng"}]
    assert _audio_languages(src) == ["eng"]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT audio_tracks_json FROM scan_results") as cur:
            assert [t["language"] for t in json.loads((await cur.fetchone())[0])] == ["eng"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_with_the_setting_off_nothing_is_detected(test_db, tmp_path, monkeypatch):
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", False)
    src = tmp_path / "Film (1989).mkv"
    await _library(test_db, src, ["und", "fre"])
    asked = _whisper(monkeypatch, {1: "eng"})

    remuxed = await _run(test_db, src, "audio", monkeypatch, audio_tracks_to_remove=[2])

    assert asked == []
    assert [r["audio_languages"] for r in remuxed] == [None]
    assert _audio_languages(src) == ["und"]


ENGLISH_SRT = ("1\n00:00:01,000 --> 00:00:03,000\nWhere did you put the keys to the car?\n\n"
               "2\n00:00:04,000 --> 00:00:06,000\nI left them on the kitchen table this morning.\n\n"
               "3\n00:00:07,000 --> 00:00:09,000\nThere is something out there in the woods.\n")


async def _ocr(tmp_path, monkeypatch, language, srt_text):
    """image_subs_to_srt over one PGS track, with the extraction and OCR
    stubbed to give `srt_text`."""
    async def extract(cmd, out_path, timeout=600):
        with open(out_path, "wb") as fh:
            fh.write(b"PG")
        return True

    def ocr(sup_path, tess_langs, timeout):
        srt = sup_path[:-4] + ".srt"
        with open(srt, "w", encoding="utf-8") as fh:
            fh.write(srt_text)
        return srt
    monkeypatch.setattr(image_sub_ocr, "_run_extract", extract)
    monkeypatch.setattr(image_sub_ocr, "_pgsrip_rip_isolated", ocr)
    plan = [{"stream_index": 3, "codec": "hdmv_pgs_subtitle", "language": language, "forced": False, "title": "",
             "pack": "eng"}]
    work = tmp_path / "ocr"
    work.mkdir()
    return await image_sub_ocr.image_subs_to_srt(str(tmp_path / "Film.m2ts"), plan, str(work))


@pytest.mark.asyncio
async def test_an_ocr_subtitle_of_an_und_track_gets_the_language_of_its_text(tmp_path, monkeypatch):
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", True)
    assert [s["language"] for s in await _ocr(tmp_path, monkeypatch, "und", ENGLISH_SRT)] == ["eng"]


@pytest.mark.asyncio
async def test_an_ocr_subtitle_keeps_a_language_it_was_given(tmp_path, monkeypatch):
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", True)
    assert [s["language"] for s in await _ocr(tmp_path, monkeypatch, "ger", ENGLISH_SRT)] == ["ger"]


@pytest.mark.asyncio
async def test_an_ocr_subtitle_stays_und_with_the_setting_off(tmp_path, monkeypatch):
    from backend import scanner
    monkeypatch.setitem(scanner._cleanup_enabled_cache, "auto_detect_languages", False)
    assert [s["language"] for s in await _ocr(tmp_path, monkeypatch, "und", ENGLISH_SRT)] == ["und"]
