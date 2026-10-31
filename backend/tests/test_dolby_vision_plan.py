"""What a conversion does with Dolby Vision, before it runs (v0.10.0): with
Settings → Video → Dolby Vision on "Convert to HDR10", neither Add to
Queue's "What will happen" nor a waiting job's said so. Both now say it —
dropped and the HDR under it converted, kept, or the video not converted
and why — from the same decision the encode makes (dovi.encode_outcome)."""
import json
import shutil

import aiosqlite
import pytest

from backend import dovi
from backend.tests.test_dolby_vision_mode import _command
from backend.tests.test_queue_plans import _file, _job, routes, tr  # noqa: F401  (routes: a fixture)
from backend.tests.test_vmaf_target_rules import FILM, PLAIN, _settings, library  # noqa: F401  (a fixture)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["keep", "keep_or_hdr10", "hdr10"])
@pytest.mark.parametrize("hdr_format", ["dv8", "dv5"])
@pytest.mark.parametrize("resolution", ["copy", "1080p"])
@pytest.mark.parametrize("strip", [True, False])
async def test_the_plan_is_what_the_encode_does(test_db, tmp_path, monkeypatch, mode, hdr_format, resolution, strip):
    result = await _command(test_db, tmp_path, mode, hdr_format, resolution, monkeypatch=monkeypatch, strip=strip)
    if result["success"]:
        encoded = ("hdr10" if "dovi_rpu=strip=1" in " ".join(result["command"]) else None, None)
    else:
        encoded = (None, result["error_key"].removeprefix("errors.dolbyVisionKept."))
    assert dovi.encode_outcome(hdr_format, "hdr10", mode, encoder="libx265", output_codec=None,
                               target_resolution=resolution, disc_type=None) == encoded


def _dv(path, hdr="dv8", probe=True):
    """Make a scanned file Dolby Vision (its stored probe says what's under it)."""
    probe_json = json.dumps({"probe": {"dv_base": "hdr10"}}) if probe else None
    return ("UPDATE scan_results SET hdr_format = ?, probe_json = ? WHERE file_path = ?", (hdr, probe_json, path))


@pytest.mark.asyncio
async def test_a_waiting_job_says_what_becomes_of_it(routes, test_db, monkeypatch):
    jobs_route, _ = routes
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    monkeypatch.setattr(dovi, "_strip_filter", lambda: True)
    await _file(test_db, "/m/a.mkv", [tr(1, "eng", True), tr(2, "fra", False)])
    async with aiosqlite.connect(test_db) as db:
        await db.execute(*_dv("/m/a.mkv"))
        await db.commit()

    async def plan(mode, job_type="convert", audio_remove=(), **cols):
        await _settings(test_db, dolby_vision_mode=mode, default_encoder="libx265")
        return await jobs_route.job_plan(await _job(test_db, "/m/a.mkv", job_type, audio_remove, **cols))
    p = await plan("hdr10")
    assert p["dolby_vision"] == {"profile": "8", "action": "drop", "hdr": "hdr10", "why": None}
    assert (await plan("keep"))["dolby_vision"]["action"] == "keep"
    kept = await plan("keep", "combined", [2], target_resolution="1080p")  # resized: it can't be kept
    assert (kept["dolby_vision"]["action"], kept["dolby_vision"]["why"]) == ("skip", "resize")
    assert kept["estimated_savings"] == 100 * 1024 ** 2  # the French track's: the video isn't converted
    assert (await plan("keep_or_hdr10", target_resolution="1080p"))["dolby_vision"]["action"] == "drop"
    # A clean-up copies the video: it removes Dolby Vision only under "Convert to HDR10".
    assert (await plan("hdr10", "audio", [2]))["dolby_vision"] == {
        "profile": "8", "action": "remove", "hdr": "hdr10", "why": None}
    assert (await plan("keep", "audio", [2]))["dolby_vision"] is None

    async with aiosqlite.connect(test_db) as db:
        await db.execute(*_dv("/m/a.mkv", "dv5"))
        await db.commit()
    assert (await plan("hdr10"))["dolby_vision"]["why"] == "profile"
    assert (await plan("hdr10", "audio", [2]))["dolby_vision"]["action"] == "keep"


@pytest.mark.asyncio
async def test_the_dialog_counts_them(library, monkeypatch):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    monkeypatch.setattr(dovi, "_strip_filter", lambda: True)
    async with aiosqlite.connect(library) as db:
        await db.execute(*_dv(FILM))
        await db.execute(*_dv(PLAIN, probe=False))  # scanned before scans read what's under it
        await db.commit()

    async def converted(mode, **choices):
        await _settings(library, dolby_vision_mode=mode)
        return (await estimate_jobs(EstimateRequest(file_paths=[FILM, PLAIN], **choices)))["dolby_vision_converted"]
    assert await converted("hdr10") == {"dropped": {"hdr10": 2}, "kept": 0, "not_converted": {}}
    assert await converted("keep") == {"dropped": {}, "kept": 2, "not_converted": {}}
    assert await converted("keep", target_resolution_override="1080p") == {
        "dropped": {}, "kept": 0, "not_converted": {"resize": 2}}
    await _settings(library, target_resolution="1080p")  # Settings' resize, as the job takes it
    assert (await converted("keep"))["not_converted"] == {"resize": 2}
    await _settings(library, target_resolution="copy")
    assert (await converted("hdr10", cleanup_only=True)) == {"dropped": {}, "kept": 0, "not_converted": {}}


def test_the_lines_and_their_words():
    """The frontend has no test runner: these check the source and catalogs."""
    from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source
    plan = _source("PendingJobPlan.tsx")
    assert '"queue:plan.dolbyVision.skip"' in plan and "`queue:plan.dolbyVision.${" in plan
    modal = _source("EstimateModal.tsx")
    assert "rows.push(...dvConvertedRows(estimate.dolby_vision_converted));" in modal
    for lang in PLURALS:
        for action, names in (("drop", {"profile", "hdr"}), ("remove", {"profile", "hdr"}), ("keep", {"profile"}),
                              ("keepWhy", {"profile", "why"}), ("skip", {"why"})):
            assert _placeholders(lang, f"queue:plan.dolbyVision.{action}") == [names], (lang, action)
        for why in ("disc", "profile", "tools", "encoder", "codec", "resize", "ffmpeg"):
            assert _placeholders(lang, f"queue:plan.dolbyVisionWhy.{why}") == [set()], (lang, why)
        for key, names in (("dropped", {"num", "hdr"}), ("kept", {"num"}), ("notConverted", {"num", "why"})):
            assert all(p == names for p in _placeholders(
                lang, f"scannerModals:estimate.whatHappens.dolbyVisionConverted.{key}")), (lang, key)
