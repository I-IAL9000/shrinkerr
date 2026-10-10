"""Remote workers (v0.10.0, found while checking the docs):

- Workers never detected Quick Sync or VAAPI, so an Intel / AMD worker (the
  Nodes page writes its command) encoded on the CPU; and they weren't sent
  Quick Sync / VAAPI quality or NVENC's bit depth (always 10-bit, which a
  Maxwell GPU can't encode).
- A job that failed on a worker sent no "job failed" notification; a
  cancelled one was recorded as failed; and both a cancel and a node pause
  counted towards suspending the node.
- Worker containers ran the server's health check (port 6680, which a
  worker doesn't serve), so they always showed as unhealthy.
"""
import json
import os
import subprocess
import sys
import time

import aiosqlite
import pytest

import backend.encoder_caps as encoder_caps
import backend.routes.nodes as nodes_route
from backend import worker_mode
from backend.queue import JobQueue
from backend.tests.test_remote_worker_parity import _setup, node_api  # noqa: F401 (fixture)


class _Proc:
    returncode = 0

    def __init__(self, out=b""):
        self._out = out

    async def communicate(self):
        return self._out, b""


@pytest.mark.asyncio
async def test_workers_detect_quick_sync_and_vaapi(monkeypatch):
    async def fake_exec(*args, **kwargs):
        if "-encoders" in args:
            return _Proc(b" V....D libx265\n V....D hevc_qsv\n V....D hevc_vaapi\n")
        return _Proc()
    monkeypatch.setattr(worker_mode.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(encoder_caps, "_intel_render_node", lambda: "/dev/dri/renderD128")
    monkeypatch.setattr(encoder_caps, "_vaapi_render_node", lambda: "/dev/dri/renderD128")

    async def no_vt():
        return False
    monkeypatch.setattr(encoder_caps, "videotoolbox_encode_works", no_vt)
    caps, _ = await worker_mode._detect_capabilities()
    assert {"qsv", "vaapi", "libx265"} <= set(caps)

    monkeypatch.setattr(encoder_caps, "_intel_render_node", lambda: None)  # an NVIDIA-only host
    monkeypatch.setattr(encoder_caps, "_vaapi_render_node", lambda: None)
    caps, _ = await worker_mode._detect_capabilities()
    assert "qsv" not in caps and "vaapi" not in caps


@pytest.mark.asyncio
async def test_workers_get_quick_sync_vaapi_and_bit_depth_settings(node_api, test_db):  # noqa: F811
    _, request = node_api
    await _setup(test_db, {"qsv_cq": "19", "qsv_preset": "slow", "qsv_lookahead": "true", "vaapi_qp": "24",
                           "vaapi_compression_level": "2", "nvenc_bit_depth": "8bit"})
    await JobQueue(test_db).add_job("/media/b.mkv", "convert", encoder="libx265")
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]
    assert (job["qsv_cq"], job["qsv_preset"], job["qsv_lookahead"]) == (19, "slow", True)
    assert (job["vaapi_qp"], job["vaapi_compression_level"], job["nvenc_bit_depth"]) == (24, 2, "8bit")


@pytest.mark.asyncio
async def test_the_worker_encodes_with_them(tmp_path, monkeypatch):
    import backend.converter
    import backend.scanner
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient
    src = tmp_path / "Movie (2009) h264.mkv"
    src.write_bytes(b"x")
    seen = {}

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_convert_file(**kwargs):
        seen.update(kwargs)
        return {"success": True, "output_path": str(src), "space_saved": 1, "error": None}
    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.converter, "convert_file", fake_convert_file)
    await worker_mode.execute_job(FakeClient(), "node-1", {
        "id": 9, "file_path": str(src), "job_type": "convert", "encoder": "qsv",
        "qsv_cq": 19, "qsv_preset": "slow", "qsv_lookahead": True, "vaapi_qp": 24,
        "vaapi_compression_level": 2, "nvenc_bit_depth": "8bit"}, ["qsv", "libx265"])
    s = seen["pre_settings"]
    assert (s["encoder"], s["qsv_cq"], s["qsv_preset"], s["qsv_lookahead"]) == ("qsv", 19, "slow", True)
    assert (s["vaapi_qp"], s["vaapi_compression_level"], s["nvenc_bit_depth"]) == (24, 2, "8bit")


