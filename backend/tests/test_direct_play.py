"""Direct play (v0.10.0): the Scanner finds what makes media servers
transcode (Hi10P, image-only subtitles, no widely decoded audio, old
containers) and "Fix for direct play" queues the fix — a conversion for
Hi10P, else a remux to MKV — with a compatibility track where the audio
needs one and SRT copies of image subtitles."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from backend.converter import COMPAT_TRACK_TITLE, compat_track_plan

DP = {"compat_for_direct_play": True, "audio_compat_codec": "aac", "audio_compat_bitrate": 160}


def _t(index, lang, codec, channels=6):
    return {"stream_index": index, "language": lang, "codec": codec, "channels": channels}


def test_when_the_audio_needs_a_compatibility_track():
    assert compat_track_plan([_t(1, "eng", "dts")], DP) == {
        "source": 1, "language": "eng", "out_index": 1, "codec": "aac", "bitrate": 160, "loudnorm": False}
    assert compat_track_plan([_t(1, "eng", "truehd", 8), _t(2, "eng", "dts", 2)], DP)["source"] == 1  # stereo DTS too
    assert compat_track_plan([_t(1, "eng", "dts"), _t(2, "eng", "ac3")], DP) is None  # one plays everywhere
    assert compat_track_plan([_t(1, "eng", "dts"), _t(2, "fre", "aac", 2)], DP)["source"] == 1  # another language's
    assert compat_track_plan([_t(1, "eng", "dts")], {}) is None  # not fixing for direct play


def test_the_remux_command():
    from backend.audio import build_remux_cmd
    plan = compat_track_plan([_t(1, "eng", "dts"), _t(3, "fre", "dts")], DP)
    cmd = build_remux_cmd("/m/in.avi", "/m/out.mkv", [1, 3], compat_track=plan)
    text = " ".join(cmd)
    assert cmd.index("0:1") < cmd.index("0:3") < len(cmd) - cmd[::-1].index("0:1") - 1  # mapped again, last
    assert "-c copy" in text and text.index("-c copy") < text.index("-c:a:2 aac -b:a:2 160k -ac:a:2 2")
    assert f"-metadata:s:a:2 title={COMPAT_TRACK_TITLE} -metadata:s:a:2 language=eng -disposition:a:2 0" in text
    assert cmd[-1] == "/m/out.mkv" and "aac" not in " ".join(build_remux_cmd("/m/in.avi", "/m/out.mkv", [1, 3]))


@pytest.mark.asyncio
async def test_which_tracks_count(test_db):
    """Kept tracks only; an image subtitle is fine beside a text one in its
    language; the main audio is the first kept track."""
    from backend.database import connect_db
    from backend.direct_play import problems_for

    def tracks(*specs):
        return json.dumps([{"stream_index": i, "language": lang, "codec": codec, "keep": keep}
                           for i, (lang, codec, keep) in enumerate(specs, start=1)])
    rows = {
        "/m/a.mkv": ([("eng", "aac", True)], [("eng", "hdmv_pgs_subtitle", True), ("eng", "subrip", True)]),
        "/m/b.mkv": ([("eng", "aac", True)], [("eng", "hdmv_pgs_subtitle", True), ("fre", "subrip", True)]),
        "/m/c.mkv": ([("eng", "aac", True)], [("eng", "hdmv_pgs_subtitle", False)]),
        "/m/d.mkv": ([("eng", "aac", False), ("fre", "dts", True), ("eng", "ac3", True)], []),  # main: French
        "/m/e.mkv": ([("eng", "dts", True), ("eng", "ac3", False)], []),
        "/m/f.MPG": ([("eng", "mp2", True), ("eng", "ac3", True)], []),
        "/m/g.mkv": ([("eng", "dts", True), ("fre", "aac", True)], []),  # French AAC doesn't help
    }
    async with aiosqlite.connect(test_db) as db:
        for path, (audio, subs) in rows.items():
            await db.execute("INSERT INTO scan_results (file_path, file_size, audio_tracks_json, subtitle_tracks_json, "
                             "scan_timestamp) VALUES (?, 1, ?, ?, 'x')", (path, tracks(*audio), tracks(*subs)))
        await db.commit()
    db = await connect_db()
    try:
        assert await problems_for(db, list(rows)) == {
            "/m/b.mkv": ["subs"], "/m/d.mkv": ["audio"], "/m/e.mkv": ["audio"], "/m/f.MPG": ["container"],
            "/m/g.mkv": ["audio"]}
    finally:
        await db.close()


def test_a_fix_job_reads_subtitles_and_adds_the_track():
    from backend.rule_actions import settings_overrides
    assert settings_overrides({"direct_play": 1}) == {"image_subs_to_srt": True, "compat_for_direct_play": True}
    assert settings_overrides({"direct_play": None}) == {}


def _enc() -> str:
    if not shutil.which("ffmpeg"):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


needs_media = pytest.mark.skipif(not all(e in _enc() for e in ("libx264", "libx265", " dca ", " aac ", "libmp3lame")),
                                 reason="needs ffmpeg encoders")


def _make(path, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-f", "lavfi", "-i", "sine=d=2:r=48000", *args, str(path)], check=True)


@pytest_asyncio.fixture
async def library(test_db, tmp_path, monkeypatch):
    """Four films, scanned: Hi10P with DTS, DTS-only, an AVI with MP3, and
    one that plays directly."""
    import sys
    import backend.config
    import backend.scanner as scanner
    from backend import bazarr
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    from backend.routes.scan import _write_batch
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda *a, **kw: None)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    root = tmp_path / "media"
    films = {"hi10": root / "Hi10 (2020)" / "Hi10 (2020).mkv", "dts": root / "Dts (2020)" / "Dts (2020).mkv",
             "avi": root / "Old (1990)" / "Old (1990).avi", "fine": root / "Fine (2020)" / "Fine (2020).mkv"}
    for f in films.values():
        f.parent.mkdir(parents=True)
    _make(films["hi10"], "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p10le", "-c:a", "dca", "-strict", "-2", "-ac", "6",
          "-metadata:s:a:0", "language=eng")
    _make(films["dts"], "-c:v", "libx264", "-c:a", "dca", "-strict", "-2", "-ac", "6", "-metadata:s:a:0", "language=eng")
    _make(films["avi"], "-c:v", "mpeg4", "-c:a", "libmp3lame")
    _make(films["fine"], "-c:v", "libx264", "-c:a", "aac", "-metadata:s:a:0", "language=eng")
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("backup_original_days", "0"), ("trash_original_after_conversion", "false"),
                     ("vmaf_analysis_enabled", "false"), ("libx265_preset", "ultrafast"), ("default_encoder", "libx265"),
                     ("always_keep_languages", '["eng", "und"]')):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    await _write_batch(test_db, await scanner.scan_directory(str(root)), "2026-10-10T00:00:00+00:00")
    return SimpleNamespace(db=test_db, films={k: str(v) for k, v in films.items()}, worker=worker)


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY id") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


def _streams(path):
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,channels:stream_tags=title",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout)["streams"]


@needs_media
@pytest.mark.asyncio
async def test_the_scanner_finds_them_and_the_fix_queues_jobs(library):
    from backend.direct_play import problems_for
    from backend.routes import rules as rules_route
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    from backend.database import connect_db
    f = library.films
    db = await connect_db()
    try:
        assert await problems_for(db, list(f.values())) == {
            f["hi10"]: ["video", "audio"], f["dts"]: ["audio"], f["avi"]: ["container"]}
    finally:
        await db.close()
    # A skip rule and the conversion filters govern conversion, not fixes;
    # Hi10P is converted even when H.264 isn't (needs_conversion = 0).
    async with aiosqlite.connect(library.db) as conn:
        await conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('min_file_size_mb', '1000')")
        await conn.execute("UPDATE scan_results SET needs_conversion = 0 WHERE file_path = ?", (f["hi10"],))
        await conn.commit()
    await rules_route.create_rule(rules_route.RuleCreate(
        name="Skip", action="skip", match_conditions=[rules_route.MatchCondition(type="directory", value=str(
            library.films["avi"]).rsplit("/", 2)[0])]))
    result = await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(f.values()), direct_play=True))
    assert result["added"] == 3
    jobs = await _jobs(library.db)
    assert {p: (j["job_type"], j["direct_play"]) for p, j in jobs.items()} == {
        f["hi10"]: ("convert", 1), f["dts"]: ("audio", 1), f["avi"]: ("audio", 1)}


@needs_media
@pytest.mark.asyncio
async def test_the_fixes(library):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    f = library.films
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(f.values()), direct_play=True))
    queued = await _jobs(library.db)
    ids = {queued[f[k]]["id"]: k for k in ("hi10", "dts", "avi")}
    for job in queued.values():
        await library.worker._process_job(job)
    done = {ids[j["id"]]: j for j in (await _jobs(library.db)).values()}
    assert {k: j["status"] for k, j in done.items()} == {"hi10": "completed", "dts": "completed", "avi": "completed"}, [
        j["error_log"] for j in done.values()]
    # DTS only: remuxed with a stereo AAC track beside it
    dts = _streams(done["dts"]["file_path"])
    assert [(s["codec_name"], s.get("channels")) for s in dts if s["codec_type"] == "audio"] == [("dts", 6), ("aac", 2)]
    assert [s for s in dts if s["codec_type"] == "audio"][1]["tags"]["title"] == COMPAT_TRACK_TITLE
    # The AVI: an MKV now, its MP3 copied (it plays everywhere)
    avi = done["avi"]["file_path"]
    assert avi.endswith("Old (1990).mkv") and [s["codec_name"] for s in _streams(avi)] == ["mpeg4", "mp3"]
    # Hi10P: converted, and the conversion added the track too
    hi10 = _streams(done["hi10"]["file_path"])
    assert [s["codec_name"] for s in hi10] == ["hevc", "dts", "aac"]


@pytest.mark.asyncio
async def test_remote_workers_fix_them_too(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    job_id = await JobQueue(test_db).add_job("/m/Film/film.mkv", "audio")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        await db.execute("UPDATE jobs SET direct_play = 1 WHERE id = ?", (job_id,))
        await db.commit()
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert (job["direct_play"], job["image_subs_to_srt"], job["compat_for_direct_play"]) == (1, True, True)


@pytest.mark.asyncio
async def test_conversions_fix_for_direct_play_when_the_setting_is_on(test_db, monkeypatch):
    """"Fix for direct play when converting": every conversion job carries the
    fixes, however it's queued; remuxes, and conversions with it off, don't."""
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    from backend.queue import JobQueue
    from backend.rule_actions import settings_overrides
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    queue = JobQueue(test_db)
    await queue.add_job("/m/a.mkv", "convert")
    await queue.add_jobs_bulk([{"file_path": "/m/b.mkv", "job_type": "combined"}])
    await settings_route.update_encoding_settings(SettingsUpdate(direct_play_on_convert=True))
    assert (await settings_route.get_encoding_settings())["direct_play_on_convert"] is True
    await queue.add_job("/m/c.mkv", "convert")
    await queue.add_job("/m/d.mkv", "audio")
    await queue.add_jobs_bulk([{"file_path": "/m/e.mkv", "job_type": "combined"},
                               {"file_path": "/m/f.mkv", "job_type": "audio"}])
    jobs = await _jobs(test_db)
    assert {p[3:]: j["direct_play"] for p, j in jobs.items()} == {
        "a.mkv": None, "b.mkv": None, "c.mkv": 1, "d.mkv": None, "e.mkv": 1, "f.mkv": None}
    assert settings_overrides(jobs["/m/c.mkv"]) == {"image_subs_to_srt": True, "compat_for_direct_play": True}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_an_encode_discarded_as_larger_still_fixes_for_direct_play(test_db, tmp_path, monkeypatch):
    """The original stays when the encode is larger; the remux's fixes are
    still made to it (here DTS-only audio: a compatibility track)."""
    import backend.audio as audio
    import backend.converter as converter
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Film (2020).mkv"
    _mkv(src, ["eng"])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, audio_tracks_json) "
                         "VALUES (?, 1, '2026-10-14', ?)",
                         (str(src), json.dumps([{"stream_index": 1, "language": "eng", "codec": "dts"}])))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('direct_play_on_convert', 'true')")
        await db.commit()

    async def larger(**kwargs):
        return {"success": True, "output_path": str(src), "space_saved": 0, "error": None,
                "skipped_larger": True, "had_track_removal": False}
    monkeypatch.setattr(converter, "convert_file", larger)
    remuxed, remux = [], audio.remux_audio

    async def recorded(**kwargs):
        remuxed.append(kwargs)
        return await remux(**kwargs)
    monkeypatch.setattr(audio, "remux_audio", recorded)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    await QueueWorker(test_db)._process_job(next(j for j in await queue.get_all_jobs() if j["id"] == job_id))
    assert [(k["input_path"], k["compat_settings"]["compat_for_direct_play"]) for k in remuxed] == [(str(src), True)]
