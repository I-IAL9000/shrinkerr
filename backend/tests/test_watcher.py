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
# Locks the contract for `_backfill_iso_languages_v076` — the one-shot
# startup sweep that re-probes existing BD ISO rows whose audio_tracks
# are all-und (pre-v0.7.4 libbluray-ctypes path) so they pick up real
# language codes without manual delete-and-rediscover.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iso_lang_backfill_v076_idempotent_when_flag_set(test_db):
    """When iso_lang_backfilled_v076 = 'true' already, the method returns
    immediately without touching scan_results."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES "
            "('iso_lang_backfilled_v076', 'true')"
        )
        # Seed a row that WOULD be a backfill candidate, to confirm we
        # don't touch it when the flag is set.
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/media/movies/Stale (1999)/disc.iso",
                0,
                "2026-05-31T00:00:00Z",
                "bdmv",
                '[{"language":"und"},{"language":"und"}]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    await watcher._backfill_iso_languages_v076()

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
async def test_iso_lang_backfill_v076_sets_flag_when_no_candidates(test_db):
    """Empty result set still sets the flag (so a clean install doesn't
    re-query scan_results on every watcher cycle)."""
    import aiosqlite
    watcher = FileWatcher(test_db, interval_minutes=5)
    await watcher._backfill_iso_languages_v076()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("iso_lang_backfilled_v076",),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["value"] == "true"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_iso_lang_backfill_v076_selector_skips_dvd_iso_and_folder_bd(test_db):
    """Selector must skip DVD ISO rows AND folder-BD rows even when they
    have all-und audio_tracks. Only BD ISOs are in scope."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        # DVD ISO row — should be skipped (disc_type='dvd')
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/media/movies/Skin (2011)/disc.iso",
                0,
                "2026-05-31T00:00:00Z",
                "dvd",
                '[{"language":"und"}]',
            ),
        )
        # Folder-BD row — should be skipped (no .iso suffix)
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/media/movies/Elephant (2003)/BDMV/index.bdmv",
                0,
                "2026-05-31T00:00:00Z",
                "bdmv",
                '[{"language":"und"}]',
            ),
        )
        # Non-disc row — should be skipped (disc_type IS NULL)
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/media/movies/Plain (2020)/movie.mkv",
                0,
                "2026-05-31T00:00:00Z",
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
        await watcher._backfill_iso_languages_v076()
        assert mock_probe.call_count == 0


