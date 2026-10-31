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


@pytest.mark.asyncio
async def test_the_estimate_says_what_each_auto_comes_to(library):
    """Add to Queue's Encoder, Preset, Audio and Resolution said "Auto" (and
    Resolution "keep original", whatever Settings had): each now names
    Settings' value and the ones rules give their files instead."""
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    await _settings(library, default_encoder="nvenc", nvenc_preset="p3", audio_codec="eac3", audio_bitrate=640)
    await _rule("Films", "/m/Films", nvenc_preset="p4", target_resolution="1080p")
    await _rule("Reality", "/m/Reality", encoder="libx265", audio_codec="copy")
    auto = (await estimate_jobs(EstimateRequest(file_paths=[FILM, PLAIN, REALITY])))["auto"]
    assert auto == {
        "encoder": {"settings": {"nvenc": 2}, "rules": {"libx265": 1}},
        "preset": {"settings": {"p3": 1, "medium": 1}, "rules": {"p4": 1}},  # Reality's libx265: Settings' medium
        "audio": {"settings": {"eac3:640": 2}, "rules": {"copy": 1}},
        "resolution": {"settings": {"copy": 2}, "rules": {"1080p": 1}},
    }
    # The dialog's encoder decides which preset each file has.
    cpu = (await estimate_jobs(EstimateRequest(file_paths=[FILM, PLAIN], encoder="libx265")))["auto"]
    assert cpu["preset"] == {"settings": {"medium": 2}, "rules": {}}
    assert (await estimate_jobs(EstimateRequest(file_paths=[FILM], cleanup_only=True)))["auto"]["preset"] == {
        "settings": {}, "rules": {}}  # nothing converted


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_adding_a_waiting_file_again_updates_its_job(library):
    """A file already waiting in the queue was skipped, and Add to Queue's
    choices were dropped: re-added with P4 to fix a batch queued at P3, the
    maintainer's jobs kept P3. Its pending job takes them now, in its place
    in the queue; a running one is left alone, and the other ways in
    (policies, webhooks, download scripts) still skip it."""
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _settings(library, vmaf_target_enabled="true", vmaf_target_score=93)
    first = await _queue(library, FILM, PLAIN)
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE jobs SET status = 'running' WHERE file_path = ?", (PLAIN,))
        await db.commit()

    result = await add_jobs_from_scan(BulkQueueFromScanRequest(
        file_paths=[FILM, PLAIN], vmaf_target_override=0, nvenc_preset_override="p4", priority=2,
        update_pending=True))

    assert (result["added"], result["updated"], result["skipped_existing"]) == (0, 1, 1)
    jobs = await _jobs(library)
    film = jobs[FILM]
    assert (film["id"], film["queue_order"], film["status"]) == (first[FILM]["id"], first[FILM]["queue_order"], "pending")
    assert (film["vmaf_target"], film["nvenc_preset"], film["priority"]) == (0, "p4", 2)
    assert (jobs[PLAIN]["vmaf_target"], jobs[PLAIN]["nvenc_preset"]) == (93, None)  # running: as it was

    again = await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[FILM], vmaf_target_override=90))
    assert (again["added"], again["updated"], again["skipped_existing"]) == (0, 0, 1)
    assert (await _jobs(library))[FILM]["vmaf_target"] == 0
