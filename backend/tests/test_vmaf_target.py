"""VMAF target quality (v0.10.0): each title's quality is found on a few
encoded samples — the lowest that still scores the target VMAF — and the
file is encoded at it."""
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest

from backend import vmaf_target
from backend.tests.test_remote_worker_parity import node_api  # noqa: F401  (fixture)


@pytest.mark.asyncio
async def test_bracket_then_bisect():
    asked = []

    async def score(cq):  # one VMAF point per CQ step, samples ±0.5
        asked.append(cq)
        return [111.0 - cq - 0.5, 111.0 - cq + 0.5]
    r = await vmaf_target.search(score, 93, start=20)
    assert (r["cq"], r["reached"]) == (18, True) and r["vmaf"] == 93.0 and r["min"] == 92.5
    assert asked[0] == 20 and len(asked) == len(set(asked)) and r["rounds"] == len(asked) <= 6


@pytest.mark.asyncio
async def test_the_ceiling_is_42():
    async def score(cq):
        return [99.0, 99.0]
    assert (await vmaf_target.search(score, 93, start=36))["cq"] == 42
    assert (await vmaf_target.search(score, 93, start=60))["cq"] == 42  # a start past it is clamped
    assert (await vmaf_target.search(score, 93, start=36, half_steps=True))["cq"] == 42  # no 42.5


@pytest.mark.asyncio
async def test_the_steps_grow_from_a_far_start():  # 2, 4, 8, 16 … from the start
    async def easy(cq):
        return [99.0, 99.0]
    r = await vmaf_target.search(easy, 93, start=14)
    assert (r["cq"], r["reached"]) == (42, True) and r["rounds"] <= 6

    asked = []

    async def score(cq):  # the answer is CQ 18
        asked.append(cq)
        return [111.0 - cq] * 2
    for start in (14, 16):  # below the answer: step up until one fails, then bisect
        asked.clear()
        r = await vmaf_target.search(score, 93, start=start)
        assert (r["cq"], r["reached"]) == (18, True)
        assert len(asked) == len(set(asked)) == r["rounds"] <= 6
    asked.clear()  # above it: the mirror, 42 40 36 28 14 then bisect 21 17 19 18
    r = await vmaf_target.search(score, 93, start=42)
    assert (r["cq"], r["reached"]) == (18, True)
    assert len(asked) == len(set(asked)) == r["rounds"] <= 9

    async def hopeless(cq):
        return [80.0, 80.0]
    r = await vmaf_target.search(hopeless, 93, start=42)  # the steps end at the floor, which is scored
    assert (r["cq"], r["reached"], r["rounds"]) == (14, False, 5)


@pytest.mark.asyncio
async def test_out_of_reach_uses_the_floor():
    async def score(cq):
        return [80.0, 80.0]
    r = await vmaf_target.search(score, 93, start=30)
    assert (r["cq"], r["reached"]) == (14, False)
    asked = []

    async def odd(cq):  # an odd start steps over CQ 14: the floor is still tried
        asked.append(cq)
        return [80.0, 80.0]
    assert (await vmaf_target.search(odd, 93, start=29))["cq"] == 14 and 14 in asked


@pytest.mark.asyncio
async def test_the_floor_can_be_the_only_pass():
    async def score(cq):
        return [93.0, 93.0] if cq <= 14 else [80.0, 80.0]
    r = await vmaf_target.search(score, 93, start=29)
    assert (r["cq"], r["reached"]) == (14, True)


@pytest.mark.asyncio
async def test_a_hard_sample_fails_the_quality():
    async def score(cq):
        return [96.0, 96.0, 90.5]  # mean 94.2 but one sample 2.5 under
    assert not (await vmaf_target.search(score, 93, start=30))["reached"]
    assert vmaf_target.passes([95.0, 95.0, 91.0], 93) and not vmaf_target.passes([95.0, 95.0, 90.9], 93)


