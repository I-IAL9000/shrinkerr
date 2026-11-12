"""Ignore on the Scanner's Select all (v0.10.0): the bar had no paths to
send and did nothing. The page asks the server for the filtered list's
files and ignores them in one request: 13,477 files needing conversion
were as many requests one at a time."""
import aiosqlite
import pytest

from backend.tests.test_vmaf_target_rules import library  # noqa: F401  (a fixture)


async def _state(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, reason FROM ignored_files") as cur:
            ignored = dict(await cur.fetchall())
        async with db.execute("SELECT file_path FROM jobs WHERE status = 'pending' ORDER BY file_path") as cur:
            pending = [r[0] for r in await cur.fetchall()]
        async with db.execute("SELECT file_path FROM file_events WHERE event_type = 'ignored' ORDER BY file_path") as cur:
            logged = [r[0] for r in await cur.fetchall()]
    return ignored, pending, logged


@pytest.mark.asyncio
async def test_many_paths_are_ignored_in_one_request(library):
    from backend.queue import JobQueue
    from backend.routes.settings import IgnoreFileRequest, IgnoreFilesRequest, ignore_file, ignore_files
    await JobQueue(library).add_jobs_bulk([{"file_path": p, "job_type": "convert"}
                                           for p in ("/m/a.mkv", "/m/Show/b.mkv", "/m/c.mkv", "/m/d.mkv")])

    await ignore_files(IgnoreFilesRequest(file_paths=["/m/a.mkv", "/m/Show/"]))
    ignored, pending, logged = await _state(library)
    assert ignored == {"/m/a.mkv": "manual", "/m/Show/": "manual"}  # a title's folder as before
    assert pending == ["/m/c.mkv", "/m/d.mkv"]  # their waiting jobs go
    assert logged == ["/m/Show/", "/m/a.mkv"]

    await ignore_file(IgnoreFileRequest(file_path="/m/c.mkv"))  # one at a time, as before
    assert (await _state(library))[1] == ["/m/d.mkv"]
