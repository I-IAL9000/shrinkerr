"""Sign-in is required (v0.10.0), as in Sonarr v4: an install without it
shows the setup screen; sign-in is the sign-in page or a reverse proxy
("external"), for everyone or not for local addresses; API keys keep
working, and the integration endpoints still need one."""
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from backend import auth_policy

LOCAL, REMOTE = ("192.168.1.5", 50000), ("8.8.4.4", 50000)


def _req(host, **headers):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


def test_local_addresses():
    for host in ("127.0.0.1", "192.168.1.10", "10.0.0.2", "172.16.4.1", "::1", "fd00::5", "::ffff:192.168.1.2"):
        assert auth_policy.is_local(_req(host)), host
    for host in ("8.8.8.8", "100.64.1.1", "203.0.113.5", "2001:4860::8888", "", "not-an-ip", "0.0.0.0"):
        assert not auth_policy.is_local(_req(host)), host
    for header in ("x-forwarded-for", "x-real-ip", "forwarded"):  # through a proxy: not local
        assert not auth_policy.is_local(_req("127.0.0.1", **{header: "8.8.4.4"})), header


def test_the_method_and_who_skips_sign_in():
    assert auth_policy.method({}) == "" and auth_policy.method({"auth_enabled": True}) == "forms"  # before 0.10
    assert auth_policy.method({"auth_method": "external", "auth_enabled": True}) == "external"
    assert auth_policy.method({"auth_method": "bogus"}) == ""
    local, remote = _req("192.168.1.5"), _req("8.8.4.4")
    forms = {"auth_method": "forms", "auth_required": "enabled"}
    assert auth_policy.bypass(forms, local) is None
    assert auth_policy.bypass({**forms, "auth_required": "local_disabled"}, local) == "local"
    assert auth_policy.bypass({**forms, "auth_required": "local_disabled"}, remote) is None
    assert auth_policy.bypass({"auth_method": "external"}, remote) == "external"
    assert auth_policy.bypass({"auth_required": "local_disabled"}, local) is None  # not set up


@pytest_asyncio.fixture
async def app(test_db, monkeypatch):
    from backend import main as main_module
    import backend.database as db_module
    import backend.routes.settings as settings_route
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('session_secret', 's3cret')")
        await db.commit()
    return main_module


def _client(main_module, addr):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=main_module.app, client=addr), base_url="http://test")


async def _set(test_db, main_module, **values):
    async with aiosqlite.connect(test_db) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    main_module._auth_cache["checked_at"] = 0


@pytest.mark.asyncio
async def test_setting_up_the_sign_in_page(app):
    async with _client(app, REMOTE) as c:
        check = (await c.get("/api/auth/check")).json()
        assert (check["setup_required"], check["authenticated"]) == (True, True)  # open until set up
        r = await c.post("/api/auth/setup", json={"method": "forms", "username": "me", "password": "short"})
        assert r.status_code == 400 and r.json()["code"] == "auth.passwordTooShort"
        r = await c.post("/api/auth/setup", json={"method": "forms", "username": " ", "password": "long enough"})
        assert r.json()["code"] == "auth.usernameRequired"
        r = await c.post("/api/auth/setup", json={"method": "none"})
        assert r.json()["code"] == "auth.invalidMethod"
        r = await c.post("/api/auth/setup", json={"method": "forms", "username": "me", "password": "long enough"})
        assert r.status_code == 200 and "shrinkerr_session" in r.cookies  # signed in straight away
        check = (await c.get("/api/auth/check")).json()
        assert (check["setup_required"], check["authenticated"], check["method"]) == (False, True, "session")
        assert (await c.get("/api/views")).status_code == 200
    async with _client(app, REMOTE) as other:  # anyone else signs in
        assert (await other.get("/api/auth/check")).json()["authenticated"] is False
        assert (await other.get("/api/views")).status_code == 401
        assert (await other.post("/api/auth/setup", json={"method": "external"})).status_code == 401
        assert (await other.post("/api/auth/login", json={"username": "me", "password": "long enough"})).status_code == 200
        assert (await other.get("/api/views")).status_code == 200


@pytest.mark.asyncio
async def test_an_api_key_install_needs_the_key_to_set_it_up(app, test_db):
    await _set(test_db, app, api_key="K")
    async with _client(app, REMOTE) as c:
        assert (await c.get("/api/auth/check")).json()["setup_required"] is True
        assert (await c.post("/api/auth/setup", json={"method": "external"})).status_code == 401
        r = await c.post("/api/auth/setup", json={"method": "forms", "username": "me", "password": "long enough"},
                         headers={"X-Api-Key": "K"})
        assert r.status_code == 200
        assert (await c.get("/api/views", headers={"X-Api-Key": "K"})).status_code == 200  # keys keep working


