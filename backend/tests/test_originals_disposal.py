"""What happens to originals (v0.10.0).

- With "move to the trash", an original the trash couldn't take (no trash
  folder on the share, a cross-device move…) was deleted instead — and so
  was a converted disc. It's kept in the backup folder now.
- Track cleanups (audio jobs) only ever trashed or deleted the original:
  "keep originals for N days", a rule's originals policy and Undo didn't
  cover the tracks they removed. On a remote worker they read the worker's
  own (empty) settings and always deleted.
"""
import shutil
import sys
import types

import pytest

import backend.converter as converter
from backend import worker_mode


def _trash(monkeypatch, works: bool):
    trashed = []

    def send2trash(path):
        if not works:
            raise OSError(18, "Invalid cross-device link", path)
        trashed.append(path)
        import os
        os.remove(path)
    monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=send2trash))
    return trashed


def _original(tmp_path, name="Movie (2009).mkv"):
    folder = tmp_path / "Movie (2009)"
    folder.mkdir()
    src = folder / name
    src.write_bytes(b"original")
    return folder, src


@pytest.mark.asyncio
async def test_kept_trashed_or_deleted(tmp_path, monkeypatch):
    trashed = _trash(monkeypatch, works=True)
    folder, src = _original(tmp_path)
    kept = await converter.dispose_original(src, src.name, folder, {"backup_original_days": 7})
    assert kept == str(folder / ".shrinkerr_backup" / src.name) and not src.exists()

    (tmp_path / "x").mkdir()
    folder2, src2 = _original(tmp_path / "x")
    assert await converter.dispose_original(src2, src2.name, folder2, {"trash_original_after_conversion": True}) is None
    assert trashed == [str(src2)]

    src2.write_bytes(b"again")
    assert await converter.dispose_original(src2, src2.name, folder2, {"trash_original_after_conversion": "false"}) is None
    assert not src2.exists() and trashed == [str(src2)]  # a "false" string isn't "trash"


@pytest.mark.asyncio
async def test_a_trash_failure_keeps_the_original(tmp_path, monkeypatch):
    _trash(monkeypatch, works=False)
    folder, src = _original(tmp_path)
    kept = await converter.dispose_original(src, src.name, folder, {"trash_original_after_conversion": True})
    assert kept == str(folder / ".shrinkerr_backup" / src.name)
    assert (folder / ".shrinkerr_backup" / src.name).read_bytes() == b"original"

    custom = tmp_path / "backups"
    folder2 = tmp_path / "Other"
    folder2.mkdir()
    src2 = folder2 / "Other.mkv"
    src2.write_bytes(b"other")
    kept = await converter.dispose_original(src2, src2.name, folder2, {"trash_original_after_conversion": True,
                                                                      "backup_folder": str(custom)})
    assert kept == str(custom / "Other" / "Other.mkv") and (custom / "Other" / "Other.mkv").exists()


@pytest.mark.asyncio
async def test_a_trash_failure_keeps_a_converted_disc(tmp_path, monkeypatch):
    _trash(monkeypatch, works=False)
    movie = tmp_path / "Movie (2009)"
    (movie / "BDMV" / "STREAM").mkdir(parents=True)
    marker = movie / "BDMV" / "index.bdmv"
    marker.write_bytes(b"disc")
    (movie / "CERTIFICATE").mkdir()
    (movie / "CERTIFICATE" / "id.bdmv").write_bytes(b"cert")

    backup = await converter._dispose_disc_source(marker, movie, "bdmv", 0, True, "")

    slot = movie / ".shrinkerr_backup"
    assert backup == str(slot / "BDMV")
    assert (slot / "BDMV" / "index.bdmv").read_bytes() == b"disc"
    assert (slot / "CERTIFICATE" / "id.bdmv").exists()
    assert not (movie / "BDMV").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.parametrize("trash_works", [True, False])
