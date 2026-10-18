"""Before / after frames (v0.10.0): a conversion grabs the same moments of
the original and the output while both exist; the job keeps them (the last
KEEP_JOBS jobs), remote workers send theirs, and the job's details show
them."""
import base64
import json
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest

from backend import frames

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _clip(path, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=720x480:r=24:d=4", *args, str(path)],
                   check=True)


def _size(jpeg: bytes, tmp_path) -> tuple[int, int]:
    f = tmp_path / "probe.jpg"
    f.write_bytes(jpeg)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(f)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return tuple(int(n) for n in out.split(","))


@needs_ffmpeg
@pytest.mark.asyncio
async def test_grabbing_frames(tmp_path):
    dvd = tmp_path / "dvd.mkv"  # anamorphic 16:9, 10-bit
    _clip(dvd, "-vf", "setsar=32/27", "-c:v", "libx264", "-pix_fmt", "yuv420p10le")
    out = tmp_path / "out.mkv"
    _clip(out, "-vf", "setsar=32/27", "-c:v", "mpeg4")
    got = await frames.grab(str(dvd), str(out), 4.0)
    assert [f["t"] for f in got] == [0.8, 2.0, 3.2]
    before = base64.b64decode(got[1]["before"])
    assert before[:2] == b"\xff\xd8" and _size(before, tmp_path) == (852, 478)  # square pixels
    assert await frames.grab(str(dvd), str(tmp_path / "missing.mkv"), 4.0) == []
    assert await frames.grab(str(dvd), str(out), 0) == []


def test_keeping_them(test_db, monkeypatch):
    monkeypatch.setattr(frames, "KEEP_JOBS", 2)
    one = [{"t": 1.5, "before": base64.b64encode(b"B1").decode(), "after": base64.b64encode(b"A1").decode()},
           {"t": 0.5, "before": base64.b64encode(b"B0").decode(), "after": base64.b64encode(b"A0").decode()}]
    assert frames.save(7, one) == 2
    assert frames.load(7) == [
        {"t": 1.5, "before": "data:image/jpeg;base64," + base64.b64encode(b"B1").decode(),
         "after": "data:image/jpeg;base64," + base64.b64encode(b"A1").decode()},
        {"t": 0.5, "before": "data:image/jpeg;base64," + base64.b64encode(b"B0").decode(),
         "after": "data:image/jpeg;base64," + base64.b64encode(b"A0").decode()}]
    assert frames.save(7, one[:1]) == 1 and len(frames.load(7)) == 1  # a new run replaces them
    frames.save(10, one)
    frames.save(9, one)
    assert (frames.load(7), len(frames.load(9)), len(frames.load(10))) == ([], 2, 2)  # the newest jobs keep theirs
    assert frames.save(11, []) == 0 and frames.load(11) == [] and frames.load(12) == []
    assert frames.save(12, [{"t": 1}]) == 0  # malformed: skipped


@pytest.mark.asyncio
async def test_the_frames_people_can_still_look_at_stay(test_db, monkeypatch):
    """Every job in Review keeps its frames (approved or not), and so do the
    most recently completed — by completion, not job id: an approved review
    is completed today with an id as old as its conversion, and its frames
    were deleted ("No frames for this job")."""
    monkeypatch.setattr(frames, "KEEP_JOBS", 2)
    one = [{"t": 1.0, "before": base64.b64encode(b"B").decode(), "after": base64.b64encode(b"A").decode()}]
    async with aiosqlite.connect(test_db) as db:
        for job_id, status, approved, done in ((1, "review", 0, None), (2, "running", 1, None),
                                               (3, "completed", 0, "2026-10-18T12:00:00"),
                                               (4, "completed", 0, "2026-10-10T12:00:00"),
                                               (5, "completed", 0, "2026-10-11T12:00:00")):
            await db.execute("INSERT INTO jobs (id, file_path, job_type, status, created_at, review_approved, completed_at) "
                             "VALUES (?, ?, 'convert', ?, 'x', ?, ?)", (job_id, f"/m/{job_id}.mkv", status, approved, done))
        await db.commit()
    for job_id in (1, 2, 3, 4, 5):
        frames.save(job_id, one, prune=False)
    assert await frames.store(6, one) == 1  # a new conversion (still running)
    kept = [j for j in range(1, 7) if frames.load(j)]
    assert kept == [1, 2, 3, 5, 6]  # 4: the oldest completion


