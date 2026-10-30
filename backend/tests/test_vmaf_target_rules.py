"""A rule's own VMAF target (v0.10.0): it can search for its own score, or
switch the search off and encode at the rule's quality. The choice is stored
on the job when it's queued and travels with it: to convert_file, and to
remote workers as the settings they already understand. The VMAF minimum
(the reject threshold) is a floor under any target."""
import shutil

import aiosqlite
import pytest
import pytest_asyncio
from pydantic import ValidationError

from backend import vmaf_target
from backend.api_errors import ApiError
from backend.rule_actions import settings_overrides
from backend.tests.test_message_codes import _load, _lookup, _render_en

REALITY, FILM, PLAIN = "/m/Reality/Show/e1.mkv", "/m/Films/Film (2020)/film.mkv", "/m/Plain/c.mkv"


async def _settings(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for key, value in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()


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
        for path in (REALITY, FILM, PLAIN):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, "
                "audio_tracks_json, subtitle_tracks_json, native_language, scan_timestamp) "
                "VALUES (?, 1000, 3600, 'h264', 1, '[]', '[]', 'und', 'x')", (path,))
        await db.commit()
    await _settings(test_db, default_encoder="libx265")
    return test_db


async def _rule(name, folder, **fields):
    from backend.routes import rules as route
    return (await route.create_rule(route.RuleCreate(
        name=name, match_conditions=[route.MatchCondition(type="directory", value=folder)], **fields)))["id"]


async def _queued(db_path, *paths):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(paths)))
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY file_path") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


def test_the_settings_a_target_carries():
    assert settings_overrides({"vmaf_target": 0}) == {"vmaf_target_enabled": False}
    assert settings_overrides({"vmaf_target": 0.0}) == {"vmaf_target_enabled": False}
    assert settings_overrides({"vmaf_target": 95}) == {"vmaf_target_enabled": True, "vmaf_target_score": 95.0}
    assert settings_overrides({"vmaf_target": None}) == settings_overrides({}) == {}


@pytest.mark.asyncio
async def test_a_rule_off_never_searches(library):
    from backend.converter import get_live_encoding_settings
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    await _rule("Reality", "/m/Reality", vmaf_target=0, nvenc_cq=38)
    job = (await _queued(library, REALITY))[REALITY]
    assert job["vmaf_target"] == 0 and job["nvenc_cq"] == 38  # Off is stored, and the rule's quality is the job's
    overrides = settings_overrides(job)
    assert overrides["vmaf_target_enabled"] is False
    live = {**await get_live_encoding_settings(), **overrides}  # as the queue hands them to convert_file
    assert live["vmaf_target_enabled"] is False


@pytest.mark.asyncio
async def test_a_rule_target_without_the_global_setting(library):
    await _settings(library, vmaf_target_enabled="false", vmaf_min_score=88)
    await _rule("Films", "/m/Films", vmaf_target=95)
    job = (await _queued(library, FILM))[FILM]
    assert job["vmaf_target"] == 95.0
    overrides = settings_overrides(job)
    assert (overrides["vmaf_target_enabled"], overrides["vmaf_target_score"]) == (True, 95.0)


@pytest.mark.asyncio
async def test_no_rule_follows_settings(library):
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    await _rule("Reality", "/m/Reality", vmaf_target=0)
    jobs = await _queued(library, PLAIN, REALITY)
    assert jobs[PLAIN]["vmaf_target"] is None and jobs[REALITY]["vmaf_target"] == 0
    assert not [k for k in settings_overrides(jobs[PLAIN]) if k.startswith("vmaf_target")]
    await _rule("No target", "/m/Films", nvenc_cq=30)  # a rule that says nothing about it
    job = (await _queued(library, FILM))[FILM]
    assert job["vmaf_target"] is None and not [k for k in settings_overrides(job) if k.startswith("vmaf_target")]


async def _refused(call, code):
    with pytest.raises(ApiError) as caught:
        await call
    error = caught.value
    assert error.code == code and error.status_code == 400
    assert _render_en("serverApi", error.code, error.params) == error.detail  # the UI's text is the English message
    assert isinstance(_lookup(_load("es", "serverApi"), error.code), str)
    return error


