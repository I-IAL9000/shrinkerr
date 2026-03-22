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
