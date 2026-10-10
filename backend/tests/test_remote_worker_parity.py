"""v0.10.0: remote workers follow the same safety rules as the local one.

Workers hard-coded "no backup, no trash" (so originals were permanently
deleted however the server was set up), ran health-check jobs as a no-op
reported as success, and a remote completion never updated the Scanner row
or the job's paths (H7) — the converted file kept showing as needing
conversion, with stale track data.
"""
import json
import shutil
from types import SimpleNamespace

import aiosqlite
import pytest

import backend.nodes as nodes
import backend.routes.nodes as nodes_route
from backend.nodes import NodeManager
from backend.queue import JobQueue


@pytest.fixture
def node_api(test_db, monkeypatch):
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*args, **kwargs):
        return None

    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = NodeManager()
    return nm, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))


async def _setup(db_path, settings: dict, mappings=None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO worker_nodes (id, name, capabilities, status, registered_at, path_mappings) "
            "VALUES ('node-1', 'node-1', ?, 'online', '2026-10-08T00:00:00', ?)",
            (json.dumps(["libx265"]), json.dumps(mappings or [])))
        for k, v in settings.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.mark.asyncio
async def test_workers_get_the_originals_policy_and_no_health_checks(node_api, test_db):
    nm, request = node_api
    await _setup(test_db, {"backup_original_days": "7", "trash_original_after_conversion": "false"})
    queue = JobQueue(test_db)
    await queue.add_job("/media/a.mkv", "health_check")
    await queue.add_job("/media/b.mkv", "convert", encoder="libx265")

    res = await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request)

    job = res["job"]
    assert job["file_path"] == "/media/b.mkv"
    assert job["backup_original_days"] == 7
    assert job["trash_original_after_conversion"] is False


@pytest.mark.asyncio
async def test_unmapped_backup_folder_falls_back_to_beside_the_file(node_api, test_db):
    nm, request = node_api
    await _setup(test_db, {"backup_original_days": "7", "backup_folder": "/backups"},
                 mappings=[{"server": "/media", "worker": "/mnt/media"}])
    await JobQueue(test_db).add_job("/media/b.mkv", "convert", encoder="libx265")

    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]

    assert job["file_path"] == "/mnt/media/b.mkv"
    assert job["backup_folder"] == ""


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_remote_completion_updates_the_scan_row_and_job(node_api, test_db, tmp_path):
    from backend.tests.test_same_name_conversion import _mkv
    nm, request = node_api
    await _setup(test_db, {})
    src = tmp_path / "Movie (2009) 1080p x264.mkv"
    out = tmp_path / "Movie (2009) 1080p x265.mkv"
    _mkv(out, ["eng"])  # what the worker produced (the source is gone)
    tracks = [{"stream_index": 1, "language": "fre", "codec": "aac", "channels": 1, "keep": False},
              {"stream_index": 2, "language": "eng", "codec": "aac", "channels": 1, "keep": True}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
            "native_language, scan_timestamp, has_removable_tracks_flag) "
            "VALUES (?, 10000000, 'h264', 1, ?, 'eng', '2026-10-08T00:00:00', 1)", (str(src), json.dumps(tracks)))
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "combined", encoder="libx265", audio_tracks_to_remove=[1])
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    assert await nm.assign_job_to_node("node-1", job) is not None

    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=True, output_path=str(out), space_saved=5000,
        replaced_source=True), request)

    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM scan_results") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT file_path, original_file_path, status FROM jobs WHERE id = ?", (job_id,)) as cur:
            jrow = dict(await cur.fetchone())
    assert [r["file_path"] for r in rows] == [str(out)]
    assert rows[0]["video_codec"] == "hevc" and rows[0]["needs_conversion"] == 0
    assert [(t["stream_index"], t["language"]) for t in json.loads(rows[0]["audio_tracks_json"])] == [(1, "eng")]
    assert jrow == {"file_path": str(out), "original_file_path": str(src), "status": "completed"}


