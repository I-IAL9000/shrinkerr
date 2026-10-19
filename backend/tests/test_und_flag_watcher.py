"""The watcher keeps has_und_tracks_flag (the Unknown language filter) in step
with the tracks it writes (v0.10.0). Its subtitle sync and re-probes wrote
the tracks and left the flag: Diplomacy (2014) — French audio, French and
English subtitles — stayed under Unknown language."""
import json

import aiosqlite
import pytest

from backend.watcher import FileWatcher

SRT = "1\n00:00:01,000 --> 00:00:02,000\nhi\n"


async def _row(db_path, video, subs, flag):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, native_language, audio_tracks_json, subtitle_tracks_json, "
            "has_und_tracks_flag, scan_timestamp) VALUES (?, 1, 'fre', ?, ?, ?, '2026-01-01')",
            (video, json.dumps([{"stream_index": 1, "language": "fre", "codec": "dts"}]), json.dumps(subs), flag))
        await db.commit()


async def _flag(db_path, video):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT has_und_tracks_flag FROM scan_results WHERE file_path = ?", (video,)) as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_a_subtitle_named_with_its_language_clears_it(test_db, tmp_path):
    folder = tmp_path / "Diplomacy (2014)"
    folder.mkdir()
    video = str(folder / "Diplomacy.mkv")
    (folder / "Diplomacy.eng.srt").write_text(SRT)  # was Diplomacy.srt: und
    await _row(test_db, video, [{"stream_index": -1, "language": "und", "codec": "subrip", "external": True,
                                 "external_path": str(folder / "Diplomacy.srt")}], 1)
    siblings = list(folder.iterdir())
    assert await FileWatcher(test_db)._reconcile_external_subs({str(folder): siblings}, {video}) == 1
    assert await _flag(test_db, video) == 0


@pytest.mark.asyncio
async def test_a_subtitle_without_a_language_sets_it(test_db, tmp_path):
    folder = tmp_path / "Film (2001)"
    folder.mkdir()
    video = str(folder / "Film.mkv")
    (folder / "Film.srt").write_text(SRT)
    await _row(test_db, video, [], 0)
    siblings = list(folder.iterdir())
    assert await FileWatcher(test_db)._reconcile_external_subs({str(folder): siblings}, {video}) == 1
    assert await _flag(test_db, video) == 1