async def test_cleanups_follow_the_originals_setting(test_db, tmp_path, monkeypatch, trash_works):
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    original = src.read_bytes()

    result = await remux_audio(str(src), [1], duration=3.0, originals={"backup_original_days": 3})
    assert result["success"] and result["backup_path"] == str(tmp_path / ".shrinkerr_backup" / src.name)
    assert (tmp_path / ".shrinkerr_backup" / src.name).read_bytes() == original

    _trash(monkeypatch, works=trash_works)
    again = src.read_bytes()
    result = await remux_audio(str(src), [1], duration=3.0, originals={"trash_original_after_conversion": True})
    assert result["success"]
    if trash_works:
        assert result["backup_path"] is None
    else:  # kept in the backup folder, beside the first backup
        assert result["backup_path"] == str(tmp_path / ".shrinkerr_backup" / f"{src.stem}.1{src.suffix}")
        assert (tmp_path / ".shrinkerr_backup" / f"{src.stem}.1{src.suffix}").read_bytes() == again


@pytest.mark.asyncio
async def test_a_workers_cleanup_follows_the_servers_setting(tmp_path, monkeypatch):
    import backend.audio
    import backend.scanner
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient, _job
    src = tmp_path / "Show - S01E01.mkv"
    src.write_bytes(b"x")
    seen = []

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_remux(input_path, keep_audio_indices, duration=0, progress_callback=None,
                         keep_subtitle_indices=None, audio_languages=None, **kw):
        seen.append(kw.get("originals"))
        return {"success": True, "output_path": input_path, "space_saved": 10, "backup_path": "/backups/a.mkv"}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.audio, "remux_audio", fake_remux)
    client = FakeClient()
    job = {**_job(src), "backup_original_days": 14, "trash_original_after_conversion": False,
           "backup_folder": "/backups"}

    await worker_mode.execute_job(client, "node-1", job, ["libx265"])

    assert seen == [{"backup_original_days": 14, "trash_original_after_conversion": False, "backup_folder": "/backups"}]
    assert client.completed[0][0] is True and client.completed[0][1]["backup_path"] == "/backups/a.mkv"


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_undo_restores_a_cleanups_original(test_db, tmp_path):
    import aiosqlite
    from backend.audio import remux_audio
    from backend.queue import JobQueue
    from backend.routes.jobs import undo_conversion
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    original = src.read_bytes()
    result = await remux_audio(str(src), [1], duration=3.0, originals={"backup_original_days": 7})
    assert src.read_bytes() != original

    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "audio")
    await queue.update_backup_path(job_id, result["backup_path"], str(src))
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET status = 'completed' WHERE id = ?", (job_id,))
        await db.commit()

    await undo_conversion(job_id)
    assert src.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_the_queue_keeps_a_cleanups_backup_and_rule(test_db, tmp_path, monkeypatch):
    import json
    import aiosqlite
    import backend.audio
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["eng", "spa"])
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "audio", audio_tracks_to_remove=[2])
    async with aiosqlite.connect(test_db) as db:  # a rule said: keep this one's original for 30 days
        await db.execute("UPDATE jobs SET originals_policy = 'keep', originals_days = 30 WHERE id = ?", (job_id,))
        await db.commit()
    seen = []

    async def fake_remux(input_path, keep_audio_indices, **kw):
        seen.append(kw["originals"])
        await kw["on_output_placed"]()
        return {"success": True, "output_path": input_path, "space_saved": 5,
                "backup_path": str(tmp_path / ".shrinkerr_backup" / src.name)}

    monkeypatch.setattr(backend.audio, "remux_audio", fake_remux)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(test_db)._worker_task(job)

    assert seen[0]["backup_original_days"] == 30 and seen[0]["trash_original_after_conversion"] is False
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status, backup_path FROM jobs WHERE id = ?", (job_id,)) as cur:
            status, backup = await cur.fetchone()
    assert (status, backup) == ("completed", str(tmp_path / ".shrinkerr_backup" / src.name))