@pytest.mark.asyncio
async def test_workers_get_the_output_and_audio_settings_and_sidecar_subtitles(node_api, test_db, tmp_path, monkeypatch):
    """Workers hard-coded no filename suffix, no custom flags and no lossless
    conversion, and never merged sidecar subtitles (v0.10.0)."""
    import backend.config
    import backend.scanner as scanner
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    monkeypatch.setattr(scanner, "_cleanup_enabled_cache", {})
    nm, request = node_api
    media = tmp_path / "media"
    media.mkdir()
    srt = media / "b.is.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHalló\n\n")
    await _setup(test_db, {"filename_suffix": "-Shrinkerr", "custom_ffmpeg_flags": "-tune grain",
                           "auto_convert_lossless": "true", "lossless_target_codec": "ac3",
                           "lossless_target_bitrate": "448", "lossless_keep_object_audio": "false",
                           "merge_external_subs": "true",
                           "delete_external_subs_after_merge": "true"},
                 mappings=[{"server": str(media), "worker": "/mnt/media"}])
    subs = [{"stream_index": -1, "language": "ice", "codec": "subrip", "keep": True,
             "external": True, "external_path": str(srt)}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, subtitle_tracks_json) "
                         "VALUES (?, 1, '2026-10-08T00:00:00', ?)", (str(media / "b.mkv"), json.dumps(subs)))
        await db.commit()
    await JobQueue(test_db).add_job(str(media / "b.mkv"), "convert", encoder="libx265")

    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]

    assert (job["filename_suffix"], job["custom_ffmpeg_flags"]) == ("-Shrinkerr", "-tune grain")
    assert (job["auto_convert_lossless"], job["lossless_target_codec"], job["lossless_target_bitrate"]) == (True, "ac3", 448)
    assert job["lossless_keep_object_audio"] is False
    if shutil.which("ffprobe"):  # sidecars are only merged when ffmpeg can read them
        assert [(s["path"], s["language"]) for s in job["external_subs"]] == [("/mnt/media/b.is.srt", "ice")]
    assert job["delete_external_subs_after_merge"] is True


@pytest.mark.asyncio
async def test_the_worker_hands_them_to_convert_file(tmp_path, monkeypatch):
    import backend.converter
    import backend.scanner
    from backend import worker_mode
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient
    src = tmp_path / "Movie (2009) h264.mkv"
    src.write_bytes(b"x")
    seen = {}

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_convert_file(**kwargs):
        seen.update(kwargs)
        return {"success": True, "output_path": str(src), "space_saved": 1, "error": None}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.converter, "convert_file", fake_convert_file)
    subs = [{"path": "/mnt/media/b.is.srt", "codec": "subrip", "language": "ice", "forced": False}]
    await worker_mode.execute_job(FakeClient(), "node-1", {
        "id": 9, "file_path": str(src), "job_type": "convert", "encoder": "libx265",
        "filename_suffix": "-Shrinkerr", "custom_ffmpeg_flags": "-tune grain",
        "auto_convert_lossless": True, "lossless_target_codec": "ac3", "lossless_target_bitrate": 448,
        "lossless_keep_object_audio": False, "external_subs": subs, "delete_external_subs_after_merge": True,
    }, ["libx265"])

    settings = seen["pre_settings"]
    assert (settings["filename_suffix"], settings["custom_ffmpeg_flags"]) == ("-Shrinkerr", "-tune grain")
    assert (settings["auto_convert_lossless"], settings["lossless_target_codec"], settings["lossless_target_bitrate"]) == (True, "ac3", 448)
    assert settings["lossless_keep_object_audio"] is False
    assert seen["external_subs"] == subs and seen["delete_merged_subs"] is True


@pytest.mark.asyncio
async def test_convert_file_merges_the_sidecars_it_is_given(test_db, tmp_path):
    import subprocess
    from backend.converter import convert_file
    from backend.tests.test_cleanup_merges_external_subs import _subtitle_languages
    from backend.tests.test_output_integrity import _clip, needs_libx265
    if needs_libx265.args[0]:
        pytest.skip("needs ffmpeg with libx265")
    src = tmp_path / "Movie (2009) 1080p WEB h264.mkv"
    _clip(src, seconds=2)
    srt = tmp_path / "Movie (2009) 1080p WEB h264.is.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHalló\n\n")
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast",
                                external_subs=[{"path": str(srt), "codec": "subrip", "language": "ice", "forced": False}],
                                delete_merged_subs=True)
    assert result["success"], result.get("error")
    assert _subtitle_languages(result["output_path"]) == ["ice"]
    assert not srt.exists()
