"""Per-rule actions (v0.10.0): a rule's audio languages and subtitle policy
decide its files' track removals when queued; its originals policy and VMAF
minimum travel with the job (to the local worker and remote ones); a
minimum saving leaves small wins alone."""
import json
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio
from pydantic import ValidationError

import backend.scanner as scanner
from backend.rule_actions import settings_overrides, tracks_for_rule

GLOBAL = scanner.TrackRules(audio_keep=frozenset({"eng"}), keep_native_audio=False, dedup=False,
                            sub_keep=frozenset({"eng"}), sub_keep_unknown=True)


def _row(manual_fre=False):
    audio = [{"stream_index": 1, "language": "eng", "codec": "ac3", "channels": 6},
             {"stream_index": 2, "language": "fre", "codec": "ac3", "channels": 6, **({"manual": True, "keep": True} if manual_fre else {})},
             {"stream_index": 3, "language": "ger", "codec": "ac3", "channels": 6}]
    subs = [{"stream_index": 4, "language": "eng", "codec": "subrip"},
            {"stream_index": 5, "language": "fre", "codec": "subrip", "forced": True},
            {"stream_index": 6, "language": "und", "codec": "subrip"}]
    return {"audio_tracks_json": json.dumps(audio), "subtitle_tracks_json": json.dumps(subs),
            "native_language": "und", "duration": 60}


def _keeps(row, key):
    return {t["stream_index"]: t["keep"] for t in json.loads(row[key])}


@pytest.fixture
def global_rules(monkeypatch):
    monkeypatch.setattr(scanner, "current_track_rules", lambda: GLOBAL)


def test_a_rules_languages(global_rules):
    row = _row()
    assert tracks_for_rule(row, None) is row and tracks_for_rule(row, {"name": "x"}) is row
    german = tracks_for_rule(row, {"audio_keep_languages": "ger"})
    assert _keeps(german, "audio_tracks_json") == {1: False, 2: False, 3: True}
    assert _keeps(tracks_for_rule(_row(manual_fre=True), {"audio_keep_languages": "de"}), "audio_tracks_json") == {
        1: False, 2: True, 3: True}  # "de" is German too; a choice made by hand stays
    nothing = tracks_for_rule(row, {"audio_keep_languages": "jpn"})
    assert sum(_keeps(nothing, "audio_tracks_json").values()) == 1  # a file keeps an audio track
    removed_by_hand = json.loads(row["audio_tracks_json"])
    for t in removed_by_hand:
        t.update(manual=True, keep=False)  # every track unticked by hand: one is kept regardless
    hand = tracks_for_rule({**row, "audio_tracks_json": json.dumps(removed_by_hand)}, {"audio_keep_languages": "jpn"})
    assert sum(_keeps(hand, "audio_tracks_json").values()) == 1


def test_a_rules_subtitle_policy(global_rules):
    row = _row()
    assert _keeps(tracks_for_rule(row, {"sub_policy": "keep_all"}), "subtitle_tracks_json") == {4: True, 5: True, 6: True}
    assert _keeps(tracks_for_rule(row, {"sub_policy": "remove_all"}), "subtitle_tracks_json") == {4: False, 5: False, 6: False}
    assert _keeps(tracks_for_rule(row, {"sub_policy": "languages", "sub_keep_languages": "fre"}),
                  "subtitle_tracks_json") == {4: False, 5: True, 6: False}


def test_the_settings_a_job_carries():
    assert settings_overrides({}) == {}
    assert settings_overrides({"originals_policy": "keep", "originals_days": 30}) == {
        "backup_original_days": 30, "trash_original_after_conversion": False}
    assert settings_overrides({"originals_policy": "trash", "vmaf_min_score": 0}) == {
        "backup_original_days": 0, "trash_original_after_conversion": True, "vmaf_min_score": 0.0}
    assert settings_overrides({"originals_policy": "delete"})["trash_original_after_conversion"] is False


