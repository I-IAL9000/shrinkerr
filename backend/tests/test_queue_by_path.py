"""Add-by-path (the NZBGet / SABnzbd scripts) and the queue webhook (v0.10.0).

Both probed the file and typed the job on their own: add-by-path ignored an
"ignore" rule, the webhook applied no rules at all, the original language
came from the tracks only (so no reorder), and the file never reached the
Scanner. Now each file is stored as the watcher stores it and queued as Add
to Queue would queue it, less files with nothing to do."""
import json

import aiosqlite
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch


def track(si, lang, **extra):
    return {"stream_index": si, "language": lang, "codec": "aac", "channels": 2, **extra}


def probe(audio=(), subs=(), codec="h264"):
    return {"video_codec": codec, "video_width": 1920, "video_height": 1080, "duration": 3600,
            "file_size": 2_000_000_000, "audio_tracks": [dict(t) for t in audio],
            "subtitle_tracks": [dict(t) for t in subs]}


@pytest_asyncio.fixture
async def env(test_db, tmp_path, monkeypatch):
    """A media folder, a queue that doesn't encode, probes and TMDB faked:
    `probes[path] = probe(...)`, `natives[path] = "jpn"`."""
    import sys
    import backend.config
    import backend.metadata as metadata
    import backend.scanner as scanner
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    scanner.invalidate_sub_settings_cache()
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    media = tmp_path / "media"
    media.mkdir()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"eng\"]')")
        await db.commit()
    probes, natives = {}, {}

    async def fake_probe(path, *a, **kw):
        return probes.get(path)

    async def fake_lookup(path):
        return natives.get(path)
    monkeypatch.setattr(scanner, "probe_file", fake_probe)
    monkeypatch.setattr(metadata, "lookup_original_language", fake_lookup)

    def file(name, p=None, native=None):
        f = media / name
        f.write_bytes(b"x")
        if p is not None:
            probes[str(f)] = p
        if native:
            natives[str(f)] = native
        return str(f)
    yield test_db, file
    scanner.invalidate_sub_settings_cache()


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs ORDER BY queue_order") as cur:
            return [dict(r) for r in await cur.fetchall()]


def _rules(rules):
    return patch("backend.rule_resolver.resolve_rules_for_batch", new=AsyncMock(side_effect=rules))


def _rule(**values):
    return {"rule_name": "R", "action": "encode", "queue_priority": None, "encoder": None, "nvenc_preset": None,
            "nvenc_cq": None, "libx265_crf": None, "libx265_preset": None, "target_resolution": None,
            "audio_codec": None, "audio_bitrate": None, **values}


@pytest.mark.asyncio
async def test_add_by_path_stores_the_file_and_moves_the_original_language_first(env):
    """An anime episode with the English dub first: TMDB says Japanese, so
    the Japanese audio is moved first. From the tracks alone (the first
    one: English) there was nothing to do and nothing was queued."""
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    db_path, file = env
    ep = file("Show - 01.mkv", probe([track(1, "eng"), track(2, "jpn")], codec="hevc"), native="jpn")
    assert (await add_jobs_by_path(AddByPathRequest(file_paths=[ep])))["added"] == 1
    assert [j["job_type"] for j in await _jobs(db_path)] == ["audio"]
    async with aiosqlite.connect(db_path) as db:  # in the Scanner, as new
        async with db.execute("SELECT native_language, language_source, new_detected_at FROM scan_results "
                              "WHERE file_path = ?", (ep,)) as cur:
            native, source, detected = await cur.fetchone()
    assert (native, source) == ("jpn", "api") and detected


@pytest.mark.asyncio
async def test_add_by_path_follows_an_ignore_rule(env):
    """"ignore" keeps the video as it is and still cleans the tracks (it
    converted anyway); with no tracks to clean nothing is queued."""
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    db_path, file = env
    a = file("A.mkv", probe([track(1, "eng"), track(2, "fre")]))
    b = file("B.mkv", probe([track(1, "eng")]))

    async def rules(paths, extra_context=None):
        return {p: _rule(action="ignore") for p in paths}
    with _rules(rules):
        await add_jobs_by_path(AddByPathRequest(file_paths=[a, b]))
    jobs = await _jobs(db_path)
    assert [(j["file_path"], j["job_type"], json.loads(j["audio_tracks_to_remove"])) for j in jobs] == [(a, "audio", [2])]


@pytest.mark.asyncio
async def test_add_by_path_passes_the_download_category_to_the_rules(env):
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    _, file = env
    f = file("Film.mkv", probe([track(1, "eng")]))
    seen = []

    async def rules(paths, extra_context=None):
        seen.append(extra_context)
        return {p: None for p in paths}
    with _rules(rules):
        await add_jobs_by_path(AddByPathRequest(file_paths=[f], nzbget_category="movies"))
    assert seen == [{"nzbget_category": "movies"}]


@pytest.mark.asyncio
async def test_the_webhook_applies_rules(env):
    from backend.routes.webhooks import WebhookQueueRequest, webhook_queue
    db_path, file = env
    keep = file("Keep.mkv", probe([track(1, "eng")]))
    skip = file("Skip.mkv", probe([track(1, "eng")]))

    async def rules(paths, extra_context=None):
        return {keep: _rule(nvenc_cq=30, queue_priority=2), skip: _rule(action="skip")}
    with _rules(rules):
        assert (await webhook_queue(WebhookQueueRequest(paths=[keep, skip])))["added"] == 1
    [job] = await _jobs(db_path)
    assert (job["file_path"], job["nvenc_cq"], job["priority"]) == (keep, 30, 2)


@pytest.mark.asyncio
async def test_a_track_set_by_hand_stays_when_the_file_is_queued_by_path(env):
    """French kept by hand in the Scanner: the default rules would remove
    it, and the old path re-classified the fresh probe."""
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    db_path, file = env
    f = file("Film.mkv", probe([track(1, "eng"), track(2, "fre")]))
    stored = [track(1, "eng", keep=True, locked=False), track(2, "fre", keep=True, locked=False, manual=True)]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, native_language, audio_tracks_json, scan_timestamp) "
            "VALUES (?, 1, 'eng', ?, '2026-10-10')", (f, json.dumps(stored)))
        await db.commit()
    await add_jobs_by_path(AddByPathRequest(file_paths=[f]))
    [job] = await _jobs(db_path)
    assert job["job_type"] == "convert" and json.loads(job["audio_tracks_to_remove"]) == []


@pytest.mark.asyncio
async def test_queueing_by_path_reports_what_it_could_not_queue(env, tmp_path):
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    db_path, file = env
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"x")
    unprobed = file("Broken.mkv")  # the probe fails
    nothing = file("Done.mkv", probe([track(1, "eng")], codec="hevc"))  # nothing to do
    missing = str(tmp_path / "media" / "Gone.mkv")
    r = await add_jobs_by_path(AddByPathRequest(file_paths=[str(outside), unprobed, nothing, missing]))
    assert r["added"] == 0
    assert r["errors"] == [f"Outside media dirs: {outside}", f"Probe failed: {unprobed}", f"File not found: {missing}"]
    assert await _jobs(db_path) == []


@pytest.mark.asyncio
async def test_insert_next_puts_the_jobs_first(env):
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    db_path, file = env
    first = file("First.mkv", probe([track(1, "eng")]))
    await add_jobs_by_path(AddByPathRequest(file_paths=[first]))
    urgent = file("Urgent.mkv", probe([track(1, "eng")]))
    await add_jobs_by_path(AddByPathRequest(file_paths=[urgent], insert_next=True))
    assert [j["file_path"] for j in await _jobs(db_path)] == [urgent, first]
