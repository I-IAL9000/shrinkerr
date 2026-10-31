"""Files that came out larger (v0.10.0): a note, not an ignore.

A conversion that saved nothing used to ignore its file like an ignore by
hand: the file left the Scanner's lists, Add to Queue only cleaned it up,
and converting it again with another quality meant overriding every rule.
Now the queue's own marks — no savings, larger, video kept — are notes:
automatic queueing (new downloads, imports, policies) still skips the file,
an explicit Add to Queue converts it (its estimate says it came out larger
last time), and the Scanner shows it under Came out larger, not Ignored.
"""
import json

import aiosqlite
import pytest
import pytest_asyncio

A, B, C, D, E = (f"/m/{n} (2020)/{n}.mkv" for n in "ABCDE")
FILES = [A, B, C, D, E]
# A: ignored by hand. B, C, D: the queue's marks. E: never tried.
MARKS = {A: ("manual", None), B: ("no_savings", {"cq": 25, "encoder": "nvenc"}),
         C: ("conversion_larger", {"cq": 14, "encoder": "nvenc", "pct": 11}),
         D: ("video_kept", {"error_key": "errors.vmafTargetLarger"})}


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path in FILES:
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, video_bit_depth, "
                "audio_tracks_json, subtitle_tracks_json, duration, scan_timestamp) "
                "VALUES (?, 1000000000, 1, 'h264', 8, '[]', '[]', 3000, 't')", (path,))
        for path, (reason, details) in MARKS.items():
            await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at, details) VALUES (?, ?, ?, ?)",
                             (path, reason, "2026-10-30T20:00:00+00:00", json.dumps(details) if details else None))
        await db.commit()
    return test_db


async def _context(db_path):
    from backend.routes.scan import ignore_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return await ignore_context(db)


@pytest.mark.asyncio
async def test_the_queues_marks_are_not_ignores(library):
    ctx = await _context(library)
    assert ctx["ignored_paths"] == {A}
    assert set(ctx["larger_paths"]) == {B, C, D}
    assert ctx["larger_paths"][C] == {"reason": "conversion_larger", "at": "2026-10-30T20:00:00+00:00",
                                      "details": {"cq": 14, "encoder": "nvenc", "pct": 11}}


async def _types(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type FROM jobs") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_add_to_queue_converts_them_and_keeps_yours_ignored(library):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=FILES))
    assert await _types(library) == {B: "convert", C: "convert", D: "convert", E: "convert"}


@pytest.mark.asyncio
async def test_automatic_queueing_still_skips_them(library):
    """New downloads and imports (new_files) and policies (automatic) don't
    convert a file again that came out larger — no loop of failed encodes."""
    from backend.routes.jobs import BulkQueueFromScanRequest, _jobs_from_scan, add_jobs_from_scan
    jobs, _ = await _jobs_from_scan(FILES, BulkQueueFromScanRequest(), new_files=True)
    assert {j["file_path"] for j in jobs if j["job_type"] == "convert"} == {E}
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=FILES, automatic=True))
    assert await _types(library) == {E: "convert"}


@pytest.mark.asyncio
async def test_the_estimate_says_they_came_out_larger_last_time(library):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    got = (await estimate_jobs(EstimateRequest(file_paths=FILES)))["came_out_larger"]
    assert got["files"] == 3
    assert got["qualities"] == [{"cq": 14, "encoder": "nvenc"}, {"cq": 25, "encoder": "nvenc"}]


@pytest.mark.asyncio
async def test_the_scanners_filters(library):
    """Needs conversion leaves them out (a season added from it doesn't
    re-encode them at the same quality); Came out larger lists all three."""
    from backend.scan_filters import FILTERS
    ctx = await _context(library)
    rows = [{"file_path": p, "needs_conversion": 1, "file_size": 4 * 10 ** 9, "duration": 3000} for p in FILES]  # 10.7 Mbps
    assert [r["file_path"] for r in rows if FILTERS["needs_conversion"].py(dict(r), ctx)] == [E]
    async with aiosqlite.connect(library) as db:
        async with db.execute(f"SELECT file_path FROM scan_results WHERE {FILTERS['no_savings'].sql} "
                              "ORDER BY file_path") as cur:
            assert [r[0] for r in await cur.fetchall()] == [B, C, D]


@pytest.mark.asyncio
async def test_a_mark_refreshes_itself_and_never_replaces_yours(library):
    from backend.queue import mark_came_out_larger
    async with aiosqlite.connect(library) as db:
        await mark_came_out_larger(db, [A, B, E], "conversion_larger", {"cq": 22, "encoder": "nvenc", "pct": 3})
        await db.commit()
        async with db.execute("SELECT file_path, reason, details FROM ignored_files") as cur:
            rows = {r[0]: (r[1], json.loads(r[2]) if r[2] else None) for r in await cur.fetchall()}
    assert rows[A] == ("manual", None)  # yours stays
    assert rows[B] == ("conversion_larger", {"cq": 22, "encoder": "nvenc", "pct": 3})  # the latest try
    assert rows[E] == ("conversion_larger", {"cq": 22, "encoder": "nvenc", "pct": 3})
