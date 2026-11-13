"""Edit settings, from the Queue (v0.10.0): pending jobs take Add to Queue's
settings where they are. A maintainer queued x265 files without Force
re-encode, so they waited as clean-ups; editing them with it turns them
into conversions by their rule, without removing and re-adding them. An
edit adds nothing (a file no longer waiting stays out of the queue), and a
job keeps its priority unless one is picked."""
import re

import aiosqlite
import pytest

from backend.tests.test_vmaf_target_rules import FILM, REALITY, _rule, library  # noqa: F401  (a fixture)
from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source, _text


async def _waiting(db_path, *jobs):
    """Pending jobs (path, job type, priority) in this order; the files are
    x265 already, as the maintainer's were."""
    async with aiosqlite.connect(db_path) as db:
        for order, (path, job_type, priority) in enumerate(jobs, start=1):
            await db.execute("UPDATE scan_results SET video_codec = 'hevc', needs_conversion = 0 WHERE file_path = ?",
                             (path,))
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, priority, queue_order, created_at) "
                "VALUES (?, ?, 'pending', ?, ?, datetime('now'))", (path, job_type, priority, order))
        await db.commit()


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY queue_order") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _edit(**fields):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    return await add_jobs_from_scan(BulkQueueFromScanRequest(edit_pending=True, **fields))


@pytest.mark.asyncio
async def test_a_clean_up_becomes_its_rules_conversion_where_it_waits(library):
    await _rule("Reality", "/m/Reality", nvenc_cq=38)
    await _waiting(library, (FILM, "convert", 0), (REALITY, "audio", 1))
    before = await _jobs(library)
    result = await _edit(file_paths=[REALITY], force_reencode=True)
    assert (result["updated"], result["added"], result["unchanged"]) == (1, 0, 0)
    after = await _jobs(library)
    assert [j["id"] for j in after] == [j["id"] for j in before]  # the same jobs, in their places
    film, reality = after
    assert reality["job_type"] in ("convert", "combined") and reality["nvenc_cq"] == 38  # the rule's quality
    assert reality["priority"] == 1  # Auto (unchanged) keeps the job's
    assert film == before[0]  # not edited


@pytest.mark.asyncio
async def test_a_priority_picked_is_every_edited_jobs(library):
    await _waiting(library, (REALITY, "audio", 1))
    await _edit(file_paths=[REALITY], force_reencode=True, priority_override=2)
    assert (await _jobs(library))[0]["priority"] == 2


@pytest.mark.asyncio
async def test_an_edit_adds_nothing(library):
    """A job that started or finished while the dialog was open isn't queued
    again (a converted file would be converted twice)."""
    await _waiting(library, (REALITY, "audio", 0))
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE jobs SET status = 'running'")
        await db.commit()
    result = await _edit(file_paths=[REALITY, FILM], force_reencode=True)
    assert (result["updated"], result["added"], result["unchanged"]) == (0, 0, 2)
    jobs = await _jobs(library)
    assert [(j["file_path"], j["status"], j["job_type"]) for j in jobs] == [(REALITY, "running", "audio")]



# Where the dialog starts. It started from the rules' settings: a VMAF target
# of 95 picked for a job showed as the rule's Off, and saving another change
# put the Off back.
async def _queue(*paths, **fields):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(paths), **fields))


async def _picked(db_path):
    from backend.routes.jobs import PickedSettingsRequest, picked_settings
    return await picked_settings(PickedSettingsRequest(job_ids=[j["id"] for j in await _jobs(db_path)]))


@pytest.mark.asyncio
async def test_the_edit_starts_from_the_settings_picked(library):
    """What Add to Queue (or an edit) gave a job; what its rule gives it stays the rule's."""
    await _rule("Reality", "/m/Reality", nvenc_preset="p6", nvenc_cq=36)
    await _queue(REALITY, vmaf_target_override=95)
    assert await _picked(library) == {"picked": {"vmaf_target": 95}, "mixed": []}


