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
