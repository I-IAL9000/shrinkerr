"""Settings → Rules → "Test a file" (v0.10.0): which rule applies to a file,
and for every rule which of its conditions matched."""
import json

import aiosqlite
import pytest

FILE = "/media/Movies/Film (2020)/Film.2020.1080p.BluRay.x264-GRP.mkv"


async def _rule(db, name, priority, mode, conditions, action="skip"):
    await db.execute(
        "INSERT INTO encoding_rules (name, match_conditions, priority, action, enabled, created_at) "
        "VALUES (?, ?, ?, ?, 1, '2026-10-10')",
        (name, json.dumps({"match_mode": mode, "conditions": conditions}), priority, action))


@pytest.mark.asyncio
async def test_every_rule_is_explained(test_db):
    from backend.routes.rules import test_rule
    async with aiosqlite.connect(test_db) as db:
        await _rule(db, "4K only", 0, "all", [{"type": "resolution", "operator": "is", "value": "4K"},
                                               {"type": "source", "operator": "is", "value": "Blu-ray"}])
        await _rule(db, "Blu-rays", 1, "any", [{"type": "source", "operator": "is", "value": "Blu-ray"}], "encode")
        await _rule(db, "Also Blu-rays", 2, "any", [{"type": "source", "operator": "is", "value": "Blu-ray"}])
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_height, video_width, scan_timestamp) "
                         "VALUES (?, 1, 1080, 1920, '2026-10-10')", (FILE,))
        await db.commit()
    got = await test_rule({"file_path": f'  "{FILE}" '})  # pasted with quotes
    assert got["file_path"] == FILE and got["scanned"] is True
    assert (got["matched_rule"]["rule_name"], got["matched_rule"]["action"]) == ("Blu-rays", "encode")
    assert [(r["rule_name"], r["matched"], [c["matched"] for c in r["conditions"]]) for r in got["rules"]] == [
        ("4K only", False, [False, True]), ("Blu-rays", True, [True]), ("Also Blu-rays", True, [True])]


@pytest.mark.asyncio
async def test_a_file_that_was_never_scanned(test_db):
    from backend.routes.rules import test_rule
    async with aiosqlite.connect(test_db) as db:
        await _rule(db, "Big", 0, "any", [{"type": "file_size", "operator": "greater_than", "value": "1"}])
        await db.commit()
    got = await test_rule({"file_path": "/media/elsewhere.mkv"})
    assert (got["matched_rule"], got["scanned"]) == (None, False)


@pytest.mark.asyncio
async def test_no_rules(test_db):
    from backend.routes.rules import test_rule
    assert (await test_rule({"file_path": FILE}))["rules"] == []
