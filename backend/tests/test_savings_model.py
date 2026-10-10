"""The savings estimate learns from finished conversions (v0.10.0): a factor
on the CQ curve per bits-per-pixel band and source codec, shrunk toward the
broader group; stored per-file estimates follow it and the default quality."""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend import savings_model as sm
from backend.encoding_estimates import cq_to_savings_pct

GB = 1024 ** 3
HOUR = 3600
# 1080p, an hour: 1 GB is ~0.046 bits per pixel (band 0), 8 GB ~0.37 (band 3)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(sm, "_model", sm.SavingsModel())
    monkeypatch.setattr(sm, "_state", {"counter": None, "built": 0.0, "cq": None})


def test_bands_and_codecs():
    assert sm.band(sm.bits_per_pixel(GB, HOUR, 1920, 1080)) == 0
    assert sm.band(sm.bits_per_pixel(8 * GB, HOUR, 1920, 1080)) == 3
    assert sm.band(sm.bits_per_pixel(8 * GB, HOUR, 0, 1080)) == 3  # no width: 16:9
    assert sm.band(sm.bits_per_pixel(30 * GB, HOUR, 1920, 1080)) == 4
    assert sm.bits_per_pixel(GB, 0, 1920, 1080) is None and sm.band(None) is None
    assert [sm.codec_family(c) for c in ("h264", "AVC", "hevc", "mpeg2video", "vc1", "msmpeg4v3", "av1", None)] == [
        "h264", "h264", "hevc", "mpeg2", "vc1", "mpeg4", None, None]


def test_the_curve_until_it_learns_then_shrunk_groups():
    assert sm.SavingsModel().video_pct(20, file_size=8 * GB, duration=HOUR, width=1920, height=1080,
                                       codec="h264") == cq_to_savings_pct(20)
    model = sm.fit([(1.5, 3, "h264")] * 30 + [(0.5, 0, "h264")] * 10 + [(1.0, None, None)] * 10)
    assert model.samples == 50
    glob = (30 * 1.5 + 10 * 0.5 + 10 * 1.0 + 10) / 60
    band3 = (30 * 1.5 + 10 * glob) / 40
    assert model.factor(None, None) == pytest.approx(glob)
    assert model.factor(3, None) == pytest.approx(band3)
    assert model.factor(3, "h264") == pytest.approx((30 * 1.5 + 10 * band3) / 40)
    assert model.factor(3, "mpeg2") == model.factor(3, None)  # no MPEG-2 seen: the band's
    assert model.factor(1, "h264") == model.factor(None, None)  # no band 1 seen: everything's
    big = model.video_pct(20, file_size=8 * GB, duration=HOUR, width=1920, height=1080, codec="h264")
    small = model.video_pct(20, file_size=GB, duration=HOUR, width=1920, height=1080, codec="h264")
    assert big > cq_to_savings_pct(20) > small
    assert sm.fit([(2.5, 3, "h264")] * 1000).video_pct(26, file_size=8 * GB, duration=HOUR, height=1080) == 0.95


def _row(**kw):
    stats = {"in_size": 8 * GB, "out_size": 2 * GB, "duration": HOUR, "encoder": "nvenc", "cq": 20,
             "source_codec": "h264", "source_width": 1920, "source_height": 1080,
             "audio_tracks_to_remove": "[]", "original_file_path": "/m/Film.2020.1080p.BluRay.x264.mkv"}
    return {**stats, **kw}


def test_which_jobs_teach_it():
    ratio = 0.75 / cq_to_savings_pct(20)
    assert sm.sample(_row()) == (pytest.approx(ratio), 3, "h264")
    assert sm.sample(_row(encoder="libx265", cq=None, crf=24))[0] == pytest.approx(0.75 / cq_to_savings_pct(22))
    assert sm.sample(_row(out_size=9 * GB))[0] == 0  # a discarded larger output: no savings
    for skipped in (dict(output_codec="av1"), dict(vmaf_target='{"cq": 22}'), dict(audio_converted=1),
                    dict(lossless="eac3"), dict(audio_tracks_to_remove="[2]"), dict(in_size=0),
                    dict(out_size=None)):
        assert sm.sample(_row(**skipped)) is None, skipped
    # Older jobs: the codec from the original name, the size from the output's scan row
    older = _row(source_codec=None, source_width=None, source_height=None, video_width=1920, video_height=1080)
    assert sm.sample(older)[1:] == (3, "h264")
    assert sm.sample({**older, "target_resolution": "720p"})[1] is None  # resized: the output's size isn't the source's
    assert sm.sample({**older, "original_file_path": None})[2] is None


async def _add_job(db, i, in_gb, out_gb, codec="h264", status="completed", job_type="convert", **stats):
    stats = {"input_size": int(in_gb * GB), "output_size": int(out_gb * GB), "duration": HOUR, "encoder": "nvenc",
             "cq": 20, "output_codec": "hevc", "audio_converted_from": [], "source_codec": codec,
             "source_width": 1920, "source_height": 1080, **stats}
    await db.execute("INSERT INTO jobs (file_path, job_type, status, encoding_stats, created_at, audio_tracks_to_remove) "
                     "VALUES (?, ?, ?, ?, 'x', '[]')", (f"/m/done/{i}.mkv", job_type, status, json.dumps(stats)))