@pytest.mark.asyncio
async def test_iso_lang_backfill_v076_skips_partial_coverage(test_db):
    """A BD ISO row with [eng, und] gets pulled by the SQL LIKE prefilter
    but must be filtered out in Python — only fully-und (or empty) rows
    are in scope."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "/media/movies/Mixed (2010)/disc.iso",
                0,
                "2026-05-31T00:00:00Z",
                "bdmv",
                '[{"language":"eng"},{"language":"und"}]',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)

    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as mock_probe:
        await watcher._backfill_iso_languages_v076()
        # SQL pulls the row (LIKE '%und%' matches), Python rejects it.
        assert mock_probe.call_count == 0


@pytest.mark.asyncio
async def test_iso_lang_backfill_v076_updates_stale_bd_iso_row(test_db, tmp_path):
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
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json, subtitle_tracks_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                fp,
                0,
                "2026-05-31T00:00:00Z",
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
        await watcher._backfill_iso_languages_v076()

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


@pytest.mark.asyncio
async def test_iso_lang_backfill_v076_matches_production_json_dumps_format(test_db, tmp_path):
    """Regression test for the v0.7.5 selector bug.

    v0.7.5 used `LIKE '%"language":"und"%'` (no spaces) to filter at the
    SQL stage. But `json.dumps()` defaults to `(', ', ': ')` separators,
    so production rows store `"language": "und"` WITH a space. The
    selector silently matched zero rows on real installs and set its
    idempotency flag — backfill appeared to run but updated nothing.

    v0.7.6 drops the JSON LIKE clause entirely and does all language-
    shape filtering in Python where the parse is correct regardless of
    separator style. This test seeds a row using the EXACT serializer
    the production code uses (json.dumps on a list of dicts) and asserts
    the backfill picks it up.
    """
    import aiosqlite
    import json
    from unittest.mock import AsyncMock, patch

    iso_path = tmp_path / "Realistic (2025)" / "disc.iso"
    iso_path.parent.mkdir(parents=True)
    iso_path.touch()
    fp = str(iso_path)

    # Build the JSON the way production does — through json.dumps on dicts.
    # Default separators are (', ', ': '), so this string contains
    # `"language": "und"` with a space after the colon.
    stored = json.dumps([
        {"stream_index": 1, "language": "und", "codec": "truehd"},
        {"stream_index": 2, "language": "und", "codec": "ac3"},
    ])
    # Defensive sanity-check: confirm the fixture actually has the
    # space-separator shape we're trying to catch. If this assert ever
    # fails, json.dumps defaults changed and the test isn't testing
    # what it claims.
    assert '"language": "und"' in stored, \
        f"fixture lost the space-after-colon shape: {stored!r}"

    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json, subtitle_tracks_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fp, 0, "2026-05-31T00:00:00Z", "bdmv", stored, "[]"),
        )
        await db.commit()
    finally:
        await db.close()

    fake_probe = {
        "audio_tracks": [
            {"language": "jpn", "codec": "truehd", "stream_index": 1, "channels": 6},
        ],
        "subtitle_tracks": [],
    }
    watcher = FileWatcher(test_db, interval_minutes=5)

    with patch("backend.scanner.probe_file", new=AsyncMock(return_value=fake_probe)):
        await watcher._backfill_iso_languages_v076()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT audio_tracks_json FROM scan_results WHERE file_path = ?",
            (fp,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    assert row is not None
    audio = json.loads(row["audio_tracks_json"])
    langs = [t["language"] for t in audio]
    assert langs == ["jpn"], (
        f"audio langs = {langs!r} — backfill failed to match the realistic "
        f"json.dumps-shaped row (the v0.7.5 selector regression)"
    )


# ----------------------------------------------------------------------------
# v0.7.7: stale-row removal scoping tests.
#
# Locks the contract that the watcher's stale-row removal only deletes rows
# whose file_path is under a media_dir that exists on disk THIS CYCLE. If
# a configured media_dir is missing (e.g. volume not mounted during a
# partial-volume RC test), rows under it must be preserved — they'll be
# re-discovered when the mount comes back.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_removal_v077_preserves_rows_under_unmounted_dir(test_db, tmp_path):
    """When one of the configured media_dirs is missing from disk this
    cycle, rows under it must NOT be flagged stale or deleted."""
    import aiosqlite

    # Set up two media_dirs. One exists on disk; the other doesn't (sim
    # an unmounted volume). Seed scan_results rows under each.
    mounted_dir = tmp_path / "MountedVol"
    mounted_dir.mkdir()
    unmounted_dir = tmp_path / "UnmountedVol"  # Note: NOT created on disk

    mounted_path  = str(mounted_dir)
    unmounted_path = str(unmounted_dir)

    # Put one real video file under the mounted dir so the walk finds
    # something — defends the test from the >50%-stale sanity belt
    # firing.
    real_video = mounted_dir / "movie.mkv"
    real_video.touch()

    db = await aiosqlite.connect(test_db)
    try:
        # Register both as auto_scan media_dirs
        await db.execute(
            "INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1), (?, 1)",
            (mounted_path, unmounted_path),
        )
        # Row under the mounted dir — matches the real file on disk, so
        # it's NOT stale.
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
            (str(real_video), 0, "2026-05-31T00:00:00Z"),
        )
        # Two rows under the unmounted dir — these would historically
        # have been deleted because their dir doesn't exist on disk this
        # cycle. v0.7.7 preserves them.
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
            (f"{unmounted_path}/movie_a.mkv", 0, "2026-05-31T00:00:00Z"),
        )
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
            (f"{unmounted_path}/movie_b.mkv", 0, "2026-05-31T00:00:00Z"),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    # Stub the probe path so the new-files branch is a no-op (we're not
    # testing new-file probing here, only stale removal).
    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await watcher.check_once()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT file_path FROM scan_results ORDER BY file_path"
        ) as cur:
            surviving = [r["file_path"] for r in await cur.fetchall()]
    finally:
        await db.close()

    # All three rows must still be there. Pre-v0.7.7 the two unmounted-
    # dir rows would have been hard-DELETEd.
    assert str(real_video) in surviving, \
        f"mounted-dir row missing: {surviving}"
    assert f"{unmounted_path}/movie_a.mkv" in surviving, \
        f"v0.7.7 regression: unmounted-dir row was deleted ({surviving})"
    assert f"{unmounted_path}/movie_b.mkv" in surviving, \
        f"v0.7.7 regression: unmounted-dir row was deleted ({surviving})"


@pytest.mark.asyncio
async def test_stale_removal_v0722_per_subfolder_belt_preserves_unmounted_subvolume(test_db, tmp_path):
    """v0.7.22 per-subfolder sanity belt — when a Synology-style nested
    mount under a walked media_dir hasn't come up yet, every row under
    that specific subfolder gets flagged stale (because the walker
    finds the other subfolders but not this one). The global >50%
    belt misses this when the unmounted subfolder is <50% of the
    library. v0.7.22 protects each subfolder individually: any
    subfolder losing >50% of its known rows in one cycle is preserved.
    """
    import aiosqlite

    # Single media_dir with two subfolders. Both are physically present
    # at scan time, but `TV1/` is empty (simulates a delayed mount).
    media_dir = tmp_path / "TVShare"
    media_dir.mkdir()
    tv1 = media_dir / "TV1"
    tv1.mkdir()
    # TV1 is empty — no files inside
    tv2 = media_dir / "TV2"
    tv2.mkdir()
    # TV2 has one real file on disk so the global belt sees plenty of healthy rows
    tv2_real = tv2 / "show.mkv"
    tv2_real.touch()

    media_path = str(media_dir)

    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)",
            (media_path,),
        )
        # TV2 row matching the real file → won't be stale.
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
            (str(tv2_real), 0, "2026-05-31T00:00:00Z"),
        )
        # 9 TV2 rows that DO match files (10 total, 1 is real one above).
        # Seed extras so the global library size is comfortably big.
        for i in range(9):
            f = tv2 / f"show_{i}.mkv"
            f.touch()
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
                (str(f), 0, "2026-05-31T00:00:00Z"),
            )
        # 3 TV1 rows that DON'T match files (nothing in TV1 on disk).
        # Pre-v0.7.22 these would all be deleted (3 stale out of 13 total
        # = ~23% global, doesn't trip the global belt). Per-subfolder
        # belt should fire — TV1 alone is 100% stale (3/3).
        for i in range(3):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
                (f"{tv1}/missing_{i}.mkv", 0, "2026-05-31T00:00:00Z"),
            )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await watcher.check_once()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM scan_results WHERE file_path LIKE ?",
            (f"{tv1}/%",),
        ) as cur:
            tv1_remaining = (await cur.fetchone())["n"]
    finally:
        await db.close()

    assert tv1_remaining == 3, (
        f"TV1 rows = {tv1_remaining}, expected 3. Per-subfolder belt didn't "
        f"fire — the v0.7.22 partial-mount-protection regression hit."
    )


@pytest.mark.asyncio
async def test_stale_removal_v077_sanity_belt_aborts_on_majority_stale(test_db, tmp_path):
    """If a single cycle would flag >50% of walked-dir rows as stale
    (e.g. unreadable mount, NFS hiccup), abort stale-removal entirely
    for this cycle. Belt-and-suspenders against bug classes we haven't
    thought of yet."""
    import aiosqlite

    # Single media_dir that exists on disk but is empty (zero video
    # files). Seed scan_results with 10 rows claiming to live under it.
    # The walk yields zero files → all 10 would be flagged stale → 100%
    # of walked-dir rows → sanity belt fires.
    mounted_dir = tmp_path / "WeirdVol"
    mounted_dir.mkdir()
    mounted_path = str(mounted_dir)

    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)",
            (mounted_path,),
        )
        for i in range(10):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, ?, ?)",
                (f"{mounted_path}/movie_{i}.mkv", 0, "2026-05-31T00:00:00Z"),
            )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await watcher.check_once()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT COUNT(*) AS n FROM scan_results") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    # Sanity belt should have preserved all 10 rows. Pre-v0.7.7 (or
    # without the belt) all 10 would be gone.
    assert row["n"] == 10, \
        f"sanity belt didn't fire: {row['n']} rows remain (expected 10)"


# ----------------------------------------------------------------------------
# v0.7.8: folder-disc language backfill + extended stale-corrupt helper.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disc_lang_backfill_v078_updates_folder_disc_and_clears_corrupt_flags(
    test_db, tmp_path,
):
    """End-to-end: an all-und folder-BDMV row with stuck health_status +
    probe_status gets re-probed AND has all three corrupt markers cleared
    in the same sweep."""
    import aiosqlite
    import json
    from unittest.mock import AsyncMock, patch

    # Folder-disc marker file (not an ISO suffix — that's what scopes
    # v078 vs v076).
    bdmv_dir = tmp_path / "Folder Disc (2020)" / "BDMV"
    bdmv_dir.mkdir(parents=True)
    marker = bdmv_dir / "index.bdmv"
    marker.touch()
    fp = str(marker)

    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json, "
            " subtitle_tracks_json, health_status, probe_status, "
            " health_errors_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fp,
                0,
                "2026-05-31T00:00:00Z",
                "bdmv",
                # Use the production json.dumps shape (space after colon).
                json.dumps([{"language": "und", "codec": "truehd", "stream_index": 1}]),
                "[]",
                "corrupt",
                "corrupt",
                '{"error": "stale"}',
            ),
        )
        await db.commit()
    finally:
        await db.close()

    fake_probe = {
        "audio_tracks": [
            {"language": "deu", "codec": "truehd", "stream_index": 1, "channels": 6},
        ],
        "subtitle_tracks": [],
    }
    watcher = FileWatcher(test_db, interval_minutes=5)

    with patch("backend.scanner.probe_file", new=AsyncMock(return_value=fake_probe)):
        await watcher._backfill_disc_languages_v078()

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT audio_tracks_json, health_status, probe_status, "
            "health_errors_json FROM scan_results WHERE file_path = ?",
            (fp,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    # Languages updated
    langs = [t["language"] for t in json.loads(row["audio_tracks_json"])]
    assert langs == ["deu"], f"audio langs = {langs!r}"
    # All three corrupt markers cleared (Fix A — extended helper)
    assert row["health_status"] is None, \
        f"health_status not cleared: {row['health_status']!r}"
    assert row["probe_status"] == "ok", \
        f"probe_status not cleared: {row['probe_status']!r}"
    assert row["health_errors_json"] is None, \
        f"health_errors_json not cleared: {row['health_errors_json']!r}"


@pytest.mark.asyncio
async def test_disc_lang_backfill_v078_skips_bd_iso_rows(test_db, tmp_path):
    """BD ISO rows are out of scope for v078 (already handled by v076).
    Confirm a BD ISO row with all-und audio is NOT touched by v078."""
    import aiosqlite
    import json
    from unittest.mock import AsyncMock, patch

    iso_path = tmp_path / "BD ISO (2020)" / "disc.iso"
    iso_path.parent.mkdir(parents=True)
    iso_path.touch()
    fp = str(iso_path)

    stored = json.dumps([{"language": "und", "codec": "ac3", "stream_index": 1}])
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, audio_tracks_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (fp, 0, "2026-05-31T00:00:00Z", "bdmv", stored),
        )
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as mock_probe:
        await watcher._backfill_disc_languages_v078()
        # SQL excludes the .iso row → probe never called
        assert mock_probe.call_count == 0


@pytest.mark.asyncio
async def test_clear_stale_disc_helper_v078_clears_all_three_flags(test_db):
    """The extended `_clear_stale_disc_health_status` helper resets
    health_status, probe_status, AND health_errors_json in one call —
    not just health_status as in v0.7.2-7."""
    import aiosqlite
    from backend.scanner import _clear_stale_disc_health_status

    fp = "/media/test/disc.iso"
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT INTO scan_results "
            "(file_path, file_size, scan_timestamp, disc_type, health_status, probe_status, "
            " health_errors_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fp, 0, "2026-05-31T00:00:00Z", "bdmv", "corrupt", "corrupt", '{"e": "x"}'),
        )
        await db.commit()
    finally:
        await db.close()

    await _clear_stale_disc_health_status(test_db, fp)

    db = await aiosqlite.connect(test_db)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT health_status, probe_status, health_errors_json "
            "FROM scan_results WHERE file_path = ?",
            (fp,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    assert row["health_status"] is None
    assert row["probe_status"] == "ok"
    assert row["health_errors_json"] is None