@pytest.mark.asyncio
async def test_a_half_step():
    async def score(cq):
        return [111.0 - cq * 1.0] * 2  # passes up to CQ 18.0; 18.5 → 92.5 fails
    assert (await vmaf_target.search(score, 93, start=20, half_steps=True))["cq"] == 18.0

    async def gentle(cq):
        return [102.0 - cq * 0.5] * 2  # 18.0 → 93.0, 18.5 → 92.75: stays 18
    assert (await vmaf_target.search(gentle, 93, start=20, half_steps=True))["cq"] == 18.0

    async def flat(cq):
        return [100.6 - cq * 0.4] * 2  # 19.0 → 93.0, 19.5 → 92.8 fails; 19 kept
    assert (await vmaf_target.search(flat, 93, start=20, half_steps=True))["cq"] == 19.0

    async def fine(cq):
        return [111.7 - cq * 1.0] * 2  # 18.0 → 93.7, 18.5 → 93.2 passes, 19.0 → 92.7 fails
    r = await vmaf_target.search(fine, 93, start=20, half_steps=True)
    assert (r["cq"], r["reached"], r["vmaf"]) == (18.5, True, 93.2) and 18.5 in r["scores"]
    assert (await vmaf_target.search(fine, 93, start=20))["cq"] == 18  # whole steps: no 18.5
    assert (await vmaf_target.search(fine, 93, start=18.5, half_steps=True))["cq"] == 18.5  # a remembered half step


@pytest.mark.asyncio
async def test_one_scoring_per_encoder_setting():  # VideoToolbox: CQ ≥ 29 is all q40
    asked = []

    async def score(cq):
        asked.append(cq)
        return [99.0, 99.0]
    r = await vmaf_target.search(score, 93, start=30, setting_key=lambda cq: min(cq, 29))
    assert len([cq for cq in asked if cq >= 29]) == 1 and r["rounds"] == 1
    assert r["cq"] == 42 and set(r["scores"]) == {30, 32, 36, 42}  # every CQ the steps reached


def test_sample_points():
    assert len(vmaf_target.sample_points(25 * 60)) == 3 and len(vmaf_target.sample_points(120 * 60)) == 6
    assert len(vmaf_target.sample_points(50 * 60)) == 6 and len(vmaf_target.sample_points(150)) == 3
    assert vmaf_target.sample_points(1500)[0] == pytest.approx(1500 / 6 - 5)  # centred on (i+0.5)/n
    assert all(0 <= s <= 1500 - 10 for s in vmaf_target.sample_points(1500))


def test_fractional_steps():
    assert vmaf_target.fractional_steps("nvenc", None) and vmaf_target.fractional_steps("libx265", "hevc")
    assert not any(vmaf_target.fractional_steps(e, None) for e in ("qsv", "vaapi", "videotoolbox"))
    assert not vmaf_target.fractional_steps("nvenc", "av1")


def _tools() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libvmaf" in filters and "libx265" in encoders


needs_vmaf = pytest.mark.skipif(not _tools(), reason="needs ffmpeg with libvmaf and libx265")


@pytest.fixture
def quick(monkeypatch, tmp_path):
    """Short samples of a short, noisy clip — quality that varies with CQ."""
    monkeypatch.setattr(vmaf_target, "sample_points", lambda duration: [4.0, 12.0])
    monkeypatch.setattr(vmaf_target, "SAMPLE_SECONDS", 3)
    monkeypatch.setattr(vmaf_target, "MIN_DURATION", 10)
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=320x240:r=24:d=20,noise=alls=25:allf=t", "-c:v", "libx264", "-crf", "8",
                    "-preset", "ultrafast", str(clip)], check=True)
    return clip


@needs_vmaf
@pytest.mark.asyncio
async def test_the_quality_found(test_db, quick):
    kwargs = {"override_libx265_preset": "ultrafast"}
    result = await vmaf_target.find_quality(str(quick), "libx265", 20.0, 90.0, start=24, convert_kwargs=kwargs)
    scores = result["scores"]
    assert result["target"] == 90.0 and vmaf_target.CQ_BEST <= result["cq"] <= vmaf_target.CQ_WORST
    assert result["samples"] == 2 and result["seconds"] > 0

    def holds(s):  # a quality's {"mean", "min"} against the target, as vmaf_target.passes
        return s["mean"] >= 90 and s["min"] >= 90 - vmaf_target.MAX_SAMPLE_DIP
    if result["reached"]:
        assert holds(scores[result["cq"]])
        above = scores.get(result["cq"] + (0.5 if vmaf_target.fractional_steps("libx265", None) else 1))
        assert above is None or not holds(above)  # the next step the search tried fails
    assert scores[min(scores)]["mean"] > scores[max(scores)]["mean"]  # better quality, higher score
    assert await vmaf_target.find_quality(str(quick), "libx265", 9.0, 90.0, start=24) is None  # too short


