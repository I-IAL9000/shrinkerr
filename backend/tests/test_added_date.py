"""A file Shrinkerr rewrites keeps its Added date (v0.10.0).

The Scanner dates a file by its modified time. A clean-up or a conversion
writes a new file, and the next scan took the time of that write as the day
the file was added: 34 audio clean-ups followed by a folder rescan all showed
as newly added (top of Newest, in Added: last 7 days)."""
import os

import aiosqlite
import pytest

OLD = 1_600_000_000.0           # the day the film arrived (2020)
REWRITTEN = OLD + 86400 * 1000  # Shrinkerr's clean-up or conversion of it
SCANNED = "2026-11-03T21:00:00+00:00"


def _file(path, mtime):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _scan(db_path, path):
    """A rescan of `path`: the scanner reads its modified time from disk."""
    from backend.models import AudioTrack, ScannedFile
    from backend.routes.scan import _write_batch_sync
    tracks = [AudioTrack(stream_index=i, language=lang, codec="aac", channels=2)
              for i, lang in ((1, "eng"), (2, "spa"))]
    _write_batch_sync(db_path, [ScannedFile(
        file_path=str(path), file_name=path.name, folder_name="Film", file_size=1, file_size_gb=0.0,
        video_codec="hevc", needs_conversion=False, audio_tracks=tracks, native_language="eng",
        has_removable_tracks=False, estimated_savings_bytes=0, estimated_savings_gb=0.0,
        file_mtime=os.stat(path).st_mtime)], SCANNED)


async def _added(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_mtime FROM scan_results WHERE file_path = ?", (str(path),)) as cur:
            row = await cur.fetchone()
    return row and row[0]


def _probe(monkeypatch):
    import backend.scanner

    async def probe(path, *a, **kw):
        if not os.path.exists(path):
            return None
        return {"duration": 100.0, "file_size": 1, "video_codec": "hevc",
                "audio_tracks": [{"stream_index": 1, "language": "eng", "codec": "aac"},
                                 {"stream_index": 2, "language": "spa", "codec": "aac"}],
                "subtitle_tracks": []}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)


@pytest.mark.asyncio
async def test_a_rescan_keeps_the_added_date_of_a_file_shrinkerr_rewrote(test_db, tmp_path):
    from backend.database import note_written
    film = _file(tmp_path / "Film.mkv", OLD)
    _scan(test_db, film)
    _file(film, REWRITTEN)
    await note_written(str(film), test_db)

    _scan(test_db, film)

    assert await _added(test_db, film) == OLD


@pytest.mark.asyncio
async def test_a_file_replaced_since_is_dated_by_the_replacement(test_db, tmp_path):
    from backend.database import note_written
    film = _file(tmp_path / "Film.mkv", OLD)
    _scan(test_db, film)
    _file(film, REWRITTEN)
    await note_written(str(film), test_db)
    _file(film, REWRITTEN + 3600)  # Radarr's upgrade, under the same name

    _scan(test_db, film)

    assert await _added(test_db, film) == REWRITTEN + 3600


@pytest.mark.asyncio
async def test_a_cleanup_notes_the_file_it_wrote(test_db, tmp_path, monkeypatch):
    import sys

    import backend.audio
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    film = _file(tmp_path / "Film.mkv", OLD)
    _scan(test_db, film)
    _probe(monkeypatch)

    async def remux(input_path, keep_audio_indices, **kwargs):
        _file(film, REWRITTEN)
        return {"success": True, "output_path": input_path, "space_saved": 0}
    monkeypatch.setattr(backend.audio, "remux_audio", remux)
    job_id = await JobQueue(test_db).add_job(str(film), "audio", audio_tracks_to_remove=[2])
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())
    await QueueWorker(test_db)._process_job(job)

    _scan(test_db, film)

    assert await _added(test_db, film) == OLD


@pytest.mark.asyncio
async def test_a_conversion_notes_its_output(test_db, tmp_path, monkeypatch):
    """The same for a remote node's conversion (and a local one's first update)."""
    from backend.queue import JobQueue, refresh_converted_scan_row
    src = _file(tmp_path / "Film x264.mkv", OLD)
    _scan(test_db, src)
    _probe(monkeypatch)
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="nvenc")
    out = _file(tmp_path / "Film x265.mkv", REWRITTEN)
    src.unlink()

    await refresh_converted_scan_row(test_db, job_id, str(src), str(out))
    _scan(test_db, out)

    assert await _added(test_db, out) == OLD


