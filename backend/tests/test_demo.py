"""Demo mode (v0.10.0): SHRINKERR_DEMO=1 serves a made-up library,
read-only, with nobody signing in."""
import aiosqlite
import pytest
import pytest_asyncio

from backend import demo


def test_what_the_demo_refuses():
    for method, path in (("POST", "/api/jobs/start"), ("PUT", "/api/settings/encoding"), ("DELETE", "/api/views/1"),
                         ("POST", "/api/rules/"), ("PATCH", "/api/x"), ("POST", "/api/auth/setup"),
                         ("GET", "/api/settings/api-key"), ("GET", "/api/settings/backup/download/a.zip"),
                         ("GET", "/api/settings/nzbget-script"), ("GET", "/api/settings/nzbget-config")):
        assert demo.blocked(method, path), (method, path)
    for method, path in (("GET", "/api/jobs/"), ("POST", "/api/jobs/estimate"), ("POST", "/api/rules/test"),
                         ("POST", "/api/auth/ws-ticket"), ("POST", "/api/scan/search"), ("HEAD", "/api/health"),
                         ("POST", "/assets/x")):
        assert not demo.blocked(method, path), (method, path)


def test_the_library_is_made_up_and_the_same_every_time():
    files = demo.library()
    assert files == demo.library() and len(files) > 200
    assert all(f["path"].startswith("/demo/") and f["audio"] and f["subs"] for f in files)
    codecs = {f["codec"] for f in files}
    assert {"h264", "hevc"} <= codecs and any(f["hdr"] == "dv8" for f in files)
    assert all(f["codec"] == "hevc" for f in files if f["hdr"])


@pytest.mark.asyncio
async def test_seeding(test_db):
    n = await demo.seed(test_db)
    assert n > 200 and await demo.seed(test_db) == 0  # once
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT COUNT(*), SUM(needs_conversion), SUM(converted), SUM(has_removable_tracks_flag), "
                              "SUM(video_conv_savings_bytes > 0) FROM scan_results") as cur:
            files, to_convert, converted, cleanup, estimated = await cur.fetchone()
        async with db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cur:
            jobs = dict(await cur.fetchall())
        async with db.execute("SELECT value FROM settings WHERE key = 'queue_run_state'") as cur:
            run_state = (await cur.fetchone())[0]
    assert files == n and to_convert > 20 and converted > 20 and cleanup > 20 and estimated == to_convert
    assert jobs["completed"] == converted and jobs["pending"] == 24 and jobs["failed"] == 1
    assert run_state == "paused"


@pytest_asyncio.fixture
async def demo_app(test_db, monkeypatch):
    from backend import main as main_module
    import backend.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    monkeypatch.setattr(demo, "ENABLED", True)
    async with aiosqlite.connect(test_db) as db:  # a key is set: the demo still needs none
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_key', 'K')")
        await db.commit()
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=main_module.app, client=("8.8.4.4", 5000)),
                           base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_the_demo_app(demo_app):
    check = (await demo_app.get("/api/auth/check")).json()
    assert (check["authenticated"], check["setup_required"], check["demo"]) == (True, False, True)
    assert (await demo_app.get("/api/views")).status_code == 200
    r = await demo_app.post("/api/views", json={"name": "x", "filter": "x264"})
    assert r.status_code == 403 and r.json()["code"] == "demo.readOnly"
    assert (await demo_app.post("/api/auth/setup", json={"method": "external"})).status_code == 403