async def _running_on_node(db_path, failures=0) -> int:
    job_id = await JobQueue(db_path).add_job("/media/Film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE jobs SET status = 'running', assigned_node_id = 'node-1' WHERE id = ?", (job_id,))
        await db.execute("UPDATE worker_nodes SET current_job_id = ?, consecutive_failures = ? WHERE id = 'node-1'",
                         (job_id, failures))
        await db.commit()
    return job_id


async def _after(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            status = (await cur.fetchone())[0]
        async with db.execute("SELECT consecutive_failures, current_job_id FROM worker_nodes WHERE id = 'node-1'") as cur:
            failures, current = await cur.fetchone()
    return status, failures, current


@pytest.fixture
def failed_notices(monkeypatch):
    import backend.notifications as notifications
    sent = []

    async def notify(name, error):
        sent.append((name, error))
    monkeypatch.setattr(notifications, "notify_job_failed", notify)
    return sent


@pytest.mark.asyncio
async def test_a_failure_on_a_worker_notifies(node_api, test_db, failed_notices):  # noqa: F811
    _, request = node_api
    await _setup(test_db, {})
    job_id = await _running_on_node(test_db)
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=False, error="ffmpeg exited 1"), request)
    assert await _after(test_db, job_id) == ("failed", 1, None)
    assert failed_notices == [("Film.mkv", "ffmpeg exited 1")]


@pytest.mark.asyncio
async def test_a_cancel_on_a_worker_is_a_cancel(node_api, test_db, failed_notices):  # noqa: F811
    _, request = node_api
    await _setup(test_db, {})
    job_id = await _running_on_node(test_db, failures=2)
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=False, error="Cancelled by user",
        error_key="errors.cancelledByUser"), request)
    assert await _after(test_db, job_id) == ("cancelled", 2, None)
    assert failed_notices == []


@pytest.mark.asyncio
async def test_a_node_pause_doesnt_count_as_a_failure(node_api, test_db, failed_notices):  # noqa: F811
    nm, request = node_api
    await _setup(test_db, {})
    job_id = await _running_on_node(test_db, failures=4)
    nm.request_cancel(job_id, requeue=True)
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=False, error="Cancelled by user",
        error_key="errors.cancelledByUser"), request)
    assert await _after(test_db, job_id) == ("pending", 4, None)
    assert failed_notices == []


HEALTHCHECK = os.path.join(os.path.dirname(__file__), "..", "..", "docker", "healthcheck.py")


def _healthy(tmp_path, env) -> bool:
    return subprocess.run([sys.executable, HEALTHCHECK], env={**os.environ, "TMPDIR": str(tmp_path), **env},
                          timeout=20).returncode == 0


def test_a_worker_is_healthy_while_its_heartbeats_get_through(tmp_path, monkeypatch):
    worker = {"SHRINKERR_MODE": "worker"}
    assert not _healthy(tmp_path, worker)  # no heartbeat yet
    monkeypatch.setattr(worker_mode, "ALIVE_FILE", str(tmp_path / "shrinkerr-worker-alive"))
    worker_mode._mark_alive()
    assert _healthy(tmp_path, worker)
    old = time.time() - 600
    os.utime(tmp_path / "shrinkerr-worker-alive", (old, old))
    assert not _healthy(tmp_path, worker)


def test_the_server_still_asks_its_api(monkeypatch):
    import importlib.util
    import urllib.request
    spec = importlib.util.spec_from_file_location("healthcheck", HEALTHCHECK)
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    asked = []

    def urlopen(url, timeout=None):
        asked.append(url)
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("SHRINKERR_MODE", raising=False)
    assert hc.healthy() is False and asked == ["http://localhost:6680/api/health"]


@pytest.mark.asyncio
async def test_a_heartbeat_marks_the_worker_alive(tmp_path, monkeypatch):
    import httpx
    alive = tmp_path / "alive"
    monkeypatch.setattr(worker_mode, "ALIVE_FILE", str(alive))

    def handler(request):
        return httpx.Response(200, json={"status": "ok", "heartbeat_interval": 30})
    client = worker_mode.ServerClient("http://server:6680", "key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server:6680")
    await client.heartbeat("node-1", "n", "h", ["libx265"], [], None, None, None, 1)
    assert alive.exists()