@pytest_asyncio.fixture
async def env(test_db, monkeypatch, global_rules):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    row = tracks_for_rule(_row(), {"audio_keep_languages": "eng", "sub_policy": "languages", "sub_keep_languages": "eng"})
    # as a scan stores them: the global languages (eng) decided
    async with aiosqlite.connect(test_db) as db:
        for path, savings, tracks in (("/m/Anime/Show/e1.mkv", 500, True), ("/m/Small/a.mkv", 100, False),
                                      ("/m/Small/b.mkv", 100, True), ("/m/Other/c.mkv", 500, True)):
            audio = row["audio_tracks_json"] if tracks else json.dumps([{"stream_index": 1, "language": "eng", "keep": True}])
            subs = row["subtitle_tracks_json"] if tracks else "[]"
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, audio_tracks_json, "
                "subtitle_tracks_json, native_language, scan_timestamp, video_conv_savings_bytes) "
                "VALUES (?, 1000, 60, 'h264', 1, ?, ?, 'und', 'x', ?)", (path, audio, subs, savings))
        await db.commit()
    from backend.routes import rules as route
    await route.create_rule(route.RuleCreate(
        name="Anime", match_conditions=[route.MatchCondition(type="directory", value="/m/Anime")],
        audio_keep_languages="ger", sub_policy="languages", sub_keep_languages="fre",
        originals_policy="keep", originals_days=30, vmaf_min_score=92))
    await route.create_rule(route.RuleCreate(
        name="Small wins", match_conditions=[route.MatchCondition(type="directory", value="/m/Small")], min_savings_pct=25))
    return test_db


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY file_path") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_queueing_follows_the_rule(env):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan, refresh_pending_jobs
    paths = ["/m/Anime/Show/e1.mkv", "/m/Small/a.mkv", "/m/Small/b.mkv", "/m/Other/c.mkv"]
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=paths))
    jobs = await _jobs(env)
    anime = jobs["/m/Anime/Show/e1.mkv"]
    assert (json.loads(anime["audio_tracks_to_remove"]), json.loads(anime["subtitle_tracks_to_remove"])) == ([1, 2], [4, 6])
    assert (anime["originals_policy"], anime["originals_days"], anime["vmaf_min_score"]) == ("keep", 30, 92.0)
    other = jobs["/m/Other/c.mkv"]  # no rule: the global languages, the global settings
    assert (json.loads(other["audio_tracks_to_remove"]), other["originals_policy"]) == ([2, 3], None)
    assert "/m/Small/a.mkv" not in jobs  # 10 % < 25 %, nothing else to do
    assert jobs["/m/Small/b.mkv"]["job_type"] == "audio"  # the cleanup only
    async with aiosqlite.connect(env) as db:  # pending jobs keep following the rule
        await db.execute("UPDATE jobs SET audio_tracks_to_remove = '[]' WHERE file_path = '/m/Anime/Show/e1.mkv'")
        await db.commit()
    await refresh_pending_jobs()
    assert json.loads((await _jobs(env))["/m/Anime/Show/e1.mkv"]["audio_tracks_to_remove"]) == [1, 2]


@pytest.mark.asyncio
async def test_the_estimate_follows_it(env):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    est = await estimate_jobs(EstimateRequest(file_paths=["/m/Anime/Show/e1.mkv", "/m/Small/a.mkv", "/m/Small/b.mkv"]))
    # Anime removes eng + fre (it keeps German); b's cleanup removes fre + ger (the global eng)
    assert est["removals"]["audio"] == {"eng": 1, "fre": 2, "ger": 1} and est["by_type"]["audio"] == 1
    assert est["skipped_by_rules"] == 2


@pytest.mark.asyncio
async def test_the_job_settings_reach_convert_file(env, tmp_path, monkeypatch):
    import backend.converter as converter
    from backend.queue import JobQueue, QueueWorker
    src = tmp_path / "film.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=24:d=1",
                    "-c:v", "mpeg4", str(src)], check=True)
    seen = {}

    async def convert(**kwargs):
        seen.update(kwargs)
        return {"success": False, "error": "stop here", "output_path": None, "space_saved": 0}
    monkeypatch.setattr(converter, "convert_file", convert)
    job_id = await JobQueue(env).add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(env) as db:
        await db.execute("UPDATE jobs SET originals_policy = 'trash', vmaf_min_score = 0 WHERE id = ?", (job_id,))
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())
    await QueueWorker(env)._process_job(job)
    settings = seen["pre_settings"]
    assert (settings["trash_original_after_conversion"], settings["backup_original_days"], settings["vmaf_min_score"]) == (
        True, 0, 0.0)


@pytest.mark.asyncio
async def test_remote_workers_get_them(env, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    job_id = await JobQueue(env).add_job("/m/Film/film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        await db.execute("UPDATE jobs SET originals_policy = 'keep', originals_days = 30, vmaf_min_score = 0 WHERE id = ?", (job_id,))
        for k, v in (("backup_original_days", "7"), ("vmaf_min_score", "88")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert (job["backup_original_days"], job["trash_original_after_conversion"], job["vmaf_min_score"]) == (30, False, 0.0)


def test_the_rule_fields_are_checked():
    from backend.routes import rules as route
    cond = [route.MatchCondition(type="directory", value="/m")]
    for bad in ({"sub_policy": "some"}, {"originals_policy": "archive"}, {"vmaf_min_score": 120},
                {"min_savings_pct": -1}, {"originals_days": 0}):
        with pytest.raises(ValidationError):
            route.RuleCreate(name="x", match_conditions=cond, **bad)
