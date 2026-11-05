"""v0.10.0: the post-conversion script runs after finished jobs only, so
SHRINKERR_ERROR is empty — it read "None" when the result's error was None."""
import json
import shutil

import aiosqlite
import pytest

from backend import queue as queue_mod


@pytest.mark.asyncio
async def test_the_script_sees_no_error_text(test_db, tmp_path, monkeypatch):
    from backend.config import settings as app_settings
    out = tmp_path / "env.txt"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s|%s|%s" "$SHRINKERR_STATUS" "$SHRINKERR_ERROR" "$SHRINKERR_SPACE_SAVED" > {out}\n')
    script.chmod(0o755)
    monkeypatch.setattr(app_settings, "db_path", test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('post_conversion_script', ?)", (str(script),))
        await db.commit()

    await queue_mod._run_post_conversion_script(1, "/m/a.mkv", "/m/a.mkv", {"success": True, "error": None,
                                                                             "space_saved": 5}, {})
    assert out.read_text() == "completed||5"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_a_cleanup_with_nothing_left_to_do_still_finishes(test_db, tmp_path, monkeypatch):
    """v0.10.0: a clean-up whose file needed nothing by the time it ran (its
    tracks already as wanted) left `result` unset — "Post-conversion script
    wrapper failed: cannot access local variable 'result'": the script didn't
    run and Activity had no "completed" entry for it."""
    import sys
    import backend.file_events  # noqa: F401 — loaded first, so its DB_PATH copy is redirected below
    from backend.config import settings as app_settings
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_remux_guards import _clip
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(app_settings, "db_path", test_db)
    src = _clip(tmp_path)
    out = tmp_path / "env.txt"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s|%s" "$SHRINKERR_JOB_TYPE" "$SHRINKERR_STATUS" > {out}\n')
    script.chmod(0o755)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('post_conversion_script', ?)", (str(script),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, audio_tracks_json, subtitle_tracks_json, "
                         "scan_timestamp) VALUES (?, 1, ?, '[]', 't')",
                         (str(src), json.dumps([{"stream_index": 1, "language": "ger", "codec": "aac", "keep": True},
                                                {"stream_index": 2, "language": "rus", "codec": "aac", "keep": True}])))
        await db.commit()
    job_id = await JobQueue(test_db).add_job(str(src), "audio")
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())

    await QueueWorker(test_db)._process_job(job)

    assert out.read_text() == "audio|completed"
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT event_type FROM file_events") as cur:  # (the job renames AC3 → AAC)
            assert "completed" in [r[0] for r in await cur.fetchall()]
