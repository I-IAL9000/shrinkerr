"""The test encode runs the job's own command (v0.10.0). It had its own: libx265
always used preset `medium` at CQ + 2, QSV / VAAPI fell back to libx265, and
bit depth, hardware decode, rules and resolution were ignored."""
import shutil
import subprocess

import aiosqlite
import pytest


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    return "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                        capture_output=True, text=True).stdout


def _clip(path, seconds=12):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x240:r=25:d={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", str(path)], check=True)


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_the_converter_hands_back_the_jobs_command(test_db, tmp_path):
    from backend.converter import convert_file
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    clip = folder / "Film (2020).mkv"
    _clip(clip, 2)
    plan = await convert_file(str(clip), "nvenc", 2.0, override_cq=23, override_preset="p4", command_only=True)
    cmd = plan["command"]
    assert _arg(cmd, "-c:v") == "hevc_nvenc" and _arg(cmd, "-cq") == "23" and _arg(cmd, "-preset") == "p4"
    assert cmd[-1] == plan["output_path"]
    assert [p.name for p in folder.iterdir()] == ["Film (2020).mkv"]  # nothing ran


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_the_test_encode_uses_the_dialogs_preset_and_crf(test_db, tmp_path, monkeypatch):
    import backend.converter as converter
    import backend.test_encode as test_encode
    from backend.routes.jobs import TestEncodeRequest, start_test_encode
    monkeypatch.setattr(test_encode, "TEMP_DIR", tmp_path / "samples")
    plans = []
    real = converter.convert_file

    async def spy(*args, **kwargs):
        plan = await real(*args, **kwargs)
        plans.append(plan)
        return plan
    monkeypatch.setattr(converter, "convert_file", spy)
    clip = tmp_path / "Film (2020).mkv"
    _clip(clip)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('vmaf_analysis_enabled', 'false')")
        await db.commit()

    result = await start_test_encode(TestEncodeRequest(
        file_path=str(clip), encoder="libx265", cq=24, preset="ultrafast"))
    assert result["status"] == "complete", result.get("error")
    cmd = plans[0]["command"]
    assert _arg(cmd, "-c:v") == "libx265"
    assert _arg(cmd, "-preset") == "ultrafast"      # was always "medium"
    assert _arg(cmd, "-crf") == "24"                # was the slider + 2
    assert result["encoded_size"] > 0
    assert result["encoding_fps"] > 0  # a short sample reported 0 fps
