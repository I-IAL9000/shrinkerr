"""v0.9.43: manual track-language override endpoint."""
import pytest
from fastapi import HTTPException
import backend.routes.scan as scan_mod


@pytest.mark.asyncio
async def test_set_track_language_rejects_blank_or_und():
    for bad in ("", "und", "   "):
        with pytest.raises(HTTPException):
            await scan_mod.set_track_language(scan_mod.SetTrackLanguageRequest(
                file_path="/m/x.mkv", track_type="audio", stream_index=1, language=bad))


# v0.9.155 (SC-04, reported on a real title): setting one track's language
# re-derived the native language from the FIRST audio track and re-sorted the
# tracks against it — native German (from TMDB) + a mislabeled track set to
# Russian marked both German tracks for removal and kept the Russian one.
def _seed_row(db_path, native, source):
    import sqlite3
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
               "audio_tracks_json, subtitle_tracks_json, native_language, language_source, "
               "scan_timestamp, removed_from_list) VALUES (?,?,?,?,?,?,?,?,?,0)",
               ("/m/Film (2010)/Film.mkv", 1, "h264", 1, "[]", "[]", native, source, "now"))
    db.commit(); db.close()


def _patch_io(monkeypatch, tracks):
    import backend.scanner, backend.language_detection

    async def fake_probe(path, *a, **kw):
        return {"duration": 100.0, "audio_tracks": [dict(t) for t in tracks], "subtitle_tracks": []}

    async def fake_write(*a, **kw):
        return True

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.language_detection, "apply_track_languages_to_file", fake_write)


TRACKS = [
    {"stream_index": 1, "language": "und", "codec": "ac3", "channels": 6},   # the mislabeled one
    {"stream_index": 2, "language": "ger", "codec": "eac3", "channels": 6},
    {"stream_index": 3, "language": "ger", "codec": "ac3", "channels": 6},
]


@pytest.mark.asyncio
async def test_set_language_keeps_authoritative_native_tracks(test_db, monkeypatch):
    import sqlite3
    _seed_row(test_db, "ger", "api")
    _patch_io(monkeypatch, TRACKS)

    res = await scan_mod.set_track_language(scan_mod.SetTrackLanguageRequest(
        file_path="/m/Film (2010)/Film.mkv", track_type="audio", stream_index=1, language="rus"))

    keep = {t["stream_index"]: t["keep"] for t in res["audio_tracks"]}
    assert keep == {1: False, 2: True, 3: True}
    db = sqlite3.connect(test_db)
    try:
        assert db.execute("SELECT native_language, language_source FROM scan_results").fetchone() == ("ger", "api")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_set_language_does_not_label_a_guess_as_manual(test_db, monkeypatch):
    import sqlite3
    _seed_row(test_db, "und", "heuristic")
    _patch_io(monkeypatch, TRACKS)

    await scan_mod.set_track_language(scan_mod.SetTrackLanguageRequest(
        file_path="/m/Film (2010)/Film.mkv", track_type="audio", stream_index=1, language="rus"))

    db = sqlite3.connect(test_db)
    try:
        assert db.execute("SELECT language_source FROM scan_results").fetchone()[0] == "heuristic"
    finally:
        db.close()
