"""v0.10.0 (SC-05): tracks sorted against the wrong original language.

A rescan keeps a stored TMDB/manual native (v0.9.94) but wrote tracks sorted
against the scan's own lookup: when TMDB's language differs from the user's
manual match — the reason it was fixed — the real original-language audio was
marked for removal. And "Fix match" changed the native without re-sorting the
folder's tracks at all.
"""
import json

import aiosqlite
import pytest

from backend.models import AudioTrack, ScannedFile


def _tracks(**keep):
    return [{"stream_index": i + 1, "language": lang, "codec": "ac3", "channels": 6, "keep": k}
            for i, (lang, k) in enumerate(keep.items())]


async def _setup(db_path, path, tracks, native, source):
    from backend.scanner import invalidate_sub_settings_cache
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('audio_cleanup_enabled', 'true')")
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, audio_tracks_json, native_language, "
            "language_source, scan_timestamp) VALUES (?, 1, 'h264', ?, ?, ?, '2026-10-08T00:00:00')",
            (path, json.dumps(tracks), native, source))
        await db.commit()
    invalidate_sub_settings_cache()


async def _row(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM scan_results WHERE file_path = ?", (path,)) as cur:
            row = await cur.fetchone()
    keep = {t["language"]: t["keep"] for t in json.loads(row["audio_tracks_json"])}
    return row, keep


@pytest.mark.asyncio
async def test_rescan_sorts_tracks_against_the_users_match(test_db):
    from backend.routes.scan import _write_batch_sync
    path = "/media/Korean Film (2019)/film.mkv"
    await _setup(test_db, path, _tracks(kor=True, eng=False), "kor", "manual")
    # The rescan's TMDB lookup says English, and sorted the tracks that way.
    scanned = ScannedFile(
        file_path=path, file_name="film.mkv", folder_name="Korean Film (2019)",
        file_size=1, file_size_gb=0.0, video_codec="h264", needs_conversion=True,
        audio_tracks=[AudioTrack(**t) for t in _tracks(kor=False, eng=True)],
        native_language="eng", language_source="api", has_removable_tracks=True,
        estimated_savings_bytes=0, estimated_savings_gb=0.0,
    )
    _write_batch_sync(test_db, [scanned], "2026-10-09T00:00:00")

    row, keep = await _row(test_db, path)
    assert (row["native_language"], row["language_source"]) == ("kor", "manual")
    assert keep == {"kor": True, "eng": False}
    assert row["has_removable_tracks_flag"] == 1


@pytest.mark.asyncio
async def test_fix_match_re_sorts_the_folders_tracks(test_db, monkeypatch):
    import backend.routes.posters as posters
    import backend.routes.scan as scan_route
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    folder = "/media/Korean Film (2019)"
    path = folder + "/film.mkv"
    # Guessed English from track order: Korean marked for removal.
    await _setup(test_db, path, _tracks(eng=True, kor=False), "eng", "heuristic")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tmdb_api_key', 'k')")
        await db.commit()

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"title": "Korean Film", "release_date": "2019-01-01", "original_language": "ko"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def no_image(*a, **k):
        return None

    monkeypatch.setattr(posters, "_download_image", no_image)
    await posters.override_poster(posters.OverrideRequest(folder_path=folder, tmdb_id=1, media_type="movie"))

    row, keep = await _row(test_db, path)
    assert (row["native_language"], row["language_source"]) == ("kor", "tmdb-manual")
    assert keep == {"eng": False, "kor": True}
