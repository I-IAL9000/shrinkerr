"""The filter index kept up to date: built in the background, its counts the
SQL engine's after any change, rebuilt for big changes, restored backups and
other databases, saved and loaded; a failing build tried again after a
pause, the SQL engine answering meanwhile and on errors."""
import asyncio
import os
import sqlite3
from contextlib import closing

import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.scan_filters import encode_advanced, parse_filter
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    service.reset()
    yield test_db
    if service._state.rebuild is not None:
        # Let a rebuild a failed test left running finish: cancelled while it
        # opens its connection, aiosqlite's thread would keep the process from exiting.
        await asyncio.wait([service._state.rebuild], timeout=10)
    service.reset()


async def _built():
    assert await service.counts(parse_filter("x264")) is None  # nothing yet: starts the build
    await service.wait_rebuilt()


async def _assert_counts_match(db_path):
    for text in ["all", *library_data.EXPRESSIONS]:
        assert await service.counts(parse_filter(text)) == await library_data.sql_counts(db_path, text), text
    counts, savings = await service.stats()
    assert counts == await library_data.sql_counts(db_path)
    assert savings == await library_data.sql_savings(db_path)


def _copy(src: str, dst: str) -> None:
    """The whole database, as a backup or a restore copies it."""
    with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)


async def _failing_build():
    raise RuntimeError("disk full")


@pytest.mark.asyncio
async def test_counts_once_built(library):
    await _built()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_counts_follow_changes(library):
    await _built()
    await library_data.mutate(library)
    await _assert_counts_match(library)
    assert service._state.index.bits == (await service.build()).bits  # what a rebuild gives


@pytest.mark.asyncio
async def test_advanced_search_counts_follow_changes(library):
    await _built()
    advanced = [text for text in library_data.EXPRESSIONS if text.startswith("adv:")]
    advanced.append(encode_advanced([{"property": "video_codec", "op": "eq", "value": "hevc"}]))  # mutate() edits it
    for text in advanced:
        await service.counts(parse_filter(text))  # their conditions' rows kept
    await library_data.mutate(library)
    for text in advanced:
        assert await service.counts(parse_filter(text)) == await library_data.sql_counts(library, text), text


@pytest.mark.asyncio
async def test_time_based_filters_are_evaluated_again_every_few_minutes(library):
    await _built()
    right = service._state.index.bits["new"]
    service._state.index.bits["new"] = 0  # stale
    await service.refresh()
    assert service._state.index.bits["new"] == 0  # not yet time
    service._state.index.time_at -= service.TIME_REFRESH
    await service.refresh()
    assert service._state.index.bits["new"] == right
    service._state.index.bits["new"] = 0
    service._state.index.time_at += 2 * service.TIME_REFRESH  # the clock went back
    await service.refresh()
    assert service._state.index.bits["new"] == right


@pytest.mark.asyncio
async def test_a_big_change_rebuilds_in_the_background(library, monkeypatch):
    await _built()
    before = await service.counts(parse_filter("x264"))
    monkeypatch.setattr(service, "BIG_CHANGE", 5)
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET video_codec = 'hevc' WHERE video_codec = 'h264'")
        await db.commit()
    assert await service.counts(parse_filter("x264")) == before  # the old index answers meanwhile
    await service.save(force=True)
    assert not os.path.exists(service.index_path())  # not saved while it's being replaced
    await service.wait_rebuilt()
    assert await service.counts(parse_filter("x264")) == await library_data.sql_counts(library, "x264")


@pytest.mark.asyncio
async def test_another_database_rebuilds(library):
    await _built()
    async with aiosqlite.connect(library) as db:  # another install's database (a backup of this one keeps the id)
        await db.execute("UPDATE settings SET value = 'another' WHERE key = 'filter_index_db_id'")
        await db.commit()
    await service.refresh()
    await service.wait_rebuilt()
    assert service._state.index.db_id == "another"


