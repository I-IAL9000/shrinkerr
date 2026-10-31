"""A VMAF target a file can't reach (v0.10.0).

Grainy footage levels off below 93 whatever the quality: on the
maintainer's reality show the samples scored 89.2 at CQ 30, 92.5 at CQ 22
and 92.7 at CQ 14. The search took "the best in range", CQ 14; the file
came out 11% larger than the original and was discarded after a 12-minute
encode. Now: the highest CQ within a point of the best score, no re-encode
(it can't do better), and a quality whose samples come out larger than the
original's isn't encoded at all.
"""
import os
import shutil
import subprocess

import pytest

from backend import vmaf_target
from backend.vmaf_target import KNEE_POINTS, search


def _levels_off(best: float = 92.8):
    """Samples that level off at `best` below CQ 20 and fall 0.6 a step above."""
    tried = []

    async def score(cq):
        tried.append(cq)
        mean = best - max(0.0, cq - 20) * 0.6
        return [mean + 1, mean - 1]
    return score, tried


@pytest.mark.asyncio
async def test_out_of_reach_settles_where_the_score_stops_improving():
    score, tried = _levels_off()
    found = await search(score, 93, 36)
    assert found["reached"] is False and found["best"] == 92.8
    # the highest CQ within KNEE_POINTS of the best: 92.8 - (cq - 20) * 0.6 >= 91.8
    assert found["cq"] == 21 and found["vmaf"] >= found["best"] - KNEE_POINTS
    assert 22 in tried and found["vmaf"] - score_at(22) > 0  # 22 was tried and is under the line
    assert found["cq"] != 14


def score_at(cq, best=92.8):
    return best - max(0.0, cq - 20) * 0.6


@pytest.mark.asyncio
async def test_a_reachable_target_is_unchanged():
    score, _ = _levels_off(best=96)
    found = await search(score, 93, 36)
    assert found["reached"] is True and found.get("best") is None
    assert score_at(found["cq"], 96) >= 93


async def _find(tmp_path, monkeypatch, vmaf):
    """find_quality on three fake samples whose encodes `vmaf` scores (and
    sizes)."""
    work = tmp_path / "work"
    work.mkdir()

    async def cut(input_path, duration, w, **kw):
        out = []
        for n in range(3):
            p = os.path.join(w, f"sample{n}.mkv")
            with open(p, "wb") as f:
                f.write(b"s" * 1000)
            out.append(p)
        return out
    monkeypatch.setattr(vmaf_target, "_cut_samples", cut)

    async def plan(sample, encoder, seconds, override_cq=None, **kw):
        return {"command": ["true"], "output_path": f"{sample}.{override_cq}.out"}
    import backend.converter as converter
    monkeypatch.setattr(converter, "convert_file", plan)

    async def run(cmd, timeout, on_proc=None):
        return True
    monkeypatch.setattr(vmaf_target, "_run", run)
    monkeypatch.setattr(vmaf_target, "_vmaf", vmaf)

    async def available():
        return True
    import backend.test_encode as test_encode
    monkeypatch.setattr(test_encode, "check_vmaf_available", available)
    monkeypatch.setattr(vmaf_target.tempfile, "mkdtemp", lambda prefix="": str(work))
    return await vmaf_target.find_quality("/m/show.mkv", "nvenc", 2500.0, 93, start=36)


@pytest.mark.asyncio
async def test_samples_larger_than_the_originals_are_flagged(tmp_path, monkeypatch):
    """The search has encoded samples at each quality: their size says
    whether the file would come out larger, before a full encode."""
    async def vmaf(ref, enc, seconds, json_path):
        cq = float(enc.rsplit(".", 2)[-2])
        with open(enc, "wb") as f:  # a lower CQ, a bigger sample: 1100 bytes at CQ 20, 900 at 30
            f.write(b"e" * int(1000 + (25 - cq) * 20))
        return 92.8 - max(0.0, cq - 20) * 0.6

    found = await _find(tmp_path, monkeypatch, vmaf)

    assert found["reached"] is False and found["cq"] == 21
    assert found["larger"] is True and found["size_ratio"] == pytest.approx(8.0)  # 1080 bytes vs 1000


@pytest.mark.asyncio
async def test_samples_that_score_low_at_every_quality_arent_trusted(tmp_path, monkeypatch):
    """The Nest (2002): its samples scored 31 at CQ 14 and at CQ 37 alike —
    compared with the wrong frames, not encoded badly (the finished file
    scored 88.7). Out of reach, the search took the highest CQ near that
    best, 37. Now the job keeps its own quality, and its report says how low
    they scored (it said a sample couldn't be cut or scored)."""
    async def vmaf(ref, enc, seconds, json_path):
        cq = float(enc.rsplit(".", 2)[-2])
        open(enc, "wb").close()
        return 31.6 - max(0.0, cq - 20) * 0.05

    found = await _find(tmp_path, monkeypatch, vmaf)
    assert found["untrusted"] is True and found["best"] == 31.6 and "cq" not in found


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_the_report_says_the_samples_werent_trusted(test_db, tmp_path, monkeypatch):
    """Stolen Kisses' report said "a sample couldn't be cut or scored": its
    samples had scored, too low at every quality to be trusted (a 4K film
    resized to 1080p, compared at 4K)."""
    import aiosqlite
    from backend.converter import convert_file
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("vmaf_target_enabled", "true"), ("vmaf_target_score", "93"),
                     ("vmaf_analysis_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=130",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    outcomes = iter([{"untrusted": True, "best": 68.8, "rounds": 3, "seconds": 40.0}, None])

    async def search(*a, **kw):
        return next(outcomes)
    monkeypatch.setattr(vmaf_target, "find_quality", search)

    untrusted = await convert_file(str(clip), "libx265", 130.0, override_crf=30, override_libx265_preset="ultrafast")
    assert untrusted["success"], untrusted.get("error")
    stats = untrusted["encoding_stats"]
    assert stats["vmaf_target"] is None and stats["crf"] == 30  # the job's own quality
    assert stats["vmaf_target_untested"] == 93.0 and stats["vmaf_target_untrusted"] == 68.8

    clip2 = tmp_path / "Film (2021).mkv"
    shutil.copy(clip, clip2)
    failed = await convert_file(str(clip2), "libx265", 130.0, override_crf=30, override_libx265_preset="ultrafast")
    assert failed["success"], failed.get("error")
    assert failed["encoding_stats"]["vmaf_target_untested"] == 93.0  # a sample failed: no score to say
    assert failed["encoding_stats"]["vmaf_target_untrusted"] is None
