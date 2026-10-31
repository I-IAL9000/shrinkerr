"""Add to Queue's lossless audio (v0.10.0): with Settings converting
lossless audio to EAC3, there was no way to keep it for one job ("Copy
(passthrough)" only set the other tracks' codec). A conversion now keeps
it or converts it whatever Settings say; the choice is stored on the job,
travels with it to the encode and to remote workers, and the waiting job
says what it does with its lossless tracks."""
import json
import subprocess

import aiosqlite
import pytest

from backend.rule_actions import queued_settings, settings_overrides
from backend.tests.test_object_audio import _encoders
from backend.tests.test_queue_plans import routes  # noqa: F401  (a fixture)
from backend.tests.test_vmaf_target_rules import FILM, PLAIN, _settings, library  # noqa: F401  (a fixture)


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs") as cur:
            return {r["file_path"]: dict(r) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_a_conversion_keeps_or_converts_its_lossless_audio(library):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _settings(library, auto_convert_lossless="true")
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[FILM], lossless_audio_override="keep"))
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[PLAIN]))
    jobs = await _jobs(library)
    assert (jobs[FILM]["lossless_audio"], jobs[PLAIN]["lossless_audio"]) == ("keep", None)
    assert settings_overrides(jobs[FILM])["auto_convert_lossless"] is False
    assert "auto_convert_lossless" not in settings_overrides(jobs[PLAIN])  # Settings', when it runs

    # Re-added while waiting: it takes the new choice.
    await add_jobs_from_scan(BulkQueueFromScanRequest(
        file_paths=[FILM, PLAIN], lossless_audio_override="convert", update_pending=True))
    jobs = await _jobs(library)
    assert {p: j["lossless_audio"] for p, j in jobs.items()} == {FILM: "convert", PLAIN: "convert"}
    assert queued_settings(jobs[FILM])["auto_convert_lossless"] is True


@pytest.mark.asyncio
async def test_only_keep_or_convert(library):
    from pydantic import ValidationError

    from backend.routes.jobs import BulkQueueFromScanRequest
    with pytest.raises(ValidationError):
        BulkQueueFromScanRequest(file_paths=[FILM], lossless_audio_override="flac")


@pytest.mark.asyncio
async def test_a_remote_worker_gets_the_jobs_choice(test_db, monkeypatch):
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

    async def assigned(setting, choice):
        await _settings(test_db, auto_convert_lossless=setting)
        async with aiosqlite.connect(test_db) as db:
            await db.execute("DELETE FROM jobs")
            await db.commit()
        job_id = await JobQueue(test_db).add_job("/m/Film/film.mkv", "convert", encoder="libx265")
        async with aiosqlite.connect(test_db) as db:
            await db.execute("UPDATE jobs SET lossless_audio = ? WHERE id = ?", (choice, job_id))
            await db.commit()
        return (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"][
            "auto_convert_lossless"]
    assert await assigned("true", "keep") is False
    assert await assigned("false", "convert") is True
    assert await assigned("true", None) is True
    assert await assigned("false", None) is False


@pytest.mark.asyncio
@pytest.mark.skipif(not all(e in _encoders() for e in ("libx265", "truehd", "eac3")),
                    reason="needs ffmpeg with libx265, truehd and eac3")
@pytest.mark.parametrize("setting, choice, expected", [
    ("true", "keep", ["truehd"]), ("false", "convert", ["eac3"]), ("true", None, ["eac3"])])
async def test_a_real_encode(test_db, tmp_path, setting, choice, expected):
    """As the queue runs a job: Settings, with the job's own on top."""
    from backend.converter import convert_file, get_live_encoding_settings
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
         "-map", "0", "-map", "1", "-c:v", "libx264", "-qp", "0", "-c:a", "truehd", "-strict", "-2", str(clip)],
        check=True)
    await _settings(test_db, auto_convert_lossless=setting, lossless_target_codec="eac3", lossless_target_bitrate=192)
    job = {"job_type": "convert", "lossless_audio": choice, "vmaf_target": 0}
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast",
                                pre_settings={**await get_live_encoding_settings(), **queued_settings(job)})
    assert result["success"], result.get("error")
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "json",
         result["output_path"]], capture_output=True, text=True, check=True).stdout)
    assert [s["codec_name"] for s in out["streams"]] == expected


@pytest.mark.asyncio
async def test_the_waiting_job_says_what_it_does_with_its_lossless_audio(routes, test_db):
    from backend.tests.test_queue_plans import _file, _job, tr
    jobs_route, _ = routes
    await _file(test_db, "/m/a.mkv", [
        tr(1, "eng", True, codec="truehd", profile="Dolby TrueHD + Dolby Atmos"),
        tr(2, "eng", True, codec="dts", profile="DTS-HD MA"),
        tr(3, "fra", False, codec="flac"),  # removed: not converted
        tr(4, "eng", True, codec="ac3")])
    await _settings(test_db, auto_convert_lossless="true", lossless_target_codec="eac3", lossless_target_bitrate=640)

    async def lossless(job_type="convert", **cols):
        return (await jobs_route.job_plan(await _job(test_db, "/m/a.mkv", job_type, [3], **cols)))["lossless"]
    assert await lossless() == {"convert": [2], "keep": [1], "codec": "eac3", "bitrate": 640}  # Atmos stays
    assert await lossless(lossless_audio="keep") == {"convert": [], "keep": [1, 2], "codec": "eac3", "bitrate": 640}
    assert await lossless("audio") is None  # a clean-up converts nothing
    await _settings(test_db, auto_convert_lossless="false")
    assert await lossless() is None  # nothing chosen, nothing converted: nothing to say
    assert (await lossless(lossless_audio="convert"))["convert"] == [2]


def test_add_to_queue_has_the_choice():
    """The frontend has no test runner: these check the source and catalogs."""
    from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source
    src = _source("EstimateModal.tsx")
    audio, lossless = (src.index(f'{{t("scannerModals:overrides.{k}")}}</label>') for k in ("audio", "lossless"))
    assert audio < lossless  # under the audio codec
    assert '<option value="keep">' in src and '<option value="convert">' in src
    assert "if (lossless !== null) o.lossless_audio = lossless;" in src
    assert ("extra.lossless_audio_override = encodingOverrides.lossless_audio"
            in _source("ScannerPage.tsx"))
    for lang in PLURALS:
        assert _placeholders(lang, "scannerModals:overrides.lossless") == [set()], lang
        assert _placeholders(lang, "scannerModals:overrides.losslessKeep") == [set()], lang
        assert _placeholders(lang, "scannerModals:overrides.losslessConvert") == [{"target"}], lang


def test_the_plan_names_the_tracks():
    from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source
    src = _source("PendingJobPlan.tsx")
    assert '"queue:plan.losslessConvert"' in src and '"queue:plan.losslessKeep"' in src
    assert "job.lossless_audio" in src  # re-added with another choice: it reloads
    for lang in PLURALS:
        assert _placeholders(lang, "queue:plan.losslessConvert") == [{"tracks", "target"}], lang
        assert _placeholders(lang, "queue:plan.losslessKeep") == [{"tracks"}], lang
