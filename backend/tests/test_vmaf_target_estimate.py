"""Add to Queue and the VMAF target (v0.10.0): the estimate counts the files
whose quality the target searches for (by target), and those a rule fixes at
its own quality, and adds the search's time to the estimate."""
import json

import aiosqlite
import pytest
import pytest_asyncio

# file: the rule that matches it, if any (below)
A, B, C, D = ("/m/A (2020)/A.mkv", "/m/B (2020)/B.mkv", "/m/C (2020)/C.mkv", "/m/D (2020)/D.mkv")
FILES = [A, B, C, D]


async def _setting(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for key, value in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()


async def _rule(db, name, priority, directory, target):
    await db.execute(
        "INSERT INTO encoding_rules (name, match_conditions, priority, action, enabled, created_at, vmaf_target) "
        "VALUES (?, ?, ?, 'encode', 1, '2026-10-30', ?)",
        (name, json.dumps({"match_mode": "any",
                           "conditions": [{"type": "directory", "operator": "is", "value": directory}]}),
         priority, target))


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    """Four h264 files that need converting: B's rule switches the target
    off, C's sets 95; A and D match no rule."""
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
        for path in FILES:
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, video_bit_depth, "
                "audio_tracks_json, subtitle_tracks_json, duration, scan_timestamp) "
                "VALUES (?, 1000000000, 1, 'h264', 8, '[]', '[]', 3000, 't')", (path,))
        await _rule(db, "Fixed", 0, "/m/B (2020)", 0)
        await _rule(db, "Film", 1, "/m/C (2020)", 95)
        await db.commit()
    await _setting(test_db, vmaf_target_enabled="true", vmaf_target_score=93, parallel_jobs=2)
    return test_db


async def _estimate(**request):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    return await estimate_jobs(EstimateRequest(file_paths=FILES, **request))


async def _search_job(db_path, seconds, completed_at, status="completed", stats=None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO jobs (file_path, job_type, status, encoder, created_at, completed_at, encoding_stats) "
            "VALUES ('/m/old.mkv', 'convert', ?, 'nvenc', ?, ?, ?)",
            (status, completed_at, completed_at,
             stats if stats is not None else json.dumps({"vmaf_target": {"seconds": seconds}})))
        await db.commit()


@pytest.mark.asyncio
async def test_the_estimate_counts_targets(library):
    got = await _estimate()
    assert got["by_type"]["convert"] == 4
    assert got["vmaf_target"] == {"targets": {"93": 2, "95": 1}, "fixed_by_rule": 1}


@pytest.mark.asyncio
async def test_the_search_time_is_added(library):
    await _search_job(library, 100, "2026-10-01")
    await _search_job(library, 200, "2026-10-02")
    on = await _estimate()
    assert on["vmaf_target_search_seconds"] == 150.0
    # Compared with no target anywhere: Settings' off and the rule's own score gone.
    await _setting(library, vmaf_target_enabled="false")
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE encoding_rules SET vmaf_target = NULL")
        await db.commit()
    off = await _estimate()
    # A, C and D are searched (B's rule fixes it), by two jobs at a time.
    assert on["estimated_time_seconds"] - off["estimated_time_seconds"] == pytest.approx(3 * 150 / 2, abs=1)


@pytest.mark.asyncio
async def test_target_off_says_nothing(library):
    await _setting(library, vmaf_target_enabled="false")
    async with aiosqlite.connect(library) as db:
        await db.execute("DELETE FROM encoding_rules")
        await db.commit()
    got = await _estimate()
    assert got["by_type"]["convert"] == 4
    assert got["vmaf_target"] == {"targets": {}, "fixed_by_rule": 0}


@pytest.mark.asyncio
async def test_no_target_files_no_search_time_lookup(library):
    """The recorded searches are only read when a file is searched: the
    lookup walks the job history, and most estimates have no target."""
    await _search_job(library, 100, "2026-10-01")
    await _setting(library, vmaf_target_enabled="false")
    async with aiosqlite.connect(library) as db:
        await db.execute("DELETE FROM encoding_rules")
        await db.commit()
    assert (await _estimate())["vmaf_target_search_seconds"] == 180.0  # the default: nothing was read


@pytest.mark.asyncio
async def test_a_rules_score_searches_even_with_settings_off(library):
    """The rule's own score applies whether or not Settings' target is on;
    files that follow Settings aren't searched. A rule's Off is "fixed by
    rule" only where it overrides Settings' target: with that off there is
    nothing to override."""
    await _setting(library, vmaf_target_enabled="false")
    got = await _estimate()
    assert got["vmaf_target"] == {"targets": {"95": 1}, "fixed_by_rule": 0}
    await _setting(library, vmaf_target_enabled="true")
    assert (await _estimate())["vmaf_target"] == {"targets": {"93": 2, "95": 1}, "fixed_by_rule": 1}


@pytest.mark.asyncio
async def test_targets_are_raised_to_the_minimum(library):
    """The search never holds a target the VMAF minimum would then reject
    (a rule's own minimum, else Settings'); with the analysis off the
    minimum isn't in force."""
    await _setting(library, vmaf_analysis_enabled="true", vmaf_min_score=94)
    got = await _estimate()
    assert got["vmaf_target"]["targets"] == {"94": 2, "95": 1}
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE encoding_rules SET vmaf_min_score = 96 WHERE name = 'Film'")
        await db.commit()
    assert (await _estimate())["vmaf_target"]["targets"] == {"94": 2, "96": 1}
    await _setting(library, vmaf_analysis_enabled="false")
    assert (await _estimate())["vmaf_target"]["targets"] == {"93": 2, "95": 1}


@pytest.mark.asyncio
async def test_only_files_the_search_runs_on_are_counted(library):
    """Not a file that's only cleaned up or under two minutes (neither is
    searched); one whose length isn't known is, and so is a disc (samples
    cut from its main title, v0.10.0)."""
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET needs_conversion = 0 WHERE file_path = ?", (A,))
        await db.execute("UPDATE scan_results SET disc_type = 'bluray' WHERE file_path = ?", (C,))
        await db.execute("UPDATE scan_results SET duration = 60 WHERE file_path = ?", (D,))
        await db.commit()
    got = await _estimate()
    assert got["vmaf_target"] == {"targets": {"95": 1}, "fixed_by_rule": 1}
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET duration = NULL WHERE file_path = ?", (D,))
        await db.commit()
    assert (await _estimate())["vmaf_target"] == {"targets": {"93": 1, "95": 1}, "fixed_by_rule": 1}


@pytest.mark.asyncio
async def test_the_search_seconds_are_the_median_of_the_last_twenty(library):
    assert (await _estimate())["vmaf_target_search_seconds"] == 180.0  # none recorded yet
    for day in range(1, 11):  # older than the last twenty
        await _search_job(library, 1, f"2026-09-{day:02d}")
    for day in range(1, 21):  # a median of 100, not their mean (150)
        await _search_job(library, 100 if day <= 15 else 300, f"2026-10-{day:02d}")
    # Not completed, no search recorded, not JSON: none of these count.
    await _search_job(library, 5000, "2026-10-25", status="failed")
    await _search_job(library, None, "2026-10-26", stats=json.dumps({"vmaf": 94}))
    await _search_job(library, None, "2026-10-27", stats="not json")
    assert (await _estimate())["vmaf_target_search_seconds"] == 100.0
