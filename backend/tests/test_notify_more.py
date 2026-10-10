"""More notifications (v0.10.0): ntfy, Gotify and Apprise, and three more
events — a conversion rejected for its VMAF score, a remote worker going
offline, and a weekly summary."""
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest

import backend.notifications as notifications


async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.fixture
def http(monkeypatch):
    """Every request ntfy / Gotify would send."""
    sent = []

    def handler(request):
        sent.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return sent


@pytest.fixture
def apprise(monkeypatch):
    """A stand-in apprise module: what it was asked to send, and where."""
    calls = []

    class Apprise:
        def __init__(self):
            self.urls = []

        def add(self, url):
            self.urls.append(url)
            return True

        def __len__(self):
            return len(self.urls)

        def notify(self, title, body):
            calls.append((self.urls, title, body))
            return True
    monkeypatch.setitem(sys.modules, "apprise", types.SimpleNamespace(Apprise=Apprise))
    return calls


@pytest.mark.asyncio
async def test_ntfy_gotify_and_apprise(test_db, http, apprise):
    await _set(test_db, notify_job_failed="true", ntfy_url="https://ntfy.example/shrinkerr", ntfy_token="tk_1",
               gotify_url="https://gotify.example/", gotify_token="app-tok",
               apprise_urls="pover://user@token\n  slack://a/b/c  \n")
    results = await notifications.notify_job_failed("Film.mkv", "boom")
    assert results == {"ntfy": True, "gotify": True, "apprise": True}
    (ntfy_url, ntfy_headers, ntfy), (gotify_url, gotify_headers, gotify) = http
    assert ntfy_url == "https://ntfy.example" and ntfy_headers["authorization"] == "Bearer tk_1"
    assert (ntfy["topic"], ntfy["title"], ntfy["priority"]) == ("shrinkerr", "Job Failed", 4)
    assert ntfy["message"] == "Film.mkv failed during conversion\n\nError: boom"
    assert gotify_url == "https://gotify.example/message" and gotify_headers["x-gotify-key"] == "app-tok"
    assert (gotify["title"], gotify["priority"]) == ("Job Failed", 8)
    assert apprise == [(["pover://user@token", "slack://a/b/c"], "Job Failed",
                        "Film.mkv failed during conversion\n\nError: boom")]


@pytest.mark.asyncio
async def test_unconfigured_or_turned_off(test_db, http, apprise):
    await _set(test_db, notify_queue_complete="false", ntfy_url="https://ntfy.example/t")
    assert await notifications.notify_queue_complete(3, "1 GB") == {}
    await _set(test_db, notify_queue_complete="true", gotify_url="https://gotify.example")  # no token
    assert await notifications.notify_queue_complete(3, "1 GB") == {"ntfy": True}
    assert http[0][2]["priority"] == 3  # not urgent
    # The test button reaches them too.
    assert (await notifications.test_notifications())["ntfy"] is True


@pytest.mark.asyncio
async def test_the_new_events(test_db, http):
    await _set(test_db, ntfy_url="https://ntfy.example/t", notify_vmaf_rejected="true", notify_node_offline="true")
    await notifications.notify_vmaf_rejected("Film.mkv", 84.26, 88)
    msg = http[-1][2]
    assert msg["title"] == "Conversion Rejected" and msg["priority"] == 4
    assert msg["message"].startswith("Film.mkv was converted but its VMAF score was too low")
    assert "VMAF: 84.3" in msg["message"] and "Minimum: 88" in msg["message"]
    await notifications.notify_node_offline("gpu-box", "2026-10-10T12:00:00")
    assert http[-1][2]["title"] == "Worker Offline" and "gpu-box" in http[-1][2]["message"]


@pytest.mark.asyncio
async def test_the_weekly_summary(test_db, http):
    await _set(test_db, ntfy_url="https://ntfy.example/t", notify_weekly_digest="true")
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(test_db) as db:
        for status, days, saved in (("completed", 1, 3 * 1024 ** 3), ("completed", 3, 1024 ** 3),
                                    ("completed", 30, 50 * 1024 ** 3), ("failed", 2, 0), ("pending", 0, 0)):
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, space_saved) "
                "VALUES (?, 'convert', ?, ?, ?, ?)",
                (f"/m/{status}{days}.mkv", status, now.isoformat(), (now - timedelta(days=days)).isoformat(), saved))
        await db.commit()
    await notifications.notify_weekly_digest()
    msg = http[-1][2]
    assert msg["title"] == "Weekly Summary"
    assert msg["message"] == ("2 files converted this week, saving 4.0 GB.\n\n"
                              "Failed: 1\nWaiting in the queue: 1\nSaved in total: 54.0 GB")