@pytest.mark.asyncio
async def test_undo_notes_the_original_it_put_back(test_db, tmp_path):
    from backend.routes.jobs import undo_conversion
    original = tmp_path / "Film x264.mkv"
    converted = _file(tmp_path / "Film x265.mkv", REWRITTEN)
    backups = tmp_path / ".shrinkerr_backup"
    backups.mkdir()
    backup = _file(backups / original.name, REWRITTEN)  # dated by the backup move
    _scan(test_db, converted)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE scan_results SET file_mtime = ?", (OLD,))
        cur = await db.execute(
            "INSERT INTO jobs (file_path, original_file_path, backup_path, job_type, status, created_at) "
            "VALUES (?, ?, ?, 'convert', 'completed', '2026-11-01T00:00:00')",
            (str(converted), str(original), str(backup)))
        await db.commit()

    await undo_conversion(cur.lastrowid)
    _scan(test_db, original)

    assert await _added(test_db, original) == OLD


def _tags_written(monkeypatch, film):
    import backend.language_detection

    async def write(*a, **kw):  # mkvpropedit edits the file in place
        _file(film, REWRITTEN)
        return True
    monkeypatch.setattr(backend.language_detection, "apply_track_languages_to_file", write)


@pytest.mark.asyncio
async def test_a_language_set_by_hand_and_written_into_the_file_is_noted(test_db, tmp_path, monkeypatch):
    import backend.routes.scan as scan_mod
    film = _file(tmp_path / "Film.mkv", OLD)
    _scan(test_db, film)
    _probe(monkeypatch)
    _tags_written(monkeypatch, film)

    await scan_mod.set_track_language(scan_mod.SetTrackLanguageRequest(
        file_path=str(film), track_type="audio", stream_index=1, language="ice"))
    _scan(test_db, film)

    assert await _added(test_db, film) == OLD


@pytest.mark.asyncio
async def test_detected_languages_written_into_the_file_are_noted(test_db, tmp_path, monkeypatch):
    import backend.routes.scan as scan_mod
    import backend.scanner
    monkeypatch.setattr(scan_mod, "DB_PATH", test_db)
    film = _file(tmp_path / "Film.mkv", OLD)
    _scan(test_db, film)

    async def probe(path, *a, **kw):  # an und track whose title names its language
        return {"duration": 100.0, "subtitle_tracks": [], "audio_tracks": [
            {"stream_index": 1, "language": "und", "codec": "aac", "channels": 2, "title": "English"}]}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    _tags_written(monkeypatch, film)

    await scan_mod.detect_languages(scan_mod.DetectLanguagesRequest(file_path=str(film)), notify_plex=False)
    _scan(test_db, film)

    assert await _added(test_db, film) == OLD


async def _job_done(db_path, path, completed_at):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, completed_at) "
                         "VALUES (?, 'audio', 'completed', ?, ?)", (str(path), completed_at, completed_at))
        await db.commit()


@pytest.mark.asyncio
async def test_files_rewritten_before_writes_were_noted_keep_their_dates(test_db, tmp_path):
    from datetime import datetime, timezone

    from backend.database import note_earlier_writes_once

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    cleaned = _file(tmp_path / "Cleaned.mkv", OLD)
    replaced = _file(tmp_path / "Replaced.mkv", OLD)
    for path in (cleaned, replaced):
        _scan(test_db, path)
        async with aiosqlite.connect(test_db) as db:  # scanned before the job
            await db.execute("UPDATE scan_results SET scan_timestamp = ?", (iso(OLD + 60),))
            await db.commit()
        _file(path, REWRITTEN)
        await _job_done(test_db, path, iso(REWRITTEN + 120))
    _file(replaced, REWRITTEN + 86400)  # replaced the day after the clean-up

    assert await note_earlier_writes_once() == 1
    _scan(test_db, cleaned)
    _scan(test_db, replaced)

    assert await _added(test_db, cleaned) == OLD
    assert await _added(test_db, replaced) == REWRITTEN + 86400
    assert await note_earlier_writes_once() == 0  # once
