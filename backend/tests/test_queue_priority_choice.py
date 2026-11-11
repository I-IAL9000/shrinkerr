"""Add to Queue's priority (v0.10.0): items added at High showed Highest in
the queue — a job took the higher of the dialog's priority and its rule's,
and the dialog never said so. Auto (the default) now takes the rule's,
named in the dialog; a priority picked there is the job's, over the rule."""
import aiosqlite
import pytest

from backend.tests.test_vmaf_target_rules import (  # noqa: F401  (library: a fixture)
    FILM, PLAIN, REALITY, _rule, _settings, library)


async def _priorities(db_path, *paths, **choices):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(paths), **choices))
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, priority FROM jobs") as cur:
            return dict(await cur.fetchall())


async def _unwatched(db_path, folder):
    await _settings(db_path, plex_prioritize_unwatched="true")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES (?, 'watch_status', 'unwatched', '2026-11-11T00:00:00')", (folder,))
        await db.commit()


@pytest.mark.asyncio
async def test_a_priority_picked_is_the_jobs_over_a_rules(library):
    await _rule("Films", "/m/Films", queue_priority=2)
    assert await _priorities(library, FILM, PLAIN, priority_override=1) == {FILM: 1, PLAIN: 1}


@pytest.mark.asyncio
async def test_normal_picked_lowers_a_rules_and_an_unwatched_titles(library):
    await _rule("Films", "/m/Films", queue_priority=2)
    await _unwatched(library, "/m/Plain")
    assert await _priorities(library, FILM, PLAIN, priority_override=0) == {FILM: 0, PLAIN: 0}


@pytest.mark.asyncio
async def test_on_auto_the_rule_or_an_unwatched_title_decides(library):
    await _rule("Films", "/m/Films", queue_priority=2)
    await _unwatched(library, "/m/Plain")
    assert await _priorities(library, FILM, PLAIN, REALITY) == {FILM: 2, PLAIN: 1, REALITY: 0}


@pytest.mark.asyncio
async def test_automatic_queueing_still_takes_the_higher(library):
    """The watcher's auto-queue priority (and the download scripts') is a
    floor a rule can raise, as before."""
    from backend.queue import JobQueue
    from backend.routes.jobs import queue_new_files
    await _rule("Films", "/m/Films", queue_priority=2)
    await queue_new_files([FILM, PLAIN], 1, JobQueue(library))
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT file_path, priority FROM jobs") as cur:
            assert dict(await cur.fetchall()) == {FILM: 2, PLAIN: 1}


@pytest.mark.asyncio
async def test_adding_a_waiting_file_again_at_a_lower_priority_lowers_it(library):
    await _rule("Films", "/m/Films", queue_priority=2)
    assert await _priorities(library, FILM) == {FILM: 2}
    assert await _priorities(library, FILM, priority_override=1, update_pending=True) == {FILM: 1}


@pytest.mark.asyncio
async def test_the_estimate_says_what_auto_comes_to(library):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    await _rule("Films", "/m/Films", queue_priority=2)
    await _unwatched(library, "/m/Plain")
    auto = (await estimate_jobs(EstimateRequest(file_paths=[FILM, PLAIN, REALITY])))["auto"]
    assert auto["priority"] == {"settings": {"0": 1, "1": 1}, "rules": {"2": 1}}
