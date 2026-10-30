"""v0.9.159: an API key with no username and password locked people out.

With an API key set, the whole API needs it — browsers too — unless a
password login is on. The sign-in page asked for a username and password
that didn't exist, so it now learns whether there is one (`password_login`).
A regenerated key also took up to a minute to replace the old one (the auth
settings are cached).
"""
import aiosqlite
import pytest
import pytest_asyncio


async def _set(db_path: str, key: str, value: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()


@pytest_asyncio.fixture
async def client(test_db, monkeypatch):
    pytest.importorskip("apscheduler")
    from httpx import AsyncClient, ASGITransport
    from backend import main as main_module
    import backend.database as db_module
    import backend.routes.settings as settings_route
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_sign_in_asks_for_the_api_key_when_there_is_no_password(client, test_db):
    await _set(test_db, "api_key", "THE-KEY")
    r = await client.get("/api/auth/check")
    assert {k: r.json()[k] for k in ("auth_required", "authenticated", "method", "password_login")} == {
        "auth_required": True, "authenticated": False, "method": None, "password_login": False}

    r = await client.get("/api/auth/check", headers={"X-Api-Key": "THE-KEY"})
    assert r.json()["authenticated"] is True and r.json()["password_login"] is False


@pytest.mark.asyncio
async def test_sign_in_asks_for_the_password_when_there_is_one(client, test_db):
    from backend import main as main_module
    await _set(test_db, "api_key", "THE-KEY")
    await _set(test_db, "auth_enabled", "true")
    await _set(test_db, "auth_username", "admin")
    await _set(test_db, "auth_password_hash", main_module._hash_password("correct horse"))
    await _set(test_db, "session_secret", "s" * 64)
    r = await client.get("/api/auth/check")
    assert r.json()["authenticated"] is False and r.json()["password_login"] is True


@pytest.mark.asyncio
async def test_no_sign_in_needed_says_so(client):
    r = await client.get("/api/auth/check")
    assert {k: r.json()[k] for k in ("auth_required", "authenticated", "method", "password_login")} == {
        "auth_required": False, "authenticated": True, "method": None, "password_login": False}


@pytest.mark.asyncio
async def test_a_regenerated_key_replaces_the_old_one_at_once(client, test_db):
    await _set(test_db, "api_key", "OLD-KEY")
    r = await client.get("/api/settings/api-key", headers={"X-Api-Key": "OLD-KEY"})
    assert r.status_code == 200  # the old key is cached now

    r = await client.post("/api/settings/api-key/regenerate", headers={"X-Api-Key": "OLD-KEY"})
    new_key = r.json()["api_key"]

    r = await client.get("/api/settings/api-key", headers={"X-Api-Key": "OLD-KEY"})
    assert r.status_code == 401
    r = await client.get("/api/settings/api-key", headers={"X-Api-Key": new_key})
    assert r.status_code == 200 and r.json()["api_key"] == new_key


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key,supplied,allowed", [
    ("", "", True),                 # nothing set up: open, like the HTTP API
    ("THE-KEY", "", False),         # an API key alone used to leave both WebSockets open
    ("THE-KEY", "WRONG", False),
    ("THE-KEY", "THE-KEY", False),  # 0.10.0: one-time tickets only (test_routes)
])
async def test_websockets_follow_the_api_key_like_the_http_api(test_db, monkeypatch, api_key, supplied, allowed):
    """/ws (live job progress) and /ws/logs (the server log) were open
    whenever password login was off, even with an API key set."""
    from types import SimpleNamespace
    from backend import main as main_module
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    await _set(test_db, "api_key", api_key)
    ws = SimpleNamespace(query_params={"api_key": supplied} if supplied else {}, cookies={})
    assert await main_module._check_ws_auth(ws) is allowed