@pytest.mark.asyncio
async def test_why_a_job_has_none(test_db, monkeypatch):
    """Frames deleted to keep the newest ones say so ("only the 100 most
    recent…"), not the catch-all reasons."""
    from backend.routes.jobs import job_frames
    monkeypatch.setattr(frames, "KEEP_JOBS", 1)
    one = [{"t": 1.0, "before": base64.b64encode(b"B").decode(), "after": base64.b64encode(b"A").decode()}]
    async with aiosqlite.connect(test_db) as db:
        for job_id, done in ((1, "2026-10-10T12:00:00"), (2, "2026-10-11T12:00:00"), (3, None)):
            await db.execute("INSERT INTO jobs (id, file_path, job_type, status, created_at, completed_at) "
                             "VALUES (?, ?, 'convert', ?, 'x', ?)", (job_id, f"/m/{job_id}.mkv",
                                                                    "completed" if done else "failed", done))
        await db.commit()
    await frames.store(1, one)
    await frames.store(2, one)  # 1 is no longer among the newest
    assert await job_frames(1) == {"frames": [], "missing": "deleted", "keep": 1}
    assert len((await job_frames(2))["frames"]) == 1 and (await job_frames(2))["missing"] is None
    assert (await job_frames(3))["missing"] == "unknown"  # never had any (before 0.10, a disc…)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET frames_count = 0")
        await db.commit()
    await frames.record_existing()  # frames saved before they were counted
    assert (await job_frames(2))["missing"] is None
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT id, frames_count FROM jobs ORDER BY id") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [(1, 0), (2, 1), (3, 0)]


def _x265() -> bool:
    return bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


async def _convert(test_db, tmp_path, monkeypatch, **settings):
    import sys
    from backend import bazarr
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda *a, **kw: None)
    src = tmp_path / "Film (2020)" / "Film (2020) x264.mkv"
    src.parent.mkdir(parents=True)
    _clip(src, "-c:v", "libx264", "-qp", "0")
    async with aiosqlite.connect(test_db) as db:
        for k, v in {"backup_original_days": "0", "trash_original_after_conversion": "false",
                     "vmaf_analysis_enabled": "false", **settings}.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET libx265_preset = 'ultrafast' WHERE id = ?", (job_id,))
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())
    await QueueWorker(test_db)._process_job(job)
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            return dict(await cur.fetchone())


@pytest.mark.skipif(not _x265(), reason="needs ffmpeg with libx265")
@pytest.mark.asyncio
async def test_a_conversion_keeps_its_frames(test_db, tmp_path, monkeypatch):
    from backend.routes.jobs import job_frames
    job = await _convert(test_db, tmp_path, monkeypatch)
    assert job["status"] == "completed"
    shown = (await job_frames(job["id"]))["frames"]
    assert [f["t"] for f in shown] == [0.8, 2.0, 3.2]
    assert all(f["before"].startswith("data:image/jpeg;base64,") and f["before"] != f["after"] for f in shown)


@pytest.mark.skipif(not _x265(), reason="needs ffmpeg with libx265")
@pytest.mark.asyncio
async def test_turned_off(test_db, tmp_path, monkeypatch):
    from backend.routes.jobs import job_frames
    job = await _convert(test_db, tmp_path, monkeypatch, compare_frames="false")
    assert job["status"] == "completed"
    assert await job_frames(job["id"]) == {"frames": [], "missing": "off", "keep": frames.KEEP_JOBS}


@pytest.mark.skipif(not _x265(), reason="needs ffmpeg with libx265")
@pytest.mark.asyncio
async def test_a_review_keeps_them_out_of_its_state(test_db, tmp_path, monkeypatch):
    job = await _convert(test_db, tmp_path, monkeypatch, review_before_replace="true")
    assert job["status"] == "review" and "frames" not in json.loads(job["review_json"])["result"]
    assert len(frames.load(job["id"])) == 3


@pytest.mark.asyncio
async def test_remote_workers_send_theirs(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = nodes.NodeManager()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('compare_frames', 'false')")
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/m/a.mkv", "convert", encoder="libx265")
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert job["compare_frames"] is False  # the server's setting
    sent = [{"t": 2.0, "before": base64.b64encode(b"B").decode(), "after": base64.b64encode(b"A").decode()}]
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="n1", job_id=job_id, success=True, output_path="/m/a.mkv", replaced_source=False, frames=sent), request)
    assert [f["t"] for f in frames.load(job_id)] == [2.0]


@pytest.mark.asyncio
async def test_the_worker_passes_them_on(tmp_path, monkeypatch):
    import backend.converter
    import backend.scanner
    from backend import worker_mode
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    sent = [{"t": 1.0, "before": "Qg==", "after": "QQ=="}]

    async def probe(*a, **kw):
        return dict(PROBE)

    async def convert(**kw):
        assert kw["pre_settings"]["compare_frames"] is True
        return {"success": True, "output_path": str(src), "space_saved": 1, "frames": list(sent)}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    monkeypatch.setattr(backend.converter, "convert_file", convert)
    client = FakeClient()
    await worker_mode.execute_job(client, "n1", {"id": 3, "file_path": str(src), "job_type": "convert",
                                                 "encoder": "libx265"}, ["libx265"])
    success, report = client.completed[-1]
    assert success is True and report["frames"] == sent
