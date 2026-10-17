"""Fix for direct play in Add to Queue (v0.10.0): ticked, the queued files
also get the fixes chosen below it (remux old containers, convert Hi10P,
SRT copies of image subtitles, a stereo compatibility track) where they
need them; every other file is queued as usual. Each job records the fixes
it makes; jobs queued before they could be chosen (1) make them all."""
import json

import aiosqlite
import pytest
import pytest_asyncio

from backend.direct_play import job_fixes, remux_fixes
from backend.rule_actions import settings_overrides

DTS = json.dumps([{"stream_index": 1, "language": "eng", "codec": "dts", "channels": 6}])
AAC = json.dumps([{"stream_index": 1, "language": "eng", "codec": "aac", "channels": 2}])
PGS = json.dumps([{"stream_index": 2, "language": "eng", "codec": "hdmv_pgs_subtitle"}])
# file: (video codec, bit depth, audio, subtitles) — the problems each has
FILES = {
    "/m/Hi10 (2020)/Hi10.mkv": ("h264", 10, DTS, "[]"),     # video, audio
    "/m/Dts (2020)/Dts.mkv": ("h264", 8, DTS, "[]"),        # audio
    "/m/Old (1990)/Old.avi": ("mpeg4", 8, AAC, "[]"),       # container
    "/m/Subs (2020)/Subs.mkv": ("hevc", 8, AAC, PGS),       # subs
    "/m/Fine (2020)/Fine.mkv": ("hevc", 8, AAC, "[]"),      # none
}


def _short(path):
    return path.rsplit("/", 1)[1]


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, (codec, depth, audio, subs) in FILES.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, video_bit_depth, "
                "audio_tracks_json, subtitle_tracks_json, scan_timestamp) VALUES (?, 1, 0, ?, ?, ?, ?, 't')",
                (path, codec, depth, audio, subs))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('default_encoder', 'libx265')")
        await db.commit()
    return test_db


async def _queue(db_path, **request):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM jobs")
        await db.commit()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(FILES), **request))
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type, direct_play FROM jobs") as cur:
            return {_short(p): (t, dp) for p, t, dp in await cur.fetchall()}


@pytest.mark.asyncio
async def test_every_fix_ticked(library):
    assert await _queue(library, direct_play=True) == {
        "Hi10.mkv": ("convert", "audio,video"), "Dts.mkv": ("audio", "audio"), "Old.avi": ("audio", "container"),
        "Subs.mkv": ("audio", "subs"), "Fine.mkv": ("audio", None)}


@pytest.mark.asyncio
async def test_only_the_fixes_ticked(library):
    """Remux and Hi10P unticked: the AVI is queued as it would be without
    the fixes, and the Hi10P file isn't converted."""
    assert await _queue(library, direct_play=True, direct_play_fixes=["subs", "audio"]) == {
        "Hi10.mkv": ("audio", "audio"), "Dts.mkv": ("audio", "audio"), "Old.avi": ("audio", None),
        "Subs.mkv": ("audio", "subs"), "Fine.mkv": ("audio", None)}


@pytest.mark.asyncio
async def test_cleanup_only_never_converts_hi10p(library):
    jobs = await _queue(library, direct_play=True, cleanup_only=True)
    assert jobs["Hi10.mkv"] == ("audio", "audio")


@pytest.mark.asyncio
async def test_unticked_it_overrides_the_setting_for_conversions(library):
    """"Fix for direct play when converting" ticks it in the dialog; unticked
    there, those conversions aren't fixed. Queued without the dialog (the
    watcher, policies), the setting applies as before."""
    async with aiosqlite.connect(library) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('direct_play_on_convert', 'true')")
        await db.execute("UPDATE scan_results SET needs_conversion = 1 WHERE file_path LIKE '%Dts.mkv'")
        await db.commit()
    assert (await _queue(library, direct_play=False))["Dts.mkv"] == ("convert", None)
    assert (await _queue(library))["Dts.mkv"] == ("convert", 1)


def test_what_each_job_fixes():
    assert job_fixes({"direct_play": 1}) == {"video", "subs", "audio", "container"}  # before they could be chosen
    assert job_fixes({"direct_play": "audio,container"}) == {"audio", "container"}
    assert job_fixes({"direct_play": None}) == job_fixes({}) == set()
    # A remux makes the fixes but Hi10P's: that's the conversion's.
    assert remux_fixes({"direct_play": "container"}) and remux_fixes({"direct_play": 1})
    assert not remux_fixes({"direct_play": "video"}) and not remux_fixes({"direct_play": None})
    # SRT copies and the compatibility track only when chosen
    assert settings_overrides({"direct_play": "audio"}) == {"compat_for_direct_play": True}
    assert settings_overrides({"direct_play": "subs,container"}) == {"image_subs_to_srt": True}
    assert settings_overrides({"direct_play": "video"}) == {}


@pytest.mark.asyncio
async def test_the_estimate_counts_each_fix(library):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    off = await estimate_jobs(EstimateRequest(file_paths=list(FILES)))
    # Counted ticked or not: the dialog shows them beside each fix.
    assert off["direct_play"] == {"video": 1, "subs": 1, "audio": 2, "container": 1}
    assert off["direct_play_files"] == 4
    assert off["by_type"]["convert"] == 0
    on = await estimate_jobs(EstimateRequest(file_paths=list(FILES), direct_play=True))
    assert (on["by_type"]["convert"], on["total_files"]) == (1, 5)  # Hi10P converted
    unticked = await estimate_jobs(EstimateRequest(file_paths=list(FILES), direct_play=True,
                                                   direct_play_fixes=["container"]))
    assert unticked["by_type"]["convert"] == 0


@pytest.mark.asyncio
async def test_an_old_container_converted_anyway_isnt_called_a_remux(library):
    """Converting makes an MKV too: the dialog says which old-container files
    are remuxed and which become MKVs by converting."""
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    request = EstimateRequest(file_paths=list(FILES), direct_play=True)
    assert (await estimate_jobs(request))["direct_play_container"] == {"remux": 1, "convert": 0}
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET needs_conversion = 1 WHERE file_path LIKE '%Old.avi'")
        await db.commit()
    assert (await estimate_jobs(request))["direct_play_container"] == {"remux": 0, "convert": 1}
