"""Where a VMAF target search landed (v0.10.0): the next search in the same
folder (a season, a film's folder) starts there instead of at the job's own
quality, so it needs fewer rounds. Only the start moves: the search still
runs in full. Local jobs look it up and remember it on the server; a remote
job's start travels with the assignment and its result is read back from the
job's encoding_stats."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

import backend.nodes as nodes
import backend.routes.nodes as nodes_route
from backend import vmaf_target
from backend.nodes import NodeManager
from backend.queue import JobQueue, QueueWorker

SEASON = "/m/Show/Season 1"


async def _recall(db_path, path, target=93, encoder="nvenc", codec="hevc"):
    async with aiosqlite.connect(db_path) as db:
        return await vmaf_target.remembered_cq(db, path, target, encoder, codec)


@pytest.mark.asyncio
async def test_remember_and_recall(test_db):
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember(db, f"{SEASON}/E01.mkv", 93, "nvenc", "hevc", 31.5)
    assert await _recall(test_db, f"{SEASON}/E02.mkv") == 31.5
    assert await _recall(test_db, f"{SEASON}/E02.mkv", target=95) is None
    assert await _recall(test_db, f"{SEASON}/E02.mkv", encoder="libx265") is None
    assert await _recall(test_db, f"{SEASON}/E02.mkv", codec="av1") is None
    assert await _recall(test_db, "/m/Show/Season 2/E01.mkv") is None  # another folder

    async with aiosqlite.connect(test_db) as db:  # the latest landing replaces it
        await vmaf_target.remember(db, f"{SEASON}/E01.mkv", 93, "nvenc", "hevc", 33)
    assert await _recall(test_db, f"{SEASON}/E03.mkv") == 33

    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE vmaf_target_memory SET updated_at = ?", (old,))
        await db.commit()
    assert await _recall(test_db, f"{SEASON}/E02.mkv") is None  # too old to trust

    recent = (datetime.now(timezone.utc) - timedelta(days=89)).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE vmaf_target_memory SET updated_at = ?", (recent,))
        await db.commit()
    assert await _recall(test_db, f"{SEASON}/E02.mkv") == 33


@pytest.mark.asyncio
async def test_a_folder_with_no_landing_starts_where_the_others_landed(test_db):
    """A film is alone in its folder: it started at the rule's CQ every time.
    Now it starts at the median of where the other searches landed, once
    there are three."""
    async with aiosqlite.connect(test_db) as db:
        for film, cq in (("A", 27), ("B", 33)):
            await vmaf_target.remember(db, f"/m/{film} (2020)/{film}.mkv", 93, "nvenc", "hevc", cq)
    assert await _recall(test_db, "/m/New (2020)/New.mkv") is None  # two aren't enough
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember(db, "/m/C (2020)/C.mkv", 93, "nvenc", "hevc", 29)
        await vmaf_target.remember(db, "/m/D (2020)/D.mkv", 95, "nvenc", "hevc", 20)  # another target
    assert await _recall(test_db, "/m/New (2020)/New.mkv") == 29
    assert await _recall(test_db, "/m/A (2020)/Another.mkv") == 27  # a folder's own landing first
    assert await _recall(test_db, "/m/New (2020)/New.mkv", encoder="libx265") is None


@pytest.mark.asyncio
async def test_what_expired_is_cleared_when_something_new_is_remembered(test_db):
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember(db, f"{SEASON}/E01.mkv", 93, "nvenc", "hevc", 31)
        await db.execute("UPDATE vmaf_target_memory SET updated_at = ?",
                         ((datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),))
        await db.commit()
        await vmaf_target.remember(db, "/m/Film (2020)/Film.mkv", 93, "nvenc", "hevc", 28)
        async with db.execute("SELECT folder FROM vmaf_target_memory") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["/m/Film (2020)"]


@pytest.mark.asyncio
async def test_a_landing_is_kept_under_the_encoder_that_searched(test_db):
    """A result that doesn't say which encoder searched (a cancelled
    re-encode's) isn't remembered under a guessed one."""
    landed = {"target": 93.0, "cq": 31.0}
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember_landing(db, f"{SEASON}/E01.mkv", {"vmaf_target": landed})
        assert await (await db.execute("SELECT COUNT(*) FROM vmaf_target_memory")).fetchone() == (0,)
        await vmaf_target.remember_landing(db, f"{SEASON}/E01.mkv", {"encoder": "libx265", "vmaf_target": landed})
    assert await _recall(test_db, f"{SEASON}/E02.mkv", encoder="libx265") == 31.0


def test_the_target_a_job_searches_for():
    effective = vmaf_target.effective_target
    assert effective({}) is None
    assert effective({"vmaf_target_enabled": False, "vmaf_target_score": 95}) is None
    assert effective({"vmaf_target_enabled": True, "vmaf_target_score": 95}) == 95.0
    assert effective({"vmaf_target_enabled": True}) == float(vmaf_target.DEFAULT_TARGET)  # no score: the default
    # The VMAF minimum is a floor under it, when analysis is on.
    assert effective({"vmaf_target_enabled": True, "vmaf_target_score": 90, "vmaf_min_score": 92}) == 92.0
    assert effective({"vmaf_target_enabled": True, "vmaf_target_score": 90, "vmaf_min_score": 92,
                      "vmaf_analysis_enabled": False}) == 90.0


# --- a local job ---------------------------------------------------------------------------------------------

async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for key, value in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()


async def _rule_target(db_path, job_id, target):
    """The target a rule resolved for the job when it was queued (0: Off)."""
    if target is not None:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE jobs SET vmaf_target = ? WHERE id = ?", (target, job_id))
            await db.commit()


@pytest.fixture
def local_jobs(test_db, tmp_path, monkeypatch):
    """Jobs run through QueueWorker._process_job with a convert_file that
    records its keyword arguments and returns what a search that landed at
    `landed` (or no search: None) reports."""
    import sys

    import backend.converter
    import backend.scanner
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    folder = tmp_path / "Show" / "Season 1"
    folder.mkdir(parents=True)
    state = SimpleNamespace(folder=folder, calls=[], landed=30, target=93.0, encoder="libx265")

    async def fake_probe(path, *a, **kw):
        return {"duration": 1400.0, "file_size": 1000, "video_codec": "h264", "audio_tracks": [],
                "subtitle_tracks": []}

    async def fake_convert_file(**kwargs):
        state.calls.append(kwargs)
        src = kwargs["input_path"]
        out = src.replace(".mkv", " x265.mkv")
        open(out, "wb").close()
        return {"success": True, "output_path": out, "space_saved": 500, "error": None,
                "encoding_stats": {"encoder": state.encoder, "output_codec": "hevc",
                                   "vmaf_target": None if state.landed is None else
                                   {"target": state.target, "cq": state.landed, "vmaf": 93.4, "min": 92.1,
                                    "reached": True, "rounds": 4, "samples": 3, "seconds": 61.0}}}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.converter, "convert_file", fake_convert_file)

    async def run(name, vmaf_target=None):
        src = folder / name
        src.write_bytes(b"x")
        queue = JobQueue(test_db)
        job_id = await queue.add_job(str(src), "convert", encoder="libx265")
        await _rule_target(test_db, job_id, vmaf_target)
        job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
        await QueueWorker(test_db)._process_job(job)
        return state.calls[-1]
    state.run = run
    return state


@pytest.mark.asyncio
async def test_a_local_job_starts_where_the_last_one_landed(test_db, local_jobs):
    await _set(test_db, vmaf_target_enabled="true", vmaf_target_score=93)

    first = await local_jobs.run("E01.mkv")
    assert first["vmaf_target_start_cq"] is None  # nothing known about this folder yet
    assert await _recall(test_db, str(local_jobs.folder / "E02.mkv"), encoder="libx265") == 30

    local_jobs.landed = 34.5
    second = await local_jobs.run("E02.mkv")
    assert second["vmaf_target_start_cq"] == 30
    third = await local_jobs.run("E03.mkv")
    assert third["vmaf_target_start_cq"] == 34.5  # the last landing, not the first


@pytest.mark.asyncio
async def test_memory_is_per_target_and_per_encoder(test_db, local_jobs):
    await _set(test_db, vmaf_target_enabled="true", vmaf_target_score=93)
    await local_jobs.run("E01.mkv")

    await _set(test_db, vmaf_target_score=95)  # another target in the same folder
    assert (await local_jobs.run("E02.mkv"))["vmaf_target_start_cq"] is None
    await _set(test_db, vmaf_target_score=93)
    assert (await local_jobs.run("E03.mkv"))["vmaf_target_start_cq"] == 30


@pytest.mark.asyncio
async def test_a_rule_that_switches_the_target_off_neither_looks_up_nor_remembers(test_db, local_jobs):
    await _set(test_db, vmaf_target_enabled="true", vmaf_target_score=93)
    await local_jobs.run("E01.mkv")  # the folder's landing is remembered

    local_jobs.landed = None  # convert_file doesn't search when the target is off
    off = await local_jobs.run("E02.mkv", vmaf_target=0)
    assert off["vmaf_target_start_cq"] is None
    assert await _recall(test_db, str(local_jobs.folder / "E02.mkv"), encoder="libx265") == 30  # untouched


@pytest.mark.asyncio
async def test_nothing_is_looked_up_or_remembered_while_the_target_is_off(test_db, local_jobs):
    await _set(test_db, vmaf_target_enabled="false")
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember(db, str(local_jobs.folder / "E00.mkv"), 93, "libx265", "hevc", 30)
    local_jobs.landed = None
    assert (await local_jobs.run("E01.mkv"))["vmaf_target_start_cq"] is None


@pytest.mark.asyncio
async def test_a_job_that_cant_be_searched_leaves_the_memory_alone(test_db, local_jobs):
    await _set(test_db, vmaf_target_enabled="true", vmaf_target_score=93)
    await local_jobs.run("E01.mkv")
    local_jobs.landed = None  # a disc, or a short file: convert_file reports no search
    await local_jobs.run("E02.mkv")
    assert await _recall(test_db, str(local_jobs.folder / "E03.mkv"), encoder="libx265") == 30


# --- a remote job --------------------------------------------------------------------------------------------

@pytest.fixture
def node_api(test_db, monkeypatch):
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*args, **kwargs):
        return None

    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = NodeManager()
    return nm, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))


async def _node(db_path, settings: dict):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO worker_nodes (id, name, capabilities, status, registered_at, path_mappings) "
            "VALUES ('node-1', 'node-1', ?, 'online', '2026-10-30T00:00:00', '[]')", (json.dumps(["libx265"]),))
        for key, value in settings.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


@pytest.mark.asyncio
async def test_a_remote_job_is_sent_where_the_last_one_landed(node_api, test_db):
    nm, request = node_api
    await _node(test_db, {"vmaf_target_enabled": "true", "vmaf_target_score": "93"})
    async with aiosqlite.connect(test_db) as db:
        # The node runs libx265 whatever the job was tagged with (a translated NVENC job).
        await vmaf_target.remember(db, f"{SEASON}/E01.mkv", 93, "libx265", "hevc", 31.5)
    await JobQueue(test_db).add_job(f"{SEASON}/E02.mkv", "convert", encoder="nvenc")

    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]

    assert job["vmaf_target_start_cq"] == 31.5


@pytest.mark.asyncio
async def test_a_remote_job_with_no_target_is_sent_no_start(node_api, test_db):
    nm, request = node_api
    await _node(test_db, {"vmaf_target_enabled": "true", "vmaf_target_score": "93"})
    async with aiosqlite.connect(test_db) as db:
        await vmaf_target.remember(db, f"{SEASON}/E01.mkv", 93, "libx265", "hevc", 31.5)
    job_id = await JobQueue(test_db).add_job(f"{SEASON}/E02.mkv", "convert", encoder="libx265")
    await _rule_target(test_db, job_id, 0)

    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]

    assert job["vmaf_target_start_cq"] is None  # a rule's Off wins over Settings' target
    assert job["vmaf_target_enabled"] is False


@pytest.mark.asyncio
async def test_a_remote_completion_remembers_where_the_search_landed(node_api, test_db):
    nm, request = node_api
    await _node(test_db, {"vmaf_target_enabled": "true", "vmaf_target_score": "93"})
    queue = JobQueue(test_db)
    job_id = await queue.add_job(f"{SEASON}/E02.mkv", "convert", encoder="libx265")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    assert await nm.assign_job_to_node("node-1", job) is not None
    stats = {"encoder": "libx265", "output_codec": "hevc", "input_size": 100, "output_size": 40,
             "vmaf_target": {"target": 93.0, "cq": 33.5, "vmaf": 93.2, "min": 92.0, "reached": True,
                             "rounds": 5, "samples": 3, "seconds": 70.0}}

    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=True, output_path=f"{SEASON}/E02.mkv", space_saved=60,
        encoding_stats=stats, replaced_source=False), request)

    assert await _recall(test_db, f"{SEASON}/E03.mkv", encoder="libx265") == 33.5


@pytest.mark.asyncio
async def test_the_worker_starts_its_search_where_it_is_told(tmp_path, monkeypatch):
    import backend.converter
    import backend.scanner
    from backend import worker_mode
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
    job = {"id": 9, "file_path": str(src), "job_type": "convert", "encoder": "libx265",
           "vmaf_target_enabled": True, "vmaf_target_score": 93.0}

    await worker_mode.execute_job(FakeClient(), "node-1", {**job, "vmaf_target_start_cq": 31.5}, ["libx265"])
    assert seen["vmaf_target_start_cq"] == 31.5
    await worker_mode.execute_job(FakeClient(), "node-1", job, ["libx265"])  # an older server sends none
    assert seen["vmaf_target_start_cq"] is None
