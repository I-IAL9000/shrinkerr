"""v0.9.158 hotfix: originals were lost.

- With "move to the trash", an original the trash couldn't take (no trash
  folder on the share, a cross-device move…) was deleted for good — a
  converted file, ISO or disc alike. It's kept in the backup folder now.
- Audio / subtitle cleanups ignored "keep originals for N days" (only ever
  trashing or deleting), deleted when the trash failed, and reported a
  failure when the original couldn't be removed — so Retry cleaned the
  already-cleaned file again, by stream numbers that no longer match.
"""
import shutil
import subprocess
import sys
import types

import aiosqlite
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


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


async def _settings(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        await db.commit()


@pytest.mark.asyncio
async def test_a_conversion_keeps_the_original_when_the_trash_fails(tmp_path, monkeypatch):
    from backend.converter import convert_file
    _trash(monkeypatch, works=False)
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(src)], check=True)
    original = src.read_bytes()

    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast",
                                pre_settings={"trash_original_after_conversion": True, "backup_original_days": 0,
                                              "vmaf_analysis_enabled": False})

    assert result["success"], result.get("error")
    kept = tmp_path / ".shrinkerr_backup" / src.name
    assert result.get("backup_path") == str(kept) and kept.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["keep", "trash_fails", "trash_works"])
async def test_cleanups_follow_the_originals_setting(test_db, tmp_path, monkeypatch, mode):
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    if mode == "keep":
        await _settings(test_db, backup_original_days=7)
    else:
        await _settings(test_db, trash_original_after_conversion="true")
        _trash(monkeypatch, works=mode == "trash_works")
    src = _clip(tmp_path)
    original = src.read_bytes()

    result = await remux_audio(str(src), [1], duration=3.0)

    assert result["success"] and src.read_bytes() != original
    if mode == "trash_works":
        assert result["backup_path"] is None
    else:
        assert result["backup_path"] == str(tmp_path / ".shrinkerr_backup" / src.name)
        assert (tmp_path / ".shrinkerr_backup" / src.name).read_bytes() == original


@pytest.mark.asyncio
async def test_a_cleanup_succeeds_when_the_original_cant_be_removed(test_db, tmp_path, monkeypatch):
    from pathlib import Path
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    original = src.read_bytes()
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if ".shrinkerr-replacing" in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)

    result = await remux_audio(str(src), [1], duration=3.0)
    assert result["success"] is True
    assert (tmp_path / ".shrinkerr-replacing" / src.name).read_bytes() == original


@pytest.mark.asyncio
async def test_the_queue_keeps_a_cleanups_backup(test_db, tmp_path, monkeypatch):
    import backend.audio
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "audio", audio_tracks_to_remove=[2])
    backup = str(tmp_path / ".shrinkerr_backup" / src.name)

    async def fake_remux(input_path, keep_audio_indices, *a, **kw):
        return {"success": True, "output_path": input_path, "space_saved": 5, "backup_path": backup}
    monkeypatch.setattr(backend.audio, "remux_audio", fake_remux)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(test_db)._worker_task(job)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status, backup_path FROM jobs WHERE id = ?", (job_id,)) as cur:
            assert tuple(await cur.fetchone()) == ("completed", backup)


@pytest.mark.asyncio
async def test_a_second_backup_never_replaces_the_first(test_db, tmp_path):
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    await _settings(test_db, backup_original_days=7)
    src = _clip(tmp_path)
    first = src.read_bytes()
    await remux_audio(str(src), [1, 2], duration=3.0)
    second = src.read_bytes()
    await remux_audio(str(src), [1], duration=3.0)
    backups = tmp_path / ".shrinkerr_backup"
    assert (backups / src.name).read_bytes() == first
    assert (backups / f"{src.stem}.1{src.suffix}").read_bytes() == second
