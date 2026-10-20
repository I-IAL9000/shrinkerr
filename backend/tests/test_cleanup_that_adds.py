"""A clean-up that adds to the file (v0.10.0). The Consequences of Love:
removing the Italian and French streams of its .idx/.sub and merging the
English one into the MKV made it 2 MB larger — the merged subtitle's images
— and the job ignored the film as "no savings", so the XviD was never to
be converted; and the English stream, an image subtitle, got no SRT copy."""
import json
import shutil

import aiosqlite
import pytest

from backend import image_sub_ocr as io

TWO = """# VobSub index file, v7 (do not modify this line!)
size: 720x576
id: it, index: 0
timestamp: 00:00:01:000, filepos: 000000000
id: en, index: 1
timestamp: 00:00:01:000, filepos: 000000800
"""


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_a_cleanup_that_grew_the_file_doesnt_ignore_it(test_db, tmp_path, monkeypatch):
    import sys

    import backend.audio
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_remux_guards import _clip
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    src = _clip(tmp_path)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, "
                         "audio_tracks_json, subtitle_tracks_json, scan_timestamp) VALUES (?, 1, 1, 'mpeg4', ?, "
                         "'[]', 't')", (str(src), json.dumps([{"stream_index": 1, "language": "ger", "codec": "aac", "keep": True},
                                                              {"stream_index": 2, "language": "rus", "codec": "aac",
                                                               "keep": False}])))
        await db.commit()

    async def grew(input_path, keep_audio_indices, duration=0, progress_callback=None, keep_subtitle_indices=None,
                   audio_languages=None, **kw):
        return {"success": True, "output_path": input_path, "space_saved": -2_000_000}
    monkeypatch.setattr(backend.audio, "remux_audio", grew)
    job_id = await JobQueue(test_db).add_job(str(src), "audio", audio_tracks_to_remove=[2])
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())

    await QueueWorker(test_db)._process_job(job)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            assert (await cur.fetchone())[0] == "completed"
        async with db.execute("SELECT reason FROM ignored_files") as cur:
            assert await cur.fetchall() == []  # still to be converted


def test_a_merged_dvd_subtitle_gets_an_srt_copy(monkeypatch):
    monkeypatch.setattr(io, "tesseract_languages", lambda: frozenset({"eng", "ita"}))
    merged = [{"path": "/m/F.idx", "codec": "dvd_subtitle", "language": "eng", "idx_stream": 1},
              {"path": "/m/F.idx", "codec": "dvd_subtitle", "language": "ita", "idx_stream": 0},
              {"path": "/m/F.it.srt", "codec": "subrip", "language": "ita"}]  # Italian has text already
    [planned] = io.srt_plan([], None, merged)
    assert (planned["external_path"], planned["idx_stream"], planned["language"], planned["pack"]) == \
        ("/m/F.idx", 1, "eng", "eng")


@pytest.mark.asyncio
async def test_its_own_stream_is_read(tmp_path, monkeypatch):
    folder = tmp_path / "Film (2004)"
    folder.mkdir()
    (folder / "F.idx").write_text(TWO)
    (folder / "F.sub").write_bytes(b"\x00" * 64)
    read = []

    def fake_ocr(idx, pack):
        read.append(open(idx).read())
        srt = idx[:-4] + ".srt"
        open(srt, "w").write("1\n00:00:01,000 --> 00:00:02,000\nGood morning\n")
        return srt
    monkeypatch.setattr(io, "_subtile_ocr_run", fake_ocr)
    plan = [{"stream_index": None, "external_path": str(folder / "F.idx"), "idx_stream": 1, "codec": "dvd_subtitle",
             "language": "eng", "forced": False, "title": "", "pack": "eng"}]
    workdir = tmp_path / "ocr"
    workdir.mkdir()

    [srt] = await io.image_subs_to_srt("/m/in.mkv", plan, str(workdir))

    assert "id: en" in read[0] and "id: it" not in read[0]
    assert (srt["language"], srt["external_path"], srt["idx_stream"]) == ("eng", str(folder / "F.idx"), 1)
    assert open(srt["path"]).read().strip().endswith("Good morning")
    assert (folder / "F.idx").read_text() == TWO and not (folder / "F.srt").exists()  # the media folder untouched


def test_a_stream_with_an_srt_copy_isnt_merged_too():
    merged = [{"path": "/m/F.idx", "codec": "dvd_subtitle", "language": "eng", "idx_stream": 1},
              {"path": "/m/F.it.srt", "codec": "subrip", "language": "ita"}]
    ocr = [{"path": "/t/x1.srt", "codec": "subrip", "language": "eng", "external_path": "/m/F.idx", "idx_stream": 1}]
    assert io.without_replaced(merged, ocr) == [merged[1]]


@pytest.mark.asyncio
async def test_files_a_cleanup_ignored_are_converted_after_all(test_db):
    """Once at start-up: not one a conversion also came out larger for."""
    import backend.database as database
    async with aiosqlite.connect(test_db) as db:
        await db.executemany("INSERT INTO jobs (file_path, job_type, status, space_saved, created_at) "
                             "VALUES (?, ?, 'completed', ?, 't')",
                             [("/m/Love.mkv", "audio", -2_000_000), ("/m/Larger.mkv", "convert", -5_000_000),
                              ("/m/Larger.mkv", "audio", -1)])
        await db.executemany("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, 't')",
                             [("/m/Love.mkv", "no_savings"), ("/m/Larger.mkv", "no_savings")])
        await db.execute("DELETE FROM settings WHERE key = '_v0_10_0_unignore_cleanups'")
        await db.commit()
    await database.init_db()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path FROM ignored_files") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["/m/Larger.mkv"]
