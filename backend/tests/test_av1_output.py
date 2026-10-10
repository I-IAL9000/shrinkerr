"""AV1 output per rule (v0.10.0): a rule's output codec, encoded with the
encoder family's AV1 sibling — SVT-AV1 on the CPU, NVENC on RTX 40 and
later, QSV on Arc — or HEVC where there's none."""
import shutil
import subprocess

import aiosqlite
import pytest

from backend import encoder_caps
from backend.converter import _build_ffmpeg_cmd_impl, av1_quality, get_output_path


def _cmd(**kw) -> str:
    return " ".join(_build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", **kw))


def test_the_commands():
    svt = _cmd(encoder="libx265", crf=22, libx265_preset="slow", output_codec="av1")
    assert "-c:v libsvtav1 -preset 6 -crf 30 -pix_fmt yuv420p10le" in svt and "libx265" not in svt
    nvenc = _cmd(encoder="nvenc", cq=20, output_codec="av1")
    assert "-c:v av1_nvenc" in nvenc and "-cq 25" in nvenc and "-profile:v" not in nvenc
    assert "-c:v av1_qsv -preset medium -global_quality 22" in _cmd(encoder="qsv", qsv_cq=22, output_codec="av1")
    assert "-c:v libx265" in _cmd(encoder="libx265") and "-c:v hevc_nvenc" in _cmd(encoder="nvenc", output_codec="hevc")


def test_the_quality_scale():
    assert (av1_quality("libx265", 22), av1_quality("libx265", 60)) == (30, 63)
    assert (av1_quality("nvenc", 20), av1_quality("nvenc", 51)) == (25, 63)
    assert av1_quality("qsv", 22) == 22


def test_the_file_name():
    assert get_output_path("/m/Film (2020)/Film.2020.1080p.BluRay.x264-GRP.mkv", encoder="av1") == \
        "/m/Film (2020)/Film.2020.1080p.BluRay.AV1-GRP.mkv"


def test_which_encoders_have_it(monkeypatch):
    monkeypatch.setattr(encoder_caps, "_av1_cache", {})
    monkeypatch.setattr(encoder_caps, "_ffmpeg_encoders", lambda: {"libsvtav1", "av1_nvenc"})
    works = {"av1_nvenc": False}
    monkeypatch.setattr(encoder_caps, "_av1_test_encode", lambda name: works.get(name, False))
    assert encoder_caps.av1_encoder("libx265") == "libsvtav1"
    assert encoder_caps.av1_encoder("hevc_nvenc") is None  # listed, but this GPU can't (a P2200)
    assert encoder_caps.av1_encoder("qsv") is None  # not in this ffmpeg
    assert encoder_caps.av1_encoder("vaapi") is None and encoder_caps.av1_encoder("videotoolbox") is None
    monkeypatch.setattr(encoder_caps, "_av1_cache", {})
    works["av1_nvenc"] = True
    assert encoder_caps.av1_encoder("nvenc") == "av1_nvenc"


@pytest.mark.asyncio
async def test_the_caps_endpoint(monkeypatch):
    from backend.routes.stats import get_encoder_caps
    monkeypatch.setattr(encoder_caps, "av1_encoder", lambda enc: "libsvtav1" if enc == "libx265" else None)
    assert (await get_encoder_caps())["av1"]["libx265"] is True


@pytest.mark.asyncio
async def test_a_rule_gives_its_jobs_av1(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes import rules as route
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan, init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    await route.create_rule(route.RuleCreate(
        name="Anime in AV1", output_codec="av1",
        match_conditions=[route.MatchCondition(type="directory", value="/m/Anime")]))
    async with aiosqlite.connect(test_db) as db:
        for path in ("/m/Anime/Show/e1.mkv", "/m/Movies/Film/film.mkv"):
            await db.execute("INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, "
                             "audio_tracks_json, scan_timestamp) VALUES (?, 1000, 60, 'h264', 1, '[]', 'x')", (path,))
        await db.commit()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/Anime/Show/e1.mkv", "/m/Movies/Film/film.mkv"]))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, output_codec FROM jobs ORDER BY file_path") as cur:
            assert await cur.fetchall() == [("/m/Anime/Show/e1.mkv", "av1"), ("/m/Movies/Film/film.mkv", None)]
        async with db.execute("SELECT output_codec FROM encoding_rules") as cur:
            assert (await cur.fetchone())[0] == "av1"


def _svt() -> bool:
    return bool(shutil.which("ffmpeg")) and "libsvtav1" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def _codec(path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "Film.2020.1080p.WEB.x264-GRP.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-c:v", "libx264", "-qp", "0", str(path)], check=True)
    return path


@pytest.mark.skipif(not _svt(), reason="needs ffmpeg with libsvtav1")
@pytest.mark.asyncio
async def test_a_real_av1_encode(test_db, clip, monkeypatch):
    from backend.converter import convert_file
    monkeypatch.setattr(encoder_caps, "_av1_cache", {})
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast", output_codec="av1")
    assert result["success"], result.get("error")
    assert result["output_path"].endswith("Film.2020.1080p.WEB.AV1-GRP.mkv") and _codec(result["output_path"]) == "av1"
    stats = result["encoding_stats"]
    assert stats["output_codec"] == "av1" and "libsvtav1" in result["ffmpeg_command"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_hevc_where_there_is_no_av1(test_db, clip, monkeypatch):
    from backend.converter import convert_file
    monkeypatch.setattr(encoder_caps, "av1_encoder", lambda enc: None)
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast", output_codec="av1")
    assert result["success"], result.get("error")
    assert result["output_path"].endswith("x265-GRP.mkv") and _codec(result["output_path"]) == "hevc"
    assert result["encoding_stats"]["output_codec"] == "hevc"


@pytest.mark.asyncio
async def test_remote_workers_encode_it(tmp_path, monkeypatch):
    import backend.converter
    import backend.scanner
    from backend import worker_mode
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient
    src = tmp_path / "Movie (2009) h264.mkv"
    src.write_bytes(b"x")
    seen = {}

    async def probe(path, *a, **kw):
        return dict(PROBE)

    async def convert(**kwargs):
        seen.update(kwargs)
        return {"success": True, "output_path": str(src), "space_saved": 1, "error": None}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    monkeypatch.setattr(backend.converter, "convert_file", convert)
    await worker_mode.execute_job(FakeClient(), "node-1", {
        "id": 9, "file_path": str(src), "job_type": "convert", "encoder": "libx265", "output_codec": "av1"},
        ["libx265"])
    assert seen["output_codec"] == "av1"
