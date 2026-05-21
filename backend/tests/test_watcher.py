"""Tests for backend/watcher.py auto-queue priority resolution.

v0.5.0+: _auto_queue_new_files now runs through the rules engine instead
of using global Settings defaults directly. Verify the priority cascade:
rule.queue_priority > settings.auto_queue_priority > 0.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.watcher import FileWatcher


def _fake_scanned(path: str = "/media/test.mkv"):
    """Minimal ScannedFile-shaped object the watcher iterates over."""
    track = MagicMock()
    track.stream_index = 1
    track.keep = True
    track.locked = False
    s = MagicMock()
    s.file_path = path
    s.file_size = 1_000_000_000
    s.needs_conversion = True  # so job_type == "convert"
    s.audio_tracks = [track]
    return s


@pytest.mark.asyncio
async def test_auto_queue_priority_rule_wins_over_setting(test_db):
    """When a rule sets queue_priority=2 (Highest) and auto_queue_priority
    setting is 1 (High), the rule wins (OR-cascade, not max())."""
    # Seed the auto_queue_priority + auto_queue_new settings
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_priority', '1')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/test.mkv")

    rule_results = {
        "/media/test.mkv": {
            "queue_priority": 2,
            "action": "encode",
            "encoder": None, "nvenc_preset": None, "nvenc_cq": None,
            "libx265_crf": None, "libx265_preset": None,
            "target_resolution": None, "audio_codec": None,
            "audio_bitrate": None,
        },
    }

    captured = {}

    # NOTE: captured.update overwrites on multi-call. OK for single-file
    # batches; extend to a list if you add multi-file tests.
    async def fake_add_job(file_path, job_type, **kwargs):
        captured["file_path"] = file_path
        captured["job_type"] = job_type
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        instance = MockQueue.return_value
        instance.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 2, \
        f"Expected priority=2 (rule wins), got {captured.get('priority')}"


@pytest.mark.asyncio
async def test_auto_queue_priority_setting_wins_when_no_rule(test_db):
    """When no rule matches (or rule has no queue_priority), the global
    auto_queue_priority setting wins. Setting=1 → priority=1."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_priority', '1')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/test.mkv")

    # rule_results returns None for the path (no matching rule)
    rule_results = {"/media/test.mkv": None}

    captured = {}

    async def fake_add_job(file_path, job_type, **kwargs):
        captured["file_path"] = file_path
        captured["job_type"] = job_type
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        instance = MockQueue.return_value
        instance.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 1, \
        f"Expected priority=1 (setting wins, no rule), got {captured.get('priority')}"


@pytest.mark.asyncio
async def test_auto_queue_skip_action_short_circuits(test_db):
    """Rule action='skip' must prevent enqueue. add_job should not be called."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/skip-me.mkv")

    rule_results = {"/media/skip-me.mkv": {"action": "skip"}}

    add_job_mock = AsyncMock()

    with patch("backend.queue.JobQueue") as MockQueue:
        MockQueue.return_value.add_job = add_job_mock
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    add_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_auto_queue_ignore_action_short_circuits(test_db):
    """Rule action='ignore' must prevent enqueue (parallel to 'skip'). v0.5.0+."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/ignore-me.mkv")

    rule_results = {"/media/ignore-me.mkv": {"action": "ignore"}}

    add_job_mock = AsyncMock()

    with patch("backend.queue.JobQueue") as MockQueue:
        MockQueue.return_value.add_job = add_job_mock
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    add_job_mock.assert_not_called()