@pytest.mark.asyncio
async def test_the_target_is_kept_at_or_above_the_minimum(test_db):
    from backend.routes import rules as route

    def make(**fields):
        return route.create_rule(route.RuleCreate(
            name="x", match_conditions=[route.MatchCondition(type="directory", value="/m")], **fields))
    error = await _refused(make(vmaf_target=85, vmaf_min_score=88), "rules.vmafTargetBelowMinimum")
    assert error.params == {"target": "85", "minimum": "88"}
    await _refused(make(vmaf_target=79), "rules.vmafTargetRange")
    await _refused(make(vmaf_target=50), "rules.vmafTargetRange")
    for target in (88, 99, 0, None):  # equal to the minimum is fine; Off and Settings' never need checking
        assert (await make(vmaf_target=target, vmaf_min_score=88))["id"]
    assert (await make(vmaf_target=0, vmaf_min_score=100))["id"]
    with pytest.raises(ValidationError):  # past 99 isn't a score at all
        route.RuleCreate(name="x", match_conditions=[], vmaf_target=100)


@pytest.mark.asyncio
async def test_settings_minimum_applies_when_the_rule_has_none(test_db):
    from backend.routes import rules as route

    def make(**fields):
        return route.create_rule(route.RuleCreate(
            name="x", match_conditions=[route.MatchCondition(type="directory", value="/m")], **fields))
    await _settings(test_db, vmaf_analysis_enabled="true", vmaf_min_score=90)
    error = await _refused(make(vmaf_target=85), "rules.vmafTargetBelowMinimum")
    assert error.params == {"target": "85", "minimum": "90"}
    assert (await make(vmaf_target=85, vmaf_min_score=0))["id"]  # the rule's own minimum (never reject) wins
    assert (await make(vmaf_target=90))["id"]
    await _settings(test_db, vmaf_analysis_enabled="false")  # no analysis: Settings' minimum isn't in force
    assert (await make(vmaf_target=85))["id"]
    await _refused(make(vmaf_target=85, vmaf_min_score=88), "rules.vmafTargetBelowMinimum")  # its own still counts


@pytest.mark.asyncio
async def test_an_edit_is_checked_against_what_is_stored(test_db):
    from backend.routes import rules as route
    await _settings(test_db, vmaf_analysis_enabled="true", vmaf_min_score=0)
    rule_id = (await route.create_rule(route.RuleCreate(
        name="x", match_conditions=[route.MatchCondition(type="directory", value="/m")],
        vmaf_target=90, vmaf_min_score=88)))["id"]

    async def stored():
        async with aiosqlite.connect(test_db) as db:
            async with db.execute("SELECT vmaf_target, vmaf_min_score, enabled FROM encoding_rules WHERE id = ?",
                                  (rule_id,)) as cur:
                return await cur.fetchone()
    await _refused(route.update_rule(rule_id, route.RuleUpdate(vmaf_target=87)), "rules.vmafTargetBelowMinimum")
    await _refused(route.update_rule(rule_id, route.RuleUpdate(vmaf_min_score=95)), "rules.vmafTargetBelowMinimum")
    await _refused(route.update_rule(rule_id, route.RuleUpdate(vmaf_target=60)), "rules.vmafTargetRange")
    assert await stored() == (90.0, 88.0, 1)  # nothing was written
    await route.update_rule(rule_id, route.RuleUpdate(vmaf_target=92, vmaf_min_score=91))
    assert await stored() == (92.0, 91.0, 1)
    await route.update_rule(rule_id, route.RuleUpdate(enabled=False))  # unrelated edits aren't held up by it
    assert await stored() == (92.0, 91.0, 0)
    await route.update_rule(rule_id, route.RuleUpdate(vmaf_target=0))
    await route.update_rule(rule_id, route.RuleUpdate(vmaf_min_score=100))
    assert await stored() == (0.0, 100.0, 0)  # Off is always valid
    await route.update_rule(rule_id, route.RuleUpdate(vmaf_target=None))  # back to Settings'
    assert (await stored())[0] is None


@pytest.mark.asyncio
async def test_a_rules_target_is_shared_with_the_rule(test_db):
    from backend.routes import rules as route
    from backend.rule_sharing import export_rules, import_rules, parse
    for name, target in (("Off", 0), ("Film", 95), ("Follows", None)):
        await route.create_rule(route.RuleCreate(
            name=name, match_conditions=[route.MatchCondition(type="directory", value="/m")], vmaf_target=target))
    text = await export_rules()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM encoding_rules")
        await db.commit()
    # another install, where Settings' minimum is higher than the file's Film target
    await _settings(test_db, vmaf_analysis_enabled="true", vmaf_min_score=96)
    result = await import_rules(parse(text))
    assert (result["added"], [(e["name"], e["code"]) for e in result["errors"]]) == (
        2, [("Film", "rules.vmafTargetBelowMinimum")])  # reported; the rest still imported
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT name, vmaf_target FROM encoding_rules ORDER BY id") as cur:
            assert await cur.fetchall() == [("Off", 0.0), ("Follows", None)]


