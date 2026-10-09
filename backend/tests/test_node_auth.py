"""Which /api/nodes/ routes always need the API key (v0.10.0).

The remote-worker endpoints do, even on an install with no key or password
(otherwise anyone on the network could register as a worker). The Nodes page
and Monitor routes follow the normal rule — demanding a key there 503'd the
Monitor's node cards and the local node's settings on such installs."""
import inspect

import aiosqlite
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute

KEY_REQUIRED = "This endpoint requires an API key"


def _always_required(path: str) -> bool:
    from backend.main import _AUTH_ALWAYS_REQUIRED_PREFIXES
    return any(path.startswith(p) for p in _AUTH_ALWAYS_REQUIRED_PREFIXES)


def test_exactly_the_worker_endpoints_always_need_the_key():
    """A route that checks a worker's node token is a worker endpoint. New
    ones must be added to the list; nothing else may be on it."""
    from backend.routes.nodes import router
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    worker = {r.path for r in routes if "_require_node_token(" in inspect.getsource(r.endpoint)}
    assert worker == {
        "/api/nodes/heartbeat", "/api/nodes/request-job", "/api/nodes/report-progress",
        "/api/nodes/report-complete", "/api/nodes/report-metrics",
    }
    for r in routes:
        assert _always_required(r.path) == (r.path in worker), r.path


@pytest_asyncio.fixture
async def client(test_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from backend import main as main_module
    from backend.nodes import NodeManager
    import backend.database as db_module
    import backend.nodes as nodes_module
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setattr(nodes_module, "DB_PATH", test_db, raising=False)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    monkeypatch.setattr(main_module.app.state, "node_manager", NodeManager(), raising=False)
    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_without_auth_the_node_admin_routes_work_and_worker_routes_dont(client):
    r = await client.get("/api/nodes/metrics")
    assert r.status_code == 200, r.text
    r = await client.delete("/api/nodes/no-such-node")
    assert KEY_REQUIRED not in r.text  # reaches the route
    r = await client.post("/api/nodes/heartbeat", json={"node_id": "rogue", "name": "rogue"})
    assert r.status_code == 503 and KEY_REQUIRED in r.text


@pytest.mark.asyncio
async def test_with_a_key_set_the_node_admin_routes_need_it(client, test_db):
    from backend import main as main_module
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('api_key', 'K')")
        await db.commit()
    main_module._auth_cache["checked_at"] = 0
    assert (await client.get("/api/nodes/metrics")).status_code == 401
    assert (await client.get("/api/nodes/metrics", headers={"X-Api-Key": "K"})).status_code == 200
