"""Detect languages says why an external subtitle stayed und (v0.10.0), as
it does for embedded tracks: The Consequences of Love's .idx/.sub stayed
UND after minutes of OCR with no explanation."""
import json
import shutil

import aiosqlite
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.mark.asyncio
async def test_an_external_subtitle_that_stays_und_says_why(test_db, tmp_path, monkeypatch):
    import backend.image_sub_ocr as image_sub_ocr
    import backend.routes.scan as scan_route
    from backend.tests.test_same_name_conversion import _mkv
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    folder = tmp_path / "Film (2004)"
    folder.mkdir()
    video = folder / "Film.mkv"
    _mkv(video, ["ita"])
    srt = folder / "Film.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n\n")  # no words
    idx = folder / "Film.idx"
    idx.write_text("# VobSub index file, v7 (do not modify this line!)\n")
    (folder / "Film.sub").write_bytes(b"\x00")

    async def no_text(path, stream=None, outcome=None):
        return (None, 0.0)
    monkeypatch.setattr(image_sub_ocr, "detect_external_vobsub_language", no_text)
    subs = [{"stream_index": -1, "language": "und", "codec": "subrip", "external": True, "external_path": str(srt)},
            {"stream_index": -1, "language": "und", "codec": "dvd_subtitle", "external": True,
             "external_path": str(idx)}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, native_language, language_source, "
                         "audio_tracks_json, subtitle_tracks_json, has_und_tracks_flag, scan_timestamp) "
                         "VALUES (?, 1, 'ita', 'api', ?, ?, 1, '2026-01-01')",
                         (str(video), json.dumps([{"stream_index": 1, "language": "ita", "codec": "aac"}]),
                          json.dumps(subs)))
        await db.commit()

    await scan_route.detect_languages(scan_route.DetectLanguagesRequest(file_path=str(video)), notify_plex=False)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT subtitle_tracks_json FROM scan_results WHERE file_path = ?", (str(video),)) as cur:
            stored = {t["external_path"]: t for t in json.loads((await cur.fetchone())[0])}
    assert stored[str(srt)]["language"] == "und" and stored[str(srt)]["detect_note_key"] == "subNoText"
    assert stored[str(idx)]["language"] == "und" and stored[str(idx)]["detect_note_key"] == "ocrNoText"


@pytest.mark.asyncio
async def test_text_read_but_no_language_says_so(test_db, tmp_path, monkeypatch):
    """The Consequences of Love: OCR read 8000 characters of mixed Italian and
    English and the note said it had found no usable text. Each stream of
    the .idx is read alone, and a text without a language says that."""
    import backend.image_sub_ocr as image_sub_ocr
    import backend.routes.scan as scan_route
    from backend.tests.test_same_name_conversion import _mkv
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    video = tmp_path / "Film.mkv"
    _mkv(video, ["ita"])
    idx = tmp_path / "Film.idx"
    idx.write_text("# VobSub index file, v7 (do not modify this line!)\n")
    (tmp_path / "Film.sub").write_bytes(b"\x00")
    asked = []

    async def text_no_language(path, stream=None, outcome=None):
        asked.append(stream)
        outcome["chars"] = 7998
        return (None, 0.0)
    monkeypatch.setattr(image_sub_ocr, "detect_external_vobsub_language", text_no_language)
    subs = [{"stream_index": -1, "language": "und", "codec": "dvd_subtitle", "external": True,
             "external_path": str(idx), "idx_stream": 2}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, native_language, language_source, "
                         "audio_tracks_json, subtitle_tracks_json, has_und_tracks_flag, scan_timestamp) "
                         "VALUES (?, 1, 'ita', 'api', ?, ?, 1, '2026-01-01')",
                         (str(video), json.dumps([{"stream_index": 1, "language": "ita", "codec": "aac"}]),
                          json.dumps(subs)))
        await db.commit()

    await scan_route.detect_languages(scan_route.DetectLanguagesRequest(file_path=str(video)), notify_plex=False)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT subtitle_tracks_json FROM scan_results WHERE file_path = ?", (str(video),)) as cur:
            [stored] = json.loads((await cur.fetchone())[0])
    assert asked == [2] and stored["detect_note_key"] == "subNotConfident" and stored["idx_stream"] == 2


@pytest.mark.asyncio
async def test_a_row_older_than_its_file_is_brought_up_to_date(test_db, tmp_path, monkeypatch):
    """The Real Housewives of Atlanta: the file's audio said eng, its row und.
    Detect languages found nothing und in the file and left the row — and the
    title under Unknown language, with no explanation — as it was."""
    import backend.routes.scan as scan_route
    from backend.tests.test_same_name_conversion import _mkv
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    video = tmp_path / "Show - S09E04.mkv"
    _mkv(video, ["eng"])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, native_language, language_source, "
                         "audio_tracks_json, subtitle_tracks_json, has_und_tracks_flag, scan_timestamp) "
                         "VALUES (?, 1, 'eng', 'api', ?, '[]', 1, '2026-01-01')",
                         (str(video), json.dumps([{"stream_index": 1, "language": "und", "codec": "aac"}])))
        await db.commit()

    result = await scan_route.detect_languages(scan_route.DetectLanguagesRequest(file_path=str(video)),
                                               notify_plex=False)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT audio_tracks_json, has_und_tracks_flag FROM scan_results WHERE file_path = ?",
                              (str(video),)) as cur:
            audio, flag = await cur.fetchone()
    assert [t["language"] for t in json.loads(audio)] == ["eng"] and flag == 0 and result["changed"]
