"""The index's counts are the SQL engine's (count_all / facet_counts), and
patching changed rows and inputs gives what rebuilding gives, the lists'
values included."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex, definitions_version
from backend.scan_filters import _FILTER_LIST, _FACET_LIST, parse_filter
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    service.reset()  # _adhoc_bits keeps rows: none from another test's database
    yield test_db
    service.reset()


async def _context(db):
    from backend.routes.scan import _build_enrichment_context
    return await _build_enrichment_context(db, titles=True)


async def index_of(db_path) -> FilterIndex:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        alive, members, savings = await evaluate(db, _FILTER_LIST, await _context(db), facets=_FACET_LIST)
    return FilterIndex.of(alive, members, savings, version=definitions_version(), db_id="test")


def _lists(index: FilterIndex) -> dict[str, int]:
    """The lists' values ("<facet>:<value>" -> rows)."""
    return {k: v for k, v in index.bits.items() if ":" in k}


async def _facets(index: FilterIndex, text: str) -> dict:
    """The index's counts under `text`, as the service takes them (a folder evaluated now)."""
    expr = parse_filter(text)
    groups, excluded = service._constraints(index, expr, await service._adhoc_for(expr, index))
    return index.facet_counts(groups, excluded)


async def _state(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COALESCE(MAX(seq), 0) FROM filter_index_changes") as cur:
            seq = (await cur.fetchone())[0]
        async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
            counters = dict(await cur.fetchall())
    return seq, counters


@pytest.mark.asyncio
async def test_the_counts_are_the_sql_engines(library):
    index = await index_of(library)
    assert index.count_all() == await library_data.sql_counts(library)
    assert index.savings_of("needs_conversion") == await library_data.sql_savings(library)
    assert _lists(index)
    for text in library_data.EXPRESSIONS:
        if "adv:" in text:
            continue  # Advanced Search conditions aren't in the index: the service evaluates them (Task 7)
        assert await _facets(index, text) == await library_data.sql_counts(library, text), text


@pytest.mark.asyncio
async def test_patching_rows_and_inputs_equals_rebuilding(library):
    """mutate() and a title's metadata changed, the service's refresh gives
    what a rebuild gives: the filters' rows and the lists' values."""
    service._state.index = index = await service.build()
    before = _lists(index)
    _, counters = await _state(library)
    await library_data.mutate(library)
    async with aiosqlite.connect(library) as db:  # a Title list's input
        await db.execute("UPDATE poster_cache SET genres = 'Drama, Western' WHERE genres = 'Comedy'")
        await db.commit()
    _, new_counters = await _state(library)
    moved = {name[3:] for name, n in new_counters.items() if counters[name] != n}
    assert moved == {"ignored", "jobs", "settings", "plex", "titles"}
    await service.refresh()
    assert service._state.index is index and not service._state.stale  # patched, not rebuilt
    fresh = await index_of(library)
    assert _lists(index) != before and "genre:western" in index.bits
    assert _lists(index) == _lists(fresh)
    assert index.alive == fresh.alive
    assert index.bits == fresh.bits
    assert index.savings == fresh.savings
    assert index.count_all() == await library_data.sql_counts(library)


@pytest.mark.asyncio
async def test_patching_rows_keeps_their_savings(library):
    """With no input moved, only patch() touches the savings: it drops a hidden
    row's, replaces an edited row's and adds a new row's."""
    index = await index_of(library)
    seq, _ = await _state(library)
    async with aiosqlite.connect(library) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM scan_results WHERE removed_from_list = 0 "
                              "AND video_conv_savings_bytes > 0 ORDER BY id LIMIT 2") as cur:
            hidden, edited = [r[0] for r in await cur.fetchall()]
        assert hidden in index.savings and index.savings[edited] != 123456789
        await db.execute("UPDATE scan_results SET removed_from_list = 1 WHERE id = ?", (hidden,))
        await db.execute("UPDATE scan_results SET video_conv_savings_bytes = 123456789 WHERE id = ?", (edited,))
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, duration, video_height, "
            "video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, new_detected_at, "
            "video_conv_savings_bytes) SELECT file_path || '.new.mkv', file_size, video_codec, needs_conversion, "
            "duration, video_height, video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, "
            "new_detected_at, 987654321 FROM scan_results WHERE id = ?", (edited,))
        await db.commit()
        async with db.execute("SELECT DISTINCT row_id FROM filter_index_changes WHERE seq > ?", (seq,)) as cur:
            changed = sorted(r[0] for r in await cur.fetchall())
        async with db.execute("SELECT MAX(id) FROM scan_results") as cur:
            added = (await cur.fetchone())[0]
        assert changed == sorted([hidden, edited, added])
        alive, members, savings = await evaluate(db, _FILTER_LIST, await _context(db), ids=changed,
                                                 facets=_FACET_LIST)
        index.patch(changed, alive, members, savings)
    assert hidden not in index.savings
    assert index.savings[edited] == 123456789
    assert index.savings[added] == 987654321
    fresh = await index_of(library)
    assert index.savings == fresh.savings
    assert index.alive == fresh.alive
    assert index.bits == fresh.bits


def test_the_definitions_version_follows_the_filters(monkeypatch):
    import dataclasses
    import backend.filter_index.index as index_module
    before = definitions_version()
    changed = [dataclasses.replace(f, sql=f.sql + " AND 1") if f.id == "x264" else f for f in _FILTER_LIST]
    monkeypatch.setattr(index_module, "_FILTER_LIST", changed)
    assert definitions_version() != before
    monkeypatch.setattr(index_module, "_FILTER_LIST", _FILTER_LIST)
    monkeypatch.setattr(index_module, "FILTERS_VERSION", index_module.FILTERS_VERSION + 1)
    assert definitions_version() != before
    monkeypatch.undo()
    monkeypatch.setattr(index_module, "LISTED", index_module.LISTED + " AND 1")
    assert definitions_version() != before
    monkeypatch.undo()
    monkeypatch.setattr(index_module, "PY_COLUMNS", index_module.PY_COLUMNS + ", video_codec")
    assert definitions_version() != before