@pytest.mark.asyncio
async def test_auto_queue_date_added_rule_fires_with_priority(test_db):
    """Integration: a rule with date_added condition (matched upstream)
    correctly contributes queue_priority to the auto-queued job.
    Condition matching itself is unit-tested in test_rule_resolver.py;
    this test verifies the watcher applies a date_added-rule's
    queue_priority value to add_job. v0.5.1+."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/fresh.mkv")

    # resolve_rules_for_batch is mocked — assume the date_added condition
    # matched and the rule resolved to queue_priority=2. The watcher
    # doesn't care HOW the rule matched, only WHAT the resolved rule says.
    rule_results = {
        "/media/fresh.mkv": {
            "queue_priority": 2,
            "action": "encode",
            "encoder": None, "nvenc_preset": None, "nvenc_cq": None,
            "libx265_crf": None, "libx265_preset": None,
            "target_resolution": None, "audio_codec": None,
            "audio_bitrate": None,
        },
    }

    captured = {}
    async def fake_add_job(file_path, job_type, **kwargs):
        captured["file_path"] = file_path
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        MockQueue.return_value.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 2, \
        f"Expected priority=2 from date_added rule, got {captured.get('priority')}"


# ----------------------------------------------------------------------------
# v0.7.5: BD ISO language metadata backfill tests.
#
# Locks the contract for `_backfill_iso_languages_v075` — the one-shot
# startup sweep that re-probes existing BD ISO rows whose audio_tracks
# are all-und (pre-v0.7.4 libbluray-ctypes path) so they pick up real
# language codes without manual delete-and-rediscover.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iso_lang_backfill_v075_idempotent_when_flag_set(test_db):
    """When iso_lang_backfilled_v075 = 'true' already, the method returns
    immediately without touching scan_results."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES "
            "('iso_lang_backfilled_v075', 'true')"
        )
        # Seed a row that WOULD be a backfill candidate, to confirm we
        # don't touch it when the flag is set.
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "/media/movies/Stale (1999)/disc.iso",
                "Stale (1999)",
                "bdmv",
                '[{"language":"und"},{"language":"und"}]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    await watcher._backfill_iso_languages_v075()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT audio_tracks_json FROM scan_results WHERE file_path = ?",
            ("/media/movies/Stale (1999)/disc.iso",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["audio_tracks_json"] == '[{"language":"und"},{"language":"und"}]'
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_iso_lang_backfill_v075_sets_flag_when_no_candidates(test_db):
    """Empty result set still sets the flag (so a clean install doesn't
    re-query scan_results on every watcher cycle)."""
    import aiosqlite
    watcher = FileWatcher(test_db, interval_minutes=5)
    await watcher._backfill_iso_languages_v075()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("iso_lang_backfilled_v075",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["value"] == "true"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_iso_lang_backfill_v075_selector_skips_dvd_iso_and_folder_bd(test_db):
    """Selector must skip DVD ISO rows AND folder-BD rows even when they
    have all-und audio_tracks. Only BD ISOs are in scope."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        # DVD ISO row — should be skipped (disc_type='dvd')
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "/media/movies/Skin (2011)/disc.iso",
                "Skin (2011)",
                "dvd",
                '[{"language":"und"}]',
            ),
        )
        # Folder-BD row — should be skipped (no .iso suffix)
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "/media/movies/Elephant (2003)/BDMV/index.bdmv",
                "Elephant (2003)",
                "bdmv",
                '[{"language":"und"}]',
            ),
        )
        # Non-disc row — should be skipped (disc_type IS NULL)
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "/media/movies/Plain (2020)/movie.mkv",
                "Plain (2020)",
                None,
                '[{"language":"und"}]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)

    # Mock probe_file to detect if anything reached the probe stage.
    # None of the seeded rows should reach it — all are excluded by SQL.
    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as mock_probe:
        await watcher._backfill_iso_languages_v075()
        assert mock_probe.call_count == 0


@pytest.mark.asyncio
async def test_iso_lang_backfill_v075_skips_partial_coverage(test_db):
    """A BD ISO row with [eng, und] gets pulled by the SQL LIKE prefilter
    but must be filtered out in Python — only fully-und (or empty) rows
    are in scope."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "/media/movies/Mixed (2010)/disc.iso",
                "Mixed (2010)",
                "bdmv",
                '[{"language":"eng"},{"language":"und"}]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)

    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as mock_probe:
        await watcher._backfill_iso_languages_v075()
        # SQL pulls the row (LIKE '%und%' matches), Python rejects it.
        assert mock_probe.call_count == 0


@pytest.mark.asyncio
async def test_iso_lang_backfill_v075_updates_stale_bd_iso_row(test_db, tmp_path):
    """End-to-end: an all-und BD ISO row gets re-probed and UPDATE'd
    with the libbluray-derived language metadata."""
    import aiosqlite
    import json
    from unittest.mock import AsyncMock, patch

    # The ISO path must exist on disk — the backfill skips non-existent
    # files (stale-row removal handles those separately).
    iso_path = tmp_path / "Elephant (2003)" / "disc.iso"
    iso_path.parent.mkdir(parents=True)
    iso_path.touch()
    fp = str(iso_path)

    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_name, disc_type, audio_tracks_json, subtitle_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                fp,
                "Elephant (2003)",
                "bdmv",
                '[{"language":"und","codec":"truehd","stream_index":1,"channels":6}]',
                '[]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    # Stub probe_file to return what v0.7.4's libbluray path would return
    # for Elephant.
    fake_probe = {
        "audio_tracks": [
            {"language": "fre", "codec": "truehd", "stream_index": 1, "channels": 6},
            {"language": "eng", "codec": "ac3",    "stream_index": 2, "channels": 2},
        ],
        "subtitle_tracks": [
            {"language": "fre", "codec": "hdmv_pgs_subtitle", "stream_index": 3},
            {"language": "fre", "codec": "hdmv_pgs_subtitle", "stream_index": 4},
        ],
    }

    watcher = FileWatcher(test_db, interval_minutes=5)

    with patch(
        "backend.scanner.probe_file",
        new=AsyncMock(return_value=fake_probe),
    ):
        await watcher._backfill_iso_languages_v075()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT audio_tracks_json, subtitle_tracks_json, native_language "
            "FROM scan_results WHERE file_path = ?",
            (fp,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    assert row is not None
    audio = json.loads(row["audio_tracks_json"])
    langs = [t["language"] for t in audio]
    assert langs == ["fre", "eng"], f"audio langs = {langs!r}"
    subs = json.loads(row["subtitle_tracks_json"])
    sub_langs = [t["language"] for t in subs]
    assert sub_langs == ["fre", "fre"], f"subtitle langs = {sub_langs!r}"
