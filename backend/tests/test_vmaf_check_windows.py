"""The after-conversion VMAF check scores a file where the VMAF target
samples it (v0.10.0): 3 to 6 windows of 10 s across the file, not one
30 s window a third of the way in. The Night Eats the World's encode
scored 98.4 in that window, 92.93 at the six sample points — where the
search's samples had said 93.08 — so the check suggested room the encode
didn't have. A disc keeps its one window (read from the start)."""
import shutil
import subprocess

import aiosqlite
import pytest

import backend.converter as converter
from backend import vmaf_target


def _pass(score, low=None, high=100.0):
    return {"score": score, "min": score - 5 if low is None else low, "max": high,
            "harmonic_mean": score - 1, "error": None, "stderr_tail": []}


@pytest.mark.asyncio
async def test_the_windows_are_one_score(monkeypatch):
    by_start = {100.0: _pass(93.9), 300.0: _pass(30.0, low=2.0, high=99.0), 500.0: _pass(90.9)}
    progress = []

    async def libvmaf(*, seek, duration, progress_callback=None, **kw):
        assert duration == 10
        if progress_callback:
            await progress_callback(progress=0, fps=0, eta_seconds=None, step="VMAF analysis")
            await progress_callback(progress=50, fps=30, eta_seconds=5, step="VMAF analysis")
        return {**by_start[seek], "seek": seek}
    monkeypatch.setattr(converter, "_run_libvmaf_pass", libvmaf)

    async def said(**kw):
        progress.append(kw["progress"])
    result = await converter._run_libvmaf_windows([100.0, 300.0, 500.0], 10, progress_callback=said)

    # 300 s desynced (frames near 0 and near 100): left out while others scored
    assert result["score"] == round((93.9 + 90.9) / 2, 1) and result["min"] == 85.9 and result["max"] == 100.0
    assert result["seek"] == 500.0  # the lowest window, for the cross-check
    assert result["windows"] == [(100.0, 93.9), (300.0, 30.0), (500.0, 90.9)]
    assert progress == sorted(progress) and progress[-1] < 100  # once across the windows


def _tools() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libvmaf" in filters and "libx265" in encoders


@pytest.mark.asyncio
@pytest.mark.skipif(not _tools(), reason="needs ffmpeg with libvmaf and libx265")
async def test_a_conversion_is_checked_at_the_sample_points(test_db, tmp_path, monkeypatch):
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=90",
                    "-c:v", "libx264", "-crf", "8", "-preset", "ultrafast", str(clip)], check=True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('vmaf_analysis_enabled', 'true')")
        await db.commit()
    seeks = []
    real = converter._run_libvmaf_pass

    async def spy(**kw):
        seeks.append((kw["seek"], kw["duration"]))
        return await real(**kw)
    monkeypatch.setattr(converter, "_run_libvmaf_pass", spy)

    result = await converter.convert_file(str(clip), "libx265", 90.0, override_crf=20,
                                          override_libx265_preset="ultrafast")

    assert result["success"], result.get("error")
    assert [start for start, _ in seeks] == pytest.approx(vmaf_target.sample_points(90.0))  # three, at 10 s each
    assert len(seeks) == 3 and all(seconds == 10.0 for _, seconds in seeks)
    assert result["vmaf_score"] > 90