@pytest.mark.asyncio
async def test_not_for_local_addresses(app, test_db):
    from backend.main import _hash_password
    await _set(test_db, app, auth_method="forms", auth_required="local_disabled", auth_username="me",
               auth_password_hash=_hash_password("long enough"))
    async with _client(app, LOCAL) as c:
        check = (await c.get("/api/auth/check")).json()
        assert (check["authenticated"], check["method"]) == (True, "local")
        assert (await c.get("/api/views")).status_code == 200
        assert (await c.get("/api/views", headers={"X-Forwarded-For": "8.8.4.4"})).status_code == 401
        # the integration endpoints still need the key
        assert (await c.get("/api/settings/backup/download/x.db")).status_code in (401, 503)
    async with _client(app, REMOTE) as c:
        assert (await c.get("/api/views")).status_code == 401


@pytest.mark.asyncio
async def test_external(app, test_db):
    await _set(test_db, app, auth_method="external", api_key="K")
    async with _client(app, REMOTE) as c:
        check = (await c.get("/api/auth/check")).json()
        assert (check["authenticated"], check["method"], check["auth_method"]) == (True, "external", "external")
        assert (await c.get("/api/views")).status_code == 200
        assert (await c.post("/api/nodes/heartbeat", json={"node_id": "x", "name": "x"})).status_code == 401
    from backend.main import _check_ws_auth
    ws = SimpleNamespace(client=SimpleNamespace(host="8.8.4.4"), headers={}, query_params={}, cookies={})
    assert await _check_ws_auth(ws) is True
    await _set(test_db, app, auth_method="forms", auth_username="me", auth_password_hash="x")
    assert await _check_ws_auth(ws) is False


@pytest.mark.asyncio
async def test_the_settings(app, test_db):
    from backend.models import SettingsUpdate
    from backend.api_errors import ApiError
    from backend.routes import settings as settings_route
    await _set(test_db, app, auth_enabled="true", auth_username="me", auth_password_hash="x")  # before 0.10
    for bad, code in ((dict(auth_method=""), "settings.authRequired"), (dict(auth_enabled=False), "settings.authRequired"),
                      (dict(auth_required="sometimes"), "settings.invalidAuthRequired")):
        with pytest.raises(ApiError) as exc:
            await settings_route.update_encoding_settings(SettingsUpdate(**bad))
        assert exc.value.code == code, bad
    await settings_route.update_encoding_settings(SettingsUpdate(auth_method="external", auth_required="local_disabled"))
    got = await settings_route.get_encoding_settings()
    assert (got["auth_method"], got["auth_required"], got["auth_enabled"]) == ("external", "local_disabled", False)
    # A person in the UI (signed in, or let in by the proxy or as a local address) may set code-running settings.
    for method, ok in (("session", True), ("external", True), ("local", True), ("api_key", False), (None, False)):
        assert settings_route._signed_in_with_password(SimpleNamespace(state=SimpleNamespace(auth_method=method))) is ok


@pytest.mark.asyncio
async def test_settings_downloads_work_when_sign_in_trusts_the_request(app, test_db):
    """v0.10.0: the download-script and backup links can't send the API key,
    so with External sign-in they always failed. A reverse proxy's sign-in
    counts now — but a local address that skips sign-in doesn't (they hold
    every stored secret), nothing is ever anonymous, and the integration
    endpoints still need the key."""
    await _set(test_db, app, auth_method="external", api_key="K")
    async with _client(app, REMOTE) as c:
        assert (await c.get("/api/settings/nzbget-script")).status_code == 200
        assert (await c.get("/api/settings/sabnzbd-script")).status_code == 200
        assert (await c.get("/api/settings/nzbget-config")).status_code == 401
    await _set(test_db, app, auth_method="forms", auth_username="me", auth_password_hash="x",
               auth_required="local_disabled")
    async with _client(app, LOCAL) as c:
        assert (await c.get("/api/settings/nzbget-script")).status_code == 401
    await _set(test_db, app, auth_method="", auth_enabled="false", api_key="", auth_username="",
               auth_password_hash="", auth_required="enabled")
    async with _client(app, LOCAL) as c:  # no sign-in set up at all: still never anonymous
        assert (await c.get("/api/settings/nzbget-script")).status_code in (401, 503)
        assert (await c.get("/api/settings/backup/download/x.zip")).status_code in (401, 503)
