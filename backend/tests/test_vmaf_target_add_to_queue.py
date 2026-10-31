"""Add to Queue and the queue's selection choose a conversion's VMAF target
(v0.10.0): Settings' (or a rule's), Off — encode at the quality chosen,
with no search — or a score. With the target on, there was no way to queue
a file at a set preset and CQ: the CQ only started the search."""
import aiosqlite
import pytest

from backend.tests.test_vmaf_target_rules import (  # noqa: F401  (library: a fixture)
    FILM, PLAIN, REALITY, _refused, _rule, _settings, library)


async def _queue(db_path, *paths, **choices):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(paths), **choices))
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_add_to_queue_switches_the_target_off_at_the_quality_chosen(library):
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    await _rule("Films", "/m/Films", vmaf_target=95)
    jobs = await _queue(library, FILM, PLAIN, vmaf_target_override=0, nvenc_cq_override=28)
    assert {p: (j["vmaf_target"], j["libx265_crf"]) for p, j in jobs.items()} == {FILM: (0, None), PLAIN: (0, None)}
    assert {j["nvenc_cq"] for j in jobs.values()} == {28}  # the CQ chosen, not a search's start


@pytest.mark.asyncio
async def test_add_to_queue_sets_its_own_score_over_a_rules(library):
    await _settings(library, vmaf_target_enabled="false")
    await _rule("Films", "/m/Films", vmaf_target=95)
    await _rule("Reality", "/m/Reality", vmaf_target=0)
    jobs = await _queue(library, FILM, REALITY, PLAIN, vmaf_target_override=90)
    assert {p: j["vmaf_target"] for p, j in jobs.items()} == {FILM: 90, REALITY: 90, PLAIN: 90}


@pytest.mark.asyncio
async def test_without_a_choice_the_rule_or_settings_decide(library):
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    await _rule("Films", "/m/Films", vmaf_target=95)
    jobs = await _queue(library, FILM, PLAIN)
    assert {p: j["vmaf_target"] for p, j in jobs.items()} == {FILM: 95, PLAIN: 93}


@pytest.mark.asyncio
async def test_a_score_out_of_range_or_under_the_minimum_is_refused(library):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _refused(add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[PLAIN], vmaf_target_override=70)),
                   "rules.vmafTargetRange")
    await _settings(library, vmaf_analysis_enabled="true", vmaf_min_score=92)
    await _refused(add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[PLAIN], vmaf_target_override=90)),
                   "rules.vmafTargetBelowMinimum")


@pytest.mark.asyncio
async def test_the_estimate_follows_the_choice(library):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    assert (await estimate_jobs(EstimateRequest(file_paths=[PLAIN])))["vmaf_target"]["targets"] == {"93": 1}
    off = (await estimate_jobs(EstimateRequest(file_paths=[PLAIN], vmaf_target_override=0)))["vmaf_target"]
    assert off == {"targets": {}, "fixed_by_rule": 0}  # chosen here, not a rule's
    assert (await estimate_jobs(EstimateRequest(file_paths=[PLAIN], vmaf_target_override=90))
            )["vmaf_target"]["targets"] == {"90": 1}


@pytest.mark.asyncio
async def test_the_queue_changes_waiting_conversions_targets(library):
    from backend.queue import JobQueue
    from backend.routes.jobs import BulkUpdateSettingsRequest, bulk_update_settings
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    await JobQueue(library).add_jobs_bulk([{"file_path": "/m/clean.mkv", "job_type": "audio"}])
    jobs = await _queue(library, PLAIN)
    ids = [j["id"] for j in jobs.values()]

    async def targets():
        async with aiosqlite.connect(library) as db:
            async with db.execute("SELECT file_path, vmaf_target FROM jobs") as cur:
                return dict(await cur.fetchall())
    await bulk_update_settings(BulkUpdateSettingsRequest(job_ids=ids, vmaf_target=0))
    assert await targets() == {PLAIN: 0, "/m/clean.mkv": None}  # a clean-up encodes nothing
    await bulk_update_settings(BulkUpdateSettingsRequest(job_ids=ids, vmaf_target=95))
    assert await targets() == {PLAIN: 95, "/m/clean.mkv": None}
    await _refused(bulk_update_settings(BulkUpdateSettingsRequest(job_ids=ids, vmaf_target=70)),
                   "rules.vmafTargetRange")
