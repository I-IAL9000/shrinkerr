"""v0.10.0 "safe by default": new installs keep originals 7 days, reject
encodes below VMAF 88 and leave subtitles alone until languages are chosen.
Existing installs keep what they had: the old values are written for any of
these they never saved."""
import aiosqlite
import pytest


async def _settings(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            return dict(await cur.fetchall())


@pytest.fixture
def route(test_db, monkeypatch):
    pytest.importorskip("apscheduler")
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    return settings_route


@pytest.mark.asyncio
async def test_a_new_install_gets_the_safe_defaults(route, test_db):
    await route.seed_v010_defaults()
    got = await _settings(test_db)
    assert (got["backup_original_days"], got["vmaf_min_score"], got["sub_cleanup_enabled"]) == ("7", "88", "false")
    enc = await route.get_encoding_settings()
    assert enc["backup_original_days"] == 7 and enc["vmaf_min_score"] == 88 and enc["sub_cleanup_enabled"] is False


@pytest.mark.asyncio
async def test_an_existing_install_keeps_what_it_had(route, test_db):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/media')")
        await db.execute("INSERT INTO settings (key, value) VALUES ('vmaf_min_score', '92')")  # a saved choice
        await db.commit()
    await route.seed_v010_defaults()
    got = await _settings(test_db)
    assert (got["backup_original_days"], got["vmaf_min_score"], got["sub_cleanup_enabled"]) == ("0", "92", "true")


@pytest.mark.asyncio
async def test_seeding_runs_once(route, test_db):
    await route.seed_v010_defaults()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE settings SET value = '0' WHERE key = 'backup_original_days'")  # user turns backups off
        await db.commit()
    await route.seed_v010_defaults()
    assert (await _settings(test_db))["backup_original_days"] == "0"
