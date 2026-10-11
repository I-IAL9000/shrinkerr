"""Evaluating the filter definitions over every listed row, or some ids."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index.evaluate import evaluate
from backend.scan_filters import _FILTER_LIST
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    return test_db


async def _evaluate(db_path, filters=_FILTER_LIST, ids=None):
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db)
        return await evaluate(db, filters, ctx, ids=ids)


@pytest.mark.asyncio
async def test_every_filter_matches_something_in_the_test_library(library):
    counts = await library_data.sql_counts(library)
    assert [fid for fid, n in counts.items() if not n] == []


@pytest.mark.asyncio
async def test_every_listed_row_against_every_filter(library):
    alive, members, savings = await _evaluate(library)
    counts = await library_data.sql_counts(library)
    assert len(alive) == counts["all"]
    assert {fid: len(rows) for fid, rows in members.items()} == {f.id: counts[f.id] for f in _FILTER_LIST}
    assert sum(savings.get(i, 0) for i in members["needs_conversion"]) == await library_data.sql_savings(library)


@pytest.mark.asyncio
async def test_some_rows_give_their_part_of_the_whole(library):
    alive, members, _ = await _evaluate(library)
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT id FROM scan_results WHERE removed_from_list = 1") as cur:
            hidden = [r[0] for r in await cur.fetchall()]
    some = alive[::7] + hidden + [10 ** 6]  # listed, hidden, and an id that doesn't exist
    part_alive, part_members, part_savings = await _evaluate(library, ids=some)
    wanted = set(some)
    assert sorted(part_alive) == sorted(i for i in alive if i in wanted)
    for fid, rows in members.items():
        assert sorted(part_members[fid]) == [i for i in sorted(rows) if i in wanted], fid
    assert set(part_savings) <= wanted


@pytest.mark.asyncio
async def test_only_sql_filters_skip_the_python_pass(library):
    sql_only = [f for f in _FILTER_LIST if f.sql][:5]
    _, members, savings = await _evaluate(library, filters=sql_only)
    assert set(members) == {f.id for f in sql_only}
    assert savings is None