async def _scan(db, path, size_gb, codec="h264"):
    await db.execute("INSERT INTO scan_results (file_path, file_size, duration, video_codec, video_width, video_height, "
                     "needs_conversion, scan_timestamp, video_conv_savings_bytes) VALUES (?, ?, ?, ?, 1920, 1080, 1, 'x', 0)",
                     (path, int(size_gb * GB), HOUR, codec))


async def _stored(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, video_conv_savings_bytes FROM scan_results") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_it_learns_and_the_stored_estimates_follow(test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await _scan(db, "/m/remux.mkv", 8)
        await _scan(db, "/m/web.mkv", 1)
        await db.commit()
    await sm.tick()
    assert await _stored(test_db) == {"/m/remux.mkv": int(8 * GB * cq_to_savings_pct(20)),
                                      "/m/web.mkv": int(GB * cq_to_savings_pct(20))}
    async with aiosqlite.connect(test_db) as db:  # big files shrank by 80 %, small ones by 10 %
        for i in range(20):
            await _add_job(db, i, 8, 1.6)
            await _add_job(db, 100 + i, 1, 0.9)
        await _add_job(db, 200, 8, 1, output_codec="av1")  # not samples
        await _add_job(db, 201, 8, 1, status="failed")
        await _add_job(db, 202, 8, 1, job_type="audio")
        await db.commit()
    await sm.tick()  # rebuilt at most every REBUILD_SECONDS
    assert sm.current().samples == 0
    monkeypatch.setattr(sm, "REBUILD_SECONDS", 0)
    await sm.tick()
    assert sm.current().samples == 40
    stored = await _stored(test_db)
    assert stored["/m/remux.mkv"] > 0.7 * 8 * GB and stored["/m/web.mkv"] < 0.2 * GB
    async with aiosqlite.connect(test_db) as db:  # the default quality changed
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('nvenc_cq', '26')")
        await db.commit()
    await sm.tick()
    model = sm.current()
    assert (await _stored(test_db))["/m/web.mkv"] == int(GB * model.video_pct(
        26, file_size=GB, duration=HOUR, width=1920, height=1080, codec="h264"))


@pytest.mark.asyncio
async def test_the_quality_presets_say_it(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    monkeypatch.setattr(sm, "_model", sm.SavingsModel({(): 0.5}, samples=120))
    got = await settings_route.get_quality_presets()
    assert got["learned_from"] == 120
    assert [p["savings_pct"] for p in got["presets"]] == [
        round(cq_to_savings_pct(p["cq"]) * 0.5 * 100) for p in got["presets"]]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_scans_estimate_with_it(test_db, tmp_path, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    from backend.scanner import probe_file, scan_directory
    from backend.watcher import scanned_from_probe
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    film = tmp_path / "media" / "Film (2020)" / "Film.mkv"
    film.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-c:v", "mpeg4", "-q:v", "2", str(film)], check=True)
    probe = await probe_file(str(film))
    size = film.stat().st_size
    b = sm.band(sm.bits_per_pixel(size, probe["duration"], 320, 240))
    monkeypatch.setattr(sm, "_model", sm.SavingsModel({(): 1.0, (b,): 1.0, (b, "mpeg4"): 0.5}, samples=40))
    expected = int(size * cq_to_savings_pct(23) * 0.5)
    watched = await scanned_from_probe(str(film), probe, ["mpeg4"], 23)
    assert watched.video_conv_savings_bytes == expected and watched.estimated_savings_bytes == expected
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('nvenc_cq', '23')")
        await db.commit()
    (scanned,) = await scan_directory(str(tmp_path / "media"))
    assert scanned.video_conv_savings_bytes == expected


def _x265() -> bool:
    return bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@pytest.mark.skipif(not _x265(), reason="needs ffmpeg with libx265")
@pytest.mark.asyncio
async def test_a_conversion_records_what_it_learns_from(test_db, tmp_path, monkeypatch):
    import sys
    from backend import bazarr
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda *a, **kw: None)
    src = tmp_path / "media" / "Film (2020)" / "Film (2020) x264.mkv"
    src.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-c:v", "mpeg4", "-q:v", "1", str(src)], check=True)
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("backup_original_days", "0"), ("trash_original_after_conversion", "false"),
                     ("vmaf_analysis_enabled", "false")):
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
        async with db.execute("SELECT status, encoding_stats FROM jobs WHERE id = ?", (job_id,)) as cur:
            status, stats = await cur.fetchone()
    stats = json.loads(stats)
    assert status == "completed"
    assert (stats["source_codec"], stats["source_width"], stats["source_height"]) == ("mpeg4", 320, 240)
    model = await sm.rebuild()
    assert model.samples == 1 and set(model.factors) == {(), (sm.band(sm.bits_per_pixel(
        stats["input_size"], stats["duration"], 320, 240)),), (sm.band(sm.bits_per_pixel(
            stats["input_size"], stats["duration"], 320, 240)), "mpeg4")}