@pytest.mark.asyncio
async def test_an_older_worker_gets_off_as_disabled(test_db, monkeypatch):
    from types import SimpleNamespace
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        await db.commit()

    async def assigned(global_enabled, job_target):
        await _settings(test_db, vmaf_target_enabled=global_enabled, vmaf_target_score=93)
        async with aiosqlite.connect(test_db) as db:
            await db.execute("DELETE FROM jobs")
            await db.commit()
        job_id = await JobQueue(test_db).add_job("/m/Film/film.mkv", "convert", encoder="libx265")
        async with aiosqlite.connect(test_db) as db:
            await db.execute("UPDATE jobs SET vmaf_target = ? WHERE id = ?", (job_target, job_id))
            await db.commit()
        job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
        return job["vmaf_target_enabled"], job["vmaf_target_score"]
    assert await assigned("true", 0) == (False, 93.0)  # Off, whatever Settings say
    assert await assigned("false", 0) == (False, 93.0)
    assert await assigned("false", 95) == (True, 95.0)  # a rule's score, with the setting off
    assert await assigned("true", 95) == (True, 95.0)
    assert await assigned("true", None) == (True, 93.0)  # no rule: Settings'
    assert await assigned("false", None) == (False, 93.0)


def test_the_minimum_in_force():
    force = vmaf_target.active_minimum
    assert force({"vmaf_analysis_enabled": True, "vmaf_min_score": 90.0}) == 90.0
    assert force({"vmaf_min_score": 88}) == 88.0  # analysis is on unless said off
    assert force({"vmaf_analysis_enabled": "true", "vmaf_min_score": "92"}) == 92.0
    assert force({"vmaf_analysis_enabled": False, "vmaf_min_score": 90}) == 0.0
    assert force({"vmaf_analysis_enabled": "false", "vmaf_min_score": 90}) == 0.0
    assert force({"vmaf_analysis_enabled": True, "vmaf_min_score": 0}) == 0.0
    assert force({"vmaf_analysis_enabled": True}) == 0.0 and force({}) == 0.0
    assert force({"vmaf_analysis_enabled": True, "vmaf_min_score": "oops"}) == 0.0


class _Searched(Exception):
    pass


async def _searched_target(test_db, tmp_path, monkeypatch, job, **settings):
    """The target convert_file hands the search, for a job (its rule's
    overrides) over Settings."""
    from backend.converter import convert_file, get_live_encoding_settings
    from backend.tests.test_same_name_conversion import _mkv
    folder = tmp_path / "Film (2020)"
    folder.mkdir(exist_ok=True)
    clip = folder / "Film (2020).mkv"
    _mkv(clip, ["eng"])
    await _settings(test_db, **settings)
    seen = []

    async def search(path, encoder, duration, target, **kwargs):
        seen.append(target)
        raise _Searched  # no need to encode anything
    monkeypatch.setattr(vmaf_target, "find_quality", search)
    live = {**await get_live_encoding_settings(), **settings_overrides(job)}
    with pytest.raises(_Searched):
        await convert_file(str(clip), "libx265", 2.0, pre_settings=live)
    return seen


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_the_minimum_raises_the_target(test_db, tmp_path, monkeypatch):
    on = {"vmaf_analysis_enabled": "true", "vmaf_min_score": 90}
    assert await _searched_target(test_db, tmp_path, monkeypatch, {"vmaf_target": 85}, **on) == [90.0]  # a rule's 85
    assert await _searched_target(test_db, tmp_path, monkeypatch, {}, vmaf_target_enabled="true",
                                  vmaf_target_score=85, **on) == [90.0]  # Settings' own target too
    assert await _searched_target(test_db, tmp_path, monkeypatch, {"vmaf_target": 95}, **on) == [95.0]  # above it: kept
    # the rule's own minimum replaces Settings' (0: never reject, so no floor)
    assert await _searched_target(test_db, tmp_path, monkeypatch, {"vmaf_target": 85, "vmaf_min_score": 92},
                                  **on) == [92.0]
    assert await _searched_target(test_db, tmp_path, monkeypatch, {"vmaf_target": 85, "vmaf_min_score": 0},
                                  **on) == [85.0]
    # with VMAF analysis off the minimum isn't in force: the target is the target
    assert await _searched_target(test_db, tmp_path, monkeypatch, {"vmaf_target": 85},
                                  vmaf_analysis_enabled="false", vmaf_min_score=90) == [85.0]
