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


@pytest.mark.asyncio
async def test_one_scoring_per_encoder_setting():  # VideoToolbox: CQ ≥ 29 is all q40
    asked = []

    async def score(cq):
        asked.append(cq)
        return [99.0, 99.0]
    r = await vmaf_target.search(score, 93, start=30, setting_key=lambda cq: min(cq, 29))
    assert len([cq for cq in asked if cq >= 29]) == 1 and r["rounds"] == 1
    assert r["cq"] == 42 and set(r["scores"]) == {30, 32, 34, 36, 38, 40, 42}


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
        chosen, above = scores[result["cq"]], scores.get(result["cq"] + 1)
        assert chosen["mean"] >= 90 and chosen["min"] >= 88
        assert above is None or above["mean"] < 90 or above["min"] < 88
    assert scores[min(scores)]["mean"] > scores[max(scores)]["mean"]  # better quality, higher score
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