@pytest.mark.asyncio
async def test_a_job_queued_as_it_came_has_nothing_picked(library):
    await _rule("Reality", "/m/Reality", nvenc_preset="p6", nvenc_cq=36)
    await _queue(REALITY, FILM, direct_play=False)
    assert await _picked(library) == {"picked": {}, "mixed": []}


@pytest.mark.asyncio
async def test_each_setting_picked_in_the_dialogs_terms(library):
    async with aiosqlite.connect(library) as db:  # Off is picked over Settings' 95
        await db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                             [("vmaf_target_enabled", "true"), ("vmaf_target_score", "95")])
        await db.commit()
    await _queue(FILM, encoder_override="nvenc", nvenc_preset_override="p7", nvenc_cq_override=30,
                 audio_codec_override="eac3", audio_bitrate_override=640, target_resolution_override="720p",
                 vmaf_target_override=0, lossless_audio_override="keep", nvenc_aq_override="strong")
    assert (await _picked(library))["picked"] == {
        "encoder": "nvenc", "nvenc_preset": "p7", "nvenc_cq": 30, "audio_codec": "eac3", "audio_bitrate": 640,
        "target_resolution": "720p", "vmaf_target": 0, "lossless_audio": "keep", "nvenc_aq": "strong"}


@pytest.mark.asyncio
async def test_a_forced_conversion_starts_forced(library):
    """The x265 clean-up made a conversion by an edit shows Force re-encode,
    not the rule's quality it took as picked."""
    await _rule("Reality", "/m/Reality", nvenc_cq=38)
    await _waiting(library, (REALITY, "audio", 0))
    await _edit(file_paths=[REALITY], force_reencode=True)
    assert await _picked(library) == {"picked": {"force_reencode": True}, "mixed": []}


@pytest.mark.asyncio
async def test_a_clean_up_picked_starts_as_one(library):
    """A file that needs converting, waiting as a clean-up: Cleanup only."""
    async with aiosqlite.connect(library) as db:
        await db.execute("INSERT INTO jobs (file_path, job_type, status, priority, queue_order, created_at) "
                         "VALUES (?, 'audio', 'pending', 0, 1, datetime('now'))", (FILM,))
        await db.commit()
    assert await _picked(library) == {"picked": {"cleanup_only": True}, "mixed": []}


@pytest.mark.asyncio
async def test_direct_play_unticked_or_some_fixes(library, monkeypatch):
    """Ticked as Settings have it is nothing picked; unticked, or fewer fixes, is."""
    async def problems(db, paths):
        return {p: ["audio", "subs"] for p in paths}
    monkeypatch.setattr("backend.direct_play.problems_for", problems)
    async with aiosqlite.connect(library) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('direct_play_on_convert', 'true')")
        await db.commit()
    await _queue(FILM)  # as the watcher queues: Settings' ("1")
    assert (await _picked(library))["picked"] == {}
    await _edit(file_paths=[FILM], direct_play=True)
    assert (await _picked(library))["picked"] == {}
    await _edit(file_paths=[FILM], direct_play=True, direct_play_fixes=["audio"])
    assert (await _picked(library))["picked"] == {"direct_play": True, "direct_play_fixes": ["audio"]}
    await _edit(file_paths=[FILM], direct_play=False)
    assert (await _picked(library))["picked"] == {"direct_play": False}


@pytest.mark.asyncio
async def test_jobs_that_differ_are_mixed(library):
    """A setting the jobs share starts picked; one they differ in, the dialog names."""
    await _queue(FILM, REALITY, vmaf_target_override=95, nvenc_cq_override=30)
    await _edit(file_paths=[FILM], vmaf_target_override=90, nvenc_cq_override=30)
    assert await _picked(library) == {"picked": {"nvenc_cq": 30}, "mixed": ["vmaf_target"]}
    await _edit(file_paths=[FILM], nvenc_cq_override=30)  # FILM back to its rule's (Settings') target
    assert await _picked(library) == {"picked": {"nvenc_cq": 30}, "mixed": ["vmaf_target"]}


