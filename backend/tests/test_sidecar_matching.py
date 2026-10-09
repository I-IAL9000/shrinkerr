"""v0.10.0 (SC-17): sidecar subtitles matched to the wrong video, or read
without their language."""
from pathlib import Path

import pytest

from backend.scanner import detect_external_subtitles, sidecar_tags


@pytest.mark.parametrize("stem, expected", [
    ("Movie.eng", ("eng", False, False)),
    ("Movie.en.forced", ("eng", True, False)),
    ("Movie.forced.en", ("eng", True, False)),
    ("Movie.English", ("eng", False, False)),
    ("Movie.pt-BR", ("por", False, False)),
    ("Movie.es-419.sdh", ("spa", False, True)),
    ("Movie.zh-Hans", ("chi", False, False)),
    ("Movie.fil", ("fil", False, False)),
    ("Movie.is", ("ice", False, False)),
    ("English", ("eng", False, False)),
    ("Movie", ("und", False, False)),
])
def test_sidecar_tags(stem, expected):
    assert sidecar_tags(stem) == expected


def _touch(folder: Path, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"1\n00:00:00,500 --> 00:00:01,000\nx\n" if n.endswith(".srt") else b"x")
    return [folder / n for n in names]


def test_a_longer_title_is_not_a_prefix_match(tmp_path):
    files = _touch(tmp_path, "Saw.mkv", "Saw II.mkv", "Saw II.eng.srt")
    assert detect_external_subtitles(str(tmp_path / "Saw.mkv"), files) == []
    assert [s["language"] for s in detect_external_subtitles(str(tmp_path / "Saw II.mkv"), files)] == ["eng"]


def test_an_appledouble_file_is_not_a_second_video(tmp_path):
    files = _touch(tmp_path, "Film (2009).mkv", "._Film (2009).mkv", "English.srt")
    assert [s["language"] for s in detect_external_subtitles(str(tmp_path / "Film (2009).mkv"), files)] == ["eng"]


@pytest.mark.asyncio
async def test_deleted_sidecars_leave_the_row(test_db, tmp_path):
    """With every sidecar in a folder deleted, the watcher never looked at
    the folder again and the row kept the gone subtitles."""
    import json
    import os
    import time
    import aiosqlite
    from unittest.mock import AsyncMock, patch
    from backend.watcher import FileWatcher
    media = tmp_path / "Movies"
    [video] = _touch(media / "Film (2009)", "Film (2009).mkv")
    stamp = time.time() - 3600
    os.utime(video, (stamp, stamp))
    gone = str(media / "Film (2009)" / "Film (2009).eng.srt")
    subs = [{"stream_index": -1, "language": "eng", "codec": "subrip", "keep": True, "external": True,
             "external_path": gone}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, subtitle_tracks_json, "
                         "has_external_subs_flag) VALUES (?, 1, '2026-10-08T00:00:00', ?, 1)",
                         (str(video), json.dumps(subs)))
        await db.commit()

    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await FileWatcher(test_db, interval_minutes=5).check_once()

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT subtitle_tracks_json, has_external_subs_flag FROM scan_results") as cur:
            tracks, flag = await cur.fetchone()
    assert json.loads(tracks or "[]") == [] and flag == 0