@needs_vmaf
@pytest.mark.asyncio
async def test_the_encoder_decides_the_steps(test_db, quick, monkeypatch):
    """Half steps for NVENC and libx265 HEVC only; VideoToolbox's CQs that
    map to one q:v are scored once."""
    asked = {}

    async def fake_search(score, target, start, **kw):
        asked.update(kw)
        return {"cq": 20, "vmaf": 93.0, "min": 92.0, "reached": True, "rounds": 1, "scores": {}}
    monkeypatch.setattr(vmaf_target, "search", fake_search)
    for encoder, codec, half in (("libx265", None, True), ("nvenc", None, True), ("libx265", "av1", False),
                                 ("qsv", None, False), ("videotoolbox", None, False)):
        await vmaf_target.find_quality(str(quick), encoder, 20.0, 93.0, start=24, output_codec=codec)
        assert asked["half_steps"] is half, (encoder, codec)
    key = asked["setting_key"]  # VideoToolbox's, from the last call
    assert key(30) == key(36) == key(42) == key(29) and key(16) != key(24)
    await vmaf_target.find_quality(str(quick), "libx265", 20.0, 93.0, start=24)
    key = asked["setting_key"]
    assert key(20) != key(21) and key(20.5) not in (key(20), key(21))


@needs_vmaf
@pytest.mark.asyncio
async def test_a_conversion_uses_it(test_db, quick, monkeypatch):
    from backend.converter import convert_file
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("vmaf_target_enabled", "true"), ("vmaf_target_score", "90"), ("vmaf_analysis_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    searched = []
    real = vmaf_target.find_quality

    async def spy(*a, **kw):
        searched.append(result := await real(*a, **kw))
        return result
    monkeypatch.setattr(vmaf_target, "find_quality", spy)
    import backend.converter as converter
    real_pass = converter._run_libvmaf_pass

    async def check(**kw):  # the finished file holds the target: no re-encode (test_vmaf_target_reencode.py)
        if kw["step_label"] != "VMAF analysis":
            return await real_pass(**kw)  # the search's samples
        return {"score": 91.0, "min": 91.0, "max": 91.0, "harmonic_mean": 91.0, "error": None, "stderr_tail": [],
                "seek": kw["seek"]}
    monkeypatch.setattr(converter, "_run_libvmaf_pass", check)
    said = []

    async def progress(**kw):
        said.append(kw)
    result = await convert_file(str(quick), "libx265", 20.0, override_crf=30, override_libx265_preset="ultrafast",
                                progress_callback=progress)
    assert result["success"], result.get("error")
    stats = result["encoding_stats"]
    [found] = searched
    keys = ("target", "cq", "vmaf", "min", "reached", "rounds", "samples", "seconds")
    assert {k: stats["vmaf_target"][k] for k in keys} == {k: found[k] for k in keys}
    # the finished file is checked though VMAF analysis is off (test_vmaf_target_reencode.py)
    assert stats["vmaf_target"]["checked_score"] == result["vmaf_score"] == 91.0
    assert stats["crf"] == found["cq"] + 2  # libx265's CRF, from the CQ found — not the job's 30
    assert 28 in found["scores"]  # the search started from the job's quality (CRF 30)
    rounds = [s for s in said if s.get("step_key") == "steps.vmafTargetRound"]  # each round names itself
    assert rounds[0]["step"] == "VMAF target: CQ 28 (round 1)" and rounds[0]["step_params"] == {"cq": 28, "round": 1}
    assert {s["step_params"]["round"] for s in rounds} == set(range(1, found["rounds"] + 1))


@needs_vmaf
@pytest.mark.asyncio
async def test_the_remembered_start(test_db, quick, monkeypatch):
    from backend.converter import convert_file
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("vmaf_target_enabled", "true"), ("vmaf_target_score", "90"), ("vmaf_analysis_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    starts = []

    async def spy(*a, start=None, **kw):
        starts.append(start)  # no search: the job keeps its own quality
    monkeypatch.setattr(vmaf_target, "find_quality", spy)
    result = await convert_file(str(quick), "libx265", 20.0, override_crf=30, override_libx265_preset="ultrafast",
                                vmaf_target_start_cq=26)
    assert result["success"], result.get("error")
    assert starts == [26]  # not the job's own (CRF 30 is CQ 28)


def _endless_samples(monkeypatch, sampling):
    """The search's sample encodes run a minute (sleep 60); `sampling()` is
    called as each one starts."""
    import backend.converter as converter
    real = converter.convert_file

    async def convert(*a, **kw):
        if not kw.get("command_only"):
            return await real(*a, **kw)
        plan = await real(*a, **kw)
        sampling()
        return {**plan, "command": ["sleep", "60"]}
    monkeypatch.setattr(converter, "convert_file", convert)


@needs_vmaf
@pytest.mark.asyncio
async def test_a_cancel_stops_the_search(test_db, quick, monkeypatch):
    """A Cancel kills the sample being encoded: the search ends there, and
    the encode it'd have led to is killed as it starts — the job doesn't
    wait out the search's rounds."""
    import asyncio
    import sys

    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    # the queue's cancel path asks backend.main's node manager: a stand-in
    monkeypatch.setitem(sys.modules, "backend.main", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("vmaf_target_enabled", "true"), ("vmaf_target_score", "90"), ("vmaf_analysis_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    queue, worker = JobQueue(test_db), QueueWorker(test_db)
    job_id = await queue.add_job(str(quick), "convert", encoder="libx265")
    loop = asyncio.get_running_loop()
    _endless_samples(monkeypatch, lambda: loop.call_later(
        0.5, lambda: asyncio.ensure_future(worker.cancel_current(job_id))))
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await asyncio.wait_for(worker._process_job(job), timeout=30)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            assert (await cur.fetchone())[0] == "cancelled"
    assert [p.name for p in quick.parent.glob("*.mkv")] == [quick.name]  # nothing placed or left


@needs_vmaf
@pytest.mark.asyncio
async def test_a_remote_workers_cancel_stops_the_search(quick, monkeypatch):
    import asyncio

    from backend import worker_mode
    state = SimpleNamespace(sampling=False, completed=[])

    class Client:
        async def report_progress(self, *a, **kw):
            return state.sampling  # cancelled once a sample is being encoded

        async def report_complete(self, node_id, job_id, success, **kw):
            state.completed.append((success, kw.get("error_key")))
    _endless_samples(monkeypatch, lambda: setattr(state, "sampling", True))
    job = {"id": 7, "file_path": str(quick), "job_type": "convert", "encoder": "libx265",
           "libx265_preset": "ultrafast", "vmaf_target_enabled": True, "vmaf_target_score": 90.0}
    await asyncio.wait_for(worker_mode.execute_job(Client(), "node-1", job, ["libx265"]), timeout=30)
    assert state.completed == [(False, "errors.cancelledByUser")]
    assert [p.name for p in quick.parent.glob("*.mkv")] == [quick.name]  # nothing placed or left


@pytest.mark.asyncio
async def test_remote_workers_get_the_settings(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    await JobQueue(test_db).add_job("/m/Film/film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        for k, v in (("vmaf_target_enabled", "true"), ("vmaf_target_score", "93")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert (job["vmaf_target_enabled"], job["vmaf_target_score"]) == (True, 93.0)


@pytest.mark.asyncio
async def test_a_workers_round_step_reaches_the_queue(test_db, node_api, monkeypatch):
    """The round's step has parameters, which the server can't derive from
    its English label like the other steps' keys."""
    import backend.routes.nodes as nodes_route
    from backend.tests.test_node_ownership import _job, _node
    _, request = node_api
    job_id = await _job(test_db, "running", "node-1")
    await _node(test_db, "node-1", 0, job_id)
    sent = []

    async def capture(msg):
        sent.append(msg)
    monkeypatch.setattr(nodes_route.ws_manager, "broadcast", capture)
    for step, extra in (("VMAF target: CQ 32 (round 3)", {"step_key": "steps.vmafTargetRound",
                                                         "step_params": {"cq": 32, "round": 3}}),
                        ("VMAF analysis", {})):
        await nodes_route.report_progress(
            nodes_route.ProgressReport(node_id="node-1", job_id=job_id, step=step, **extra), request)
    assert (sent[0]["step_key"], sent[0]["step_params"]) == ("steps.vmafTargetRound", {"cq": 32, "round": 3})
    assert sent[1]["step_key"] == "steps.vmafAnalysis" and "step_params" not in sent[1]  # as before


@pytest.mark.asyncio
async def test_the_setting_is_kept_in_range(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.update_encoding_settings(SettingsUpdate(vmaf_target_enabled=True, vmaf_target_score=120))
    got = await settings_route.get_encoding_settings()
    assert (got["vmaf_target_enabled"], got["vmaf_target_score"]) == (True, 99.0)
