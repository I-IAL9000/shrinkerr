"""v0.10.0: the post-conversion script runs after finished jobs only, so
SHRINKERR_ERROR is empty — it read "None" when the result's error was None."""
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