@pytest.mark.asyncio
async def test_restoring_a_backup_rebuilds(library):
    await _built()
    _copy(library, library + ".backup")
    async with aiosqlite.connect(library) as db:  # rows only: the input counters stay as they were
        await db.execute("UPDATE scan_results SET video_codec = 'hevc' WHERE id IN (SELECT id FROM scan_results "
                         "WHERE video_codec = 'h264' AND removed_from_list = 0 LIMIT 3)")
        await db.commit()
    await service.refresh()
    _copy(library + ".backup", library)  # the backup put back in place
    await service.refresh()
    await service.wait_rebuilt()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_saved_then_loaded_after_a_restart(library):
    await _built()
    await service.save(force=True)
    assert os.path.exists(service.index_path())
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT COUNT(*) FROM filter_index_changes WHERE seq <= ?",
                              (service._state.index.seq,)) as cur:
            assert (await cur.fetchone())[0] == 0  # what the file holds is dropped from the log
    await library_data.mutate(library)  # changes after the save, while "stopped"
    service.reset()
    assert await service.load()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_saving_keeps_the_changes_after_the_saved_position(library):
    await _built()
    await library_data.mutate(library)  # not applied yet
    await service.save(force=True)
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT COUNT(*) FROM filter_index_changes WHERE seq > ?",
                              (service._state.index.seq,)) as cur:
            assert (await cur.fetchone())[0] > 0
    service.reset()
    assert await service.load()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_an_index_for_other_filter_definitions_isnt_loaded(library, monkeypatch):
    await _built()
    await service.save(force=True)
    service.reset()
    monkeypatch.setattr(service, "definitions_version", lambda: "something else")
    assert not await service.load()


@pytest.mark.asyncio
async def test_a_failing_build_is_tried_again_after_a_pause(library, monkeypatch, capsys):
    build, tries = service.build, []

    async def failing():
        tries.append(1)
        await _failing_build()
    monkeypatch.setattr(service, "build", failing)
    for _ in range(3):  # the SQL engine answers; one try within the pause
        assert await service.counts(parse_filter("x264")) is None
        assert await service.stats() is None
        await service.wait_rebuilt()
    assert len(tries) == 1
    service._state.failed_at -= service._state.backoff  # the pause is over
    assert await service.counts(parse_filter("x264")) is None
    await service.wait_rebuilt()
    assert len(tries) == 2 and service._state.backoff == 2 * service.BACKOFF_FIRST
    assert capsys.readouterr().out.count("Traceback") == 1  # once per run of failures
    monkeypatch.setattr(service, "build", build)
    service._state.failed_at -= service._state.backoff
    await _built()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_after_a_failed_rebuild_the_sql_engine_answers(library, monkeypatch):
    await _built()
    build = service.build
    monkeypatch.setattr(service, "build", _failing_build)
    monkeypatch.setattr(service, "BIG_CHANGE", 5)
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET video_codec = 'hevc' WHERE video_codec = 'h264'")
        await db.commit()
    await service.refresh()  # a rebuild, which fails
    await service.wait_rebuilt()
    assert await service.counts(parse_filter("x264")) is None  # not the old index's
    monkeypatch.setattr(service, "build", build)
    service._state.failed_at -= service._state.backoff
    assert await service.counts(parse_filter("x264")) is None  # tried again, still not the old index's
    await service.wait_rebuilt()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_an_error_falls_back_to_the_sql_engine(library, capsys):
    await _built()
    async with aiosqlite.connect(library) as db:  # refresh can't read the counters
        await db.execute("DROP TABLE change_counters")
        await db.commit()
    capsys.readouterr()
    assert await service.counts(parse_filter("x264")) is None
    assert await service.stats() is None
    assert capsys.readouterr().out.count("[FILTERS]") == 1  # logged at most once a minute


@pytest.mark.asyncio
async def test_no_build_starts_while_the_saved_index_loads(library):
    service._state.loading = True
    assert await service.current() is None
    assert service._state.rebuild is None


@pytest.mark.asyncio
async def test_stopping_the_loop_stops_a_build(library, monkeypatch):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.Event().wait()  # never ends
    monkeypatch.setattr(service, "build", slow)
    loop = asyncio.create_task(service.filter_index_loop())
    await started.wait()
    loop.cancel()
    await asyncio.wait([loop], timeout=5)
    assert loop.cancelled() and service._state.rebuild.cancelled()
