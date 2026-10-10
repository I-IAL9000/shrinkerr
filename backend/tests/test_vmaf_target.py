"""VMAF target quality (v0.10.0): each title's quality is found on a few
encoded samples — the lowest that still scores the target VMAF — and the
file is encoded at it."""
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest

from backend import vmaf_target


@pytest.mark.asyncio
async def test_the_search():
    asked = []

    async def score(cq):  # one point of VMAF per CQ step
        asked.append(cq)
        return 110.0 - cq
    result = await vmaf_target.search(score, 95, start=20)
    assert (result["cq"], result["vmaf"], result["reached"]) == (15, 95.0, True)
    assert asked[0] == 20 and len(asked) <= 6 and len(set(asked)) == len(asked)
    assert (await vmaf_target.search(score, 99, start=20))["cq"] == 14  # out of reach: the best in range
    assert not (await vmaf_target.search(score, 99, start=20))["reached"]
    assert (await vmaf_target.search(score, 50, start=60))["cq"] == 34  # always reached: the smallest


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
    monkeypatch.setattr(vmaf_target, "SAMPLE_AT", (0.3, 0.7))
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
    if result["reached"]:
        assert scores[result["cq"]] >= 90 and scores.get(result["cq"] + 1, 0) < 90
    assert scores[min(scores)] > scores[max(scores)]  # better quality, higher score
    assert await vmaf_target.find_quality(str(quick), "libx265", 9.0, 90.0, start=24) is None  # too short


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
    result = await convert_file(str(quick), "libx265", 20.0, override_crf=30, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    stats = result["encoding_stats"]
    [found] = searched
    assert stats["vmaf_target"] == {k: found[k] for k in ("target", "cq", "vmaf", "reached")}
    assert stats["crf"] == found["cq"] + 2  # libx265's CRF, from the CQ found — not the job's 30
    assert 28 in found["scores"]  # the search started from the job's quality (CRF 30)


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
async def test_the_setting_is_kept_in_range(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.update_encoding_settings(SettingsUpdate(vmaf_target_enabled=True, vmaf_target_score=120))
    got = await settings_route.get_encoding_settings()
    assert (got["vmaf_target_enabled"], got["vmaf_target_score"]) == (True, 99.0)
