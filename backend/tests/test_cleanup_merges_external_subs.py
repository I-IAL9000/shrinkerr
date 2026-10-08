"""v0.10.0: an audio/subtitle cleanup rewrites the file but never merged the
sidecar subtitles beside it, even with "Merge external subtitles" on —
only conversions did.
"""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend.audio import remux_audio
from backend.tests.test_remux_guards import _clip

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


async def _setup(db_path, src, srt, delete_after: bool):
    subs = [{"stream_index": -1, "language": "ice", "codec": "subrip", "keep": True,
             "external": True, "external_path": str(srt)}]
    async with aiosqlite.connect(db_path) as db:
        for key, value in (("merge_external_subs", "true"),
                           ("delete_external_subs_after_merge", "true" if delete_after else "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, subtitle_tracks_json) "
                         "VALUES (?, 1, '2026-10-08T00:00:00', ?)", (str(src), json.dumps(subs)))
        await db.commit()


def _subtitle_languages(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                          "stream_tags=language", "-of", "json", str(path)], capture_output=True, text=True).stdout
    return [s.get("tags", {}).get("language") for s in json.loads(out).get("streams", [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_after", [False, True])
async def test_cleanup_merges_the_sidecar_subtitle(test_db, tmp_path, monkeypatch, delete_after):
    import backend.config
    import backend.scanner as scanner
    monkeypatch.setattr(scanner, "settings", backend.config.settings)  # this test's DB
    monkeypatch.setattr(scanner, "_cleanup_enabled_cache", {})
    src = _clip(tmp_path)
    srt = src.with_suffix(".is.srt")
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHalló\n\n")
    await _setup(test_db, src, srt, delete_after)

    result = await remux_audio(str(src), [1], duration=3.0)

    assert result["success"], result.get("error")
    assert _subtitle_languages(result["output_path"]) == ["ice"]
    assert srt.exists() is not delete_after