@pytest.mark.asyncio
async def test_the_weekly_summary_goes_out_once_a_week(test_db):
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    assert await notifications.weekly_digest_due(now) is False  # turned off
    await _set(test_db, notify_weekly_digest="true")
    assert await notifications.weekly_digest_due(now) is False  # just turned on: a week from now
    assert await notifications.weekly_digest_due(now + timedelta(days=6)) is False
    assert await notifications.weekly_digest_due(now + timedelta(days=7)) is True
    assert await notifications.weekly_digest_due(now + timedelta(days=8)) is False  # sent on day 7


@pytest.mark.asyncio
async def test_a_worker_going_offline_is_reported_once(test_db, monkeypatch):
    import backend.nodes as nodes
    monkeypatch.setattr(nodes, "DB_PATH", test_db)
    sent = []

    async def offline(name, last_seen):
        sent.append(name)
    monkeypatch.setattr(notifications, "notify_node_offline", offline)
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, status, registered_at, last_heartbeat) "
                         "VALUES ('n1', 'gpu-box', 'online', ?, ?)", (old, old))
        await db.commit()
    nm = nodes.NodeManager()
    nm._started_at -= 10_000  # long after a restart
    await nm.release_stale_assignments()
    await nm.release_stale_assignments()  # already offline: not again
    assert sent == ["gpu-box"]


@pytest.mark.asyncio
async def test_a_remote_vmaf_rejection_is_recorded(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    sent = []

    async def rejected(name, score, minimum):
        sent.append((name, score, minimum))
    monkeypatch.setattr(notifications, "notify_vmaf_rejected", rejected)
    nm = nodes.NodeManager()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))
    job_id = await JobQueue(test_db).add_job("/m/Film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, status, registered_at) VALUES ('n1', 'n1', 'online', 'x')")
        await db.execute("UPDATE jobs SET status = 'running', assigned_node_id = 'n1' WHERE id = ?", (job_id,))
        await db.commit()
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="n1", job_id=job_id, success=True, output_path="/m/Film.mkv", vmaf_score=84.2,
        replaced_source=False, vmaf_rejected=True, vmaf_reject_reason="VMAF 84.2 below 88",
        vmaf_reject_params={"score": "84.2", "min": "88"}), request)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT error_key, error_params FROM jobs WHERE id = ?", (job_id,)) as cur:
            key, params = await cur.fetchone()
    assert key == "errors.vmafRejected" and json.loads(params) == {"score": "84.2", "min": "88"}
    assert sent == [("Film.mkv", 84.2, "88")]


@pytest.mark.asyncio
async def test_tokens_are_masked_and_kept(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await _set(test_db, ntfy_token="tk_secret1234", gotify_token="gotsecret9876", apprise_urls="pover://u@t")
    got = await settings_route.get_encoding_settings()
    assert (got["ntfy_token"], got["gotify_token"]) == ("****1234", "****9876")
    assert got["notify_weekly_digest"] is False
    await settings_route.update_encoding_settings(SettingsUpdate(ntfy_token="****1234", notify_weekly_digest=True))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('ntfy_token', 'notify_weekly_digest')") as cur:
            assert dict(await cur.fetchall()) == {"ntfy_token": "tk_secret1234", "notify_weekly_digest": "true"}
    assert {"ntfy_token", "gotify_token", "apprise_urls", "ntfy_url"} <= settings_route._SECRET_SETTINGS_KEYS


@pytest.mark.asyncio
async def test_the_real_apprise_library(test_db):
    """Apprise itself, posting to a local json:// endpoint."""
    pytest.importorskip("apprise")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    await _set(test_db, notify_job_failed="true", apprise_urls=f"json://127.0.0.1:{server.server_port}/hook")
    try:
        assert await notifications.notify_job_failed("Film.mkv", "boom") == {"apprise": True}
    finally:
        server.server_close()
    assert got and got[0]["title"] == "Job Failed" and "Film.mkv failed during conversion" in got[0]["message"]
