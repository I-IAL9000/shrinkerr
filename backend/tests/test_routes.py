import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client(test_db, monkeypatch):
    """Create an async test client with a fresh DB."""
    from backend import main as main_module
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    from backend.database import DB_PATH
    import backend.database as db_module

    # Point the app at the test DB
    db_module.DB_PATH = test_db
    # The auth middleware reads main's own DB_PATH (bound when backend.main
    # was first imported, possibly by another test) and caches it for 60s.
    # Route modules hold their own copies too.
    import sys
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)

    # Re-init job routes with test-db-backed instances
    queue = JobQueue(test_db)
    worker = QueueWorker(test_db)
    init_job_routes(worker, queue)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_check(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_list_media_dirs_empty(client):
    response = await client.get("/api/settings/dirs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 0


@pytest.mark.asyncio
async def test_get_queue_stats(client):
    response = await client.get("/api/jobs/stats")
    assert response.status_code == 200
    data = response.json()
    assert "total_jobs" in data
    assert "pending" in data
    assert "running" in data
    assert "completed" in data
    assert "failed" in data
    assert "total_space_saved" in data
    assert data["total_jobs"] == 0


@pytest.mark.asyncio
async def test_test_api_key_emby_unconfigured(client):
    """When Emby isn't configured, the test endpoint returns success=False."""
    response = await client.post("/api/settings/test-api",
                                  json={"service": "emby"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False


@pytest.mark.asyncio
async def test_jellyfin_settings_round_trip(client):
    """PUT then GET — every Jellyfin field the user can set must persist.
    Pre-v0.4.2 only `jellyfin_url` was actually written to the DB; the
    other 8 fields were silently dropped by update_encoding_settings.
    """
    payload = {
        "jellyfin_url": "http://jelly.local:8096",
        "jellyfin_api_key": "secret-api-key",
        "jellyfin_user_id": "user-abc",
        "jellyfin_path_mapping": "/media=/mnt/media",
        "jellyfin_scan_after_conversion": True,
        "jellyfin_empty_trash": False,
        "jellyfin_pause_on_stream": True,
        "jellyfin_pause_stream_threshold": 2,
        "jellyfin_pause_transcode_only": False,
    }
    put_resp = await client.put("/api/settings/encoding", json=payload)
    assert put_resp.status_code == 200, put_resp.text

    get_resp = await client.get("/api/settings/encoding")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["jellyfin_url"] == "http://jelly.local:8096"
    # api_key is masked in the GET response (security)
    assert data["jellyfin_api_key"] == "****-key"
    assert data["jellyfin_user_id"] == "user-abc"
    assert data["jellyfin_path_mapping"] == "/media=/mnt/media"
    assert data["jellyfin_configured"] is True
    # Pause-on-stream trio — v0.4.4 fixed the GET-response omission that
    # made the toggle appear disabled after every reload.
    assert data["jellyfin_pause_on_stream"] is True
    assert data["jellyfin_pause_stream_threshold"] == 2
    assert data["jellyfin_pause_transcode_only"] is False


@pytest.mark.asyncio
async def test_emby_settings_round_trip(client):
    """Same round-trip for Emby. v0.4.2+."""
    payload = {
        "emby_url": "http://emby.local:8096",
        "emby_api_key": "another-secret",
        "emby_user_id": "user-xyz",
        "emby_path_mapping": "/media=/mnt/media",
        "emby_scan_after_conversion": True,
        "emby_empty_trash": False,
        "emby_pause_on_stream": True,
        "emby_pause_stream_threshold": 3,
        "emby_pause_transcode_only": True,
    }
    put_resp = await client.put("/api/settings/encoding", json=payload)
    assert put_resp.status_code == 200, put_resp.text

    get_resp = await client.get("/api/settings/encoding")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["emby_url"] == "http://emby.local:8096"
    assert data["emby_api_key"] == "****cret"
    assert data["emby_user_id"] == "user-xyz"
    assert data["emby_path_mapping"] == "/media=/mnt/media"
    assert data["emby_configured"] is True
    assert data["emby_pause_on_stream"] is True
    assert data["emby_pause_stream_threshold"] == 3
    assert data["emby_pause_transcode_only"] is True


@pytest.mark.asyncio
async def test_auto_queue_priority_round_trip(client):
    """PUT then GET — auto_queue_priority setting must persist (Normal/High/Highest).
    v0.5.0+."""
    # Default should be 0 (Normal) on a fresh DB
    get0 = await client.get("/api/settings/encoding")
    assert get0.status_code == 200
    assert get0.json().get("auto_queue_priority", 0) == 0

    # PUT a non-default value
    put_resp = await client.put("/api/settings/encoding",
                                  json={"auto_queue_priority": 2})
    assert put_resp.status_code == 200, put_resp.text

    # GET should return the new value as int
    get_resp = await client.get("/api/settings/encoding")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["auto_queue_priority"] == 2

    # Clamp out-of-range values to [0, 2]
    put_low = await client.put("/api/settings/encoding",
                                json={"auto_queue_priority": -5})
    assert put_low.status_code == 200
    assert (await client.get("/api/settings/encoding")).json()["auto_queue_priority"] == 0

    put_high = await client.put("/api/settings/encoding",
                                 json={"auto_queue_priority": 99})
    assert put_high.status_code == 200
    assert (await client.get("/api/settings/encoding")).json()["auto_queue_priority"] == 2


# --- F10 (v0.10.0): the API key is accepted only in the X-Api-Key header ----

async def _set_api_key(db_path, key):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_key', ?)", (key,))
        await db.commit()


@pytest.mark.asyncio
async def test_api_key_in_the_url_is_refused(client, test_db):
    await _set_api_key(test_db, "k3y-for-tests")
    assert (await client.get("/api/settings/dirs?api_key=k3y-for-tests")).status_code == 401
    assert (await client.get("/api/settings/dirs", headers={"X-Api-Key": "k3y-for-tests"})).status_code == 200


@pytest.mark.asyncio
async def test_websocket_tickets_work_once(client, test_db, monkeypatch):
    from types import SimpleNamespace
    from backend import main as main_module
    await _set_api_key(test_db, "k3y-for-tests")
    resp = await client.post("/api/auth/ws-ticket", headers={"X-Api-Key": "k3y-for-tests"})
    ticket = resp.json()["ticket"]

    def ws(**query):
        return SimpleNamespace(query_params=query, cookies={})

    assert await main_module._check_ws_auth(ws(api_key="k3y-for-tests")) is False  # no longer accepted
    assert await main_module._check_ws_auth(ws(ticket=ticket)) is True
    assert await main_module._check_ws_auth(ws(ticket=ticket)) is False  # used up
    assert (await client.post("/api/auth/ws-ticket")).status_code == 401  # needs the key itself

    expired = (await client.post("/api/auth/ws-ticket", headers={"X-Api-Key": "k3y-for-tests"})).json()["ticket"]
    main_module._ws_tickets[expired] = 0
    assert await main_module._check_ws_auth(ws(ticket=expired)) is False


@pytest.mark.asyncio
async def test_webhooks_take_the_key_as_a_basic_auth_password(client, test_db):
    """Sonarr / Radarr's Connect → Webhook has Username / Password fields
    (v0.10.0); only webhook routes take it that way."""
    import base64
    await _set_api_key(test_db, "k3y-for-tests")

    def basic(password):
        return {"Authorization": "Basic " + base64.b64encode(f"sonarr:{password}".encode()).decode()}
    test_event = {"eventType": "Test"}
    assert (await client.post("/api/webhooks/arr", json=test_event, headers=basic("k3y-for-tests"))).json() == {"status": "ok"}
    assert (await client.post("/api/webhooks/arr", json=test_event, headers=basic("wrong"))).status_code == 401
    assert (await client.post("/api/webhooks/arr", json=test_event)).status_code == 401
    assert (await client.get("/api/settings/dirs", headers=basic("k3y-for-tests"))).status_code == 401
