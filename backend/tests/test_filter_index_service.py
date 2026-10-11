"""The filter index kept up to date: built in the background, its counts the
SQL engine's after any change, rebuilt for big changes and other databases,
saved and loaded."""
import os

import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.scan_filters import parse_filter
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    service.reset()
    yield test_db
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
async def test_time_based_filters_are_evaluated_again_every_few_minutes(library):
    await _built()
    right = service._state.index.bits["new"]
    service._state.index.bits["new"] = 0  # stale
    await service.refresh()
    assert service._state.index.bits["new"] == 0  # not yet time
    service._state.index.time_at -= service.TIME_REFRESH
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
    await service.wait_rebuilt()
    assert await service.counts(parse_filter("x264")) == await library_data.sql_counts(library, "x264")


@pytest.mark.asyncio
async def test_another_database_rebuilds(library):
    await _built()
    async with aiosqlite.connect(library) as db:  # what restoring a backup looks like
        await db.execute("UPDATE settings SET value = 'another' WHERE key = 'filter_index_db_id'")
        await db.commit()
    await service.refresh()
    await service.wait_rebuilt()
    assert service._state.index.db_id == "another"


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
async def test_an_index_for_other_filter_definitions_isnt_loaded(library, monkeypatch):
    await _built()
    await service.save(force=True)
    service.reset()
    monkeypatch.setattr(service, "definitions_version", lambda: "something else")
    assert not await service.load()