# The Queue's side. The frontend has no test runner: these check the source
# and its catalogs.
KEYS = {  # each key's placeholders, in English and Spanish (plurals too)
    "queue:panel.editSettings": set(),
    "queue:item.editSettings": set(),
    "queue:toasts.settingsSaved": {"count"},
    "queue:toasts.settingsUnchanged": {"count"},
    "scannerModals:estimate.editTitle": {"count"},
    "scannerModals:estimate.priorityUnchanged": set(),
    "scannerModals:estimate.priorityUnchangedHelp": set(),
    "scannerModals:estimate.editMixed": {"settings"},
}


def test_the_edit_speaks_both_languages():
    for lang in PLURALS:
        for key, names in KEYS.items():
            assert all(found == names for found in _placeholders(lang, key)), (lang, key)


def test_the_queue_edits_with_add_to_queues_dialog():
    """Edit settings in the selection bar, and a pending row's pencil, open
    Add to Queue's dialog for those jobs; saving sends edit_pending with the
    fields Add to Queue sends (queueRequestFields, one mapping for both)."""
    queue = _source("QueuePage.tsx")
    assert "<EstimateModal" in queue and "editing={" in queue
    assert "edit_pending: true" in queue
    assert "queueRequestFields(" in queue and "queueRequestFields(" in _source("ScannerPage.tsx")
    assert queue.count("onEdit=") == 2  # the selection bar and the rows
    assert 't("queue:panel.editSettings")' in _source("QueueControlPanel.tsx")
    row = _source("JobListItem.tsx")
    assert 't("queue:item.editSettings")' in row and "onEdit(job)" in row


def test_the_dialog_saves_an_edit_and_keeps_priorities():
    src = _source("EstimateModal.tsx")
    assert "scannerModals:estimate.editTitle" in src
    assert re.search(r"editing\s*\?\s*t\(\"scannerModals:estimate.priorityUnchanged\"\)", src)
    footer = src[src.index('className="estimate-modal-footer"'):]
    assert footer.index("{!editing &&") < footer.index("scannerModals:estimate.addAndStart")  # no Add & start
    assert 't("common:actions.save")' in footer


def test_the_selection_bar_edits_settings_in_one_place():
    """Edit settings replaces the Video preset, Audio preset and VMAF target
    menus (it does what they did, and more): with them the bar took two
    lines. Priority stays a menu."""
    bar = _source("QueueControlPanel.tsx")
    assert not re.search(r"onChange(VideoPreset|AudioPreset|VmafTarget)", bar + _source("QueuePage.tsx"))
    assert "onChangePriority" in bar
    for lang in PLURALS:
        for key in ("panel.videoPresetPlaceholder", "panel.audioPresetPlaceholder", "panel.vmafTargetPlaceholder",
                    "toasts.videoPresetApplied_other", "toasts.vmafTargetSet"):
            assert _text(lang, f"queue:{key}") is None, (lang, key)  # unused strings went with them


def test_the_dialog_starts_from_the_jobs_settings():
    """Edit settings asks what the jobs were given (picked-settings), starts
    there and opens the settings that show it, and names the ones the jobs
    differ in."""
    src = _source("EstimateModal.tsx")
    assert "getPickedSettings(editing)" in src
    assert 't("scannerModals:estimate.editMixed"' in src
    assert "editing={editing.map(j => j.id)}" in _source("QueuePage.tsx")
    assert '"/jobs/picked-settings"' in _source("api.ts")


def test_a_row_redraws_when_any_of_its_jobs_fields_changes():
    """The row and its plan compared the fields on a list, without the VMAF
    target: a target set by Edit settings left the row at P6 / CQ 36, and the
    plan saying so, until something else redrew them."""
    row = _source("JobListItem.tsx")
    compare = row[row.index("function shallowJobEqual"):]
    compare = compare[:compare.index("\n}\n")]
    assert "Object.keys(a)" in compare and "a.nvenc_cq !== b.nvenc_cq" not in compare
    assert "const version = JSON.stringify(job);" in _source("PendingJobPlan.tsx")
