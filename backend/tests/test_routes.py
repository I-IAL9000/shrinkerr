import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def client(test_db):
    """Create an async test client with a fresh DB."""
    from backend import main as main_module
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    from backend.database import DB_PATH
    import backend.database as db_module

    # Point the app at the test DB
    db_module.DB_PATH = test_db

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
